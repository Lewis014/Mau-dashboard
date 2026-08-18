"""
Sincroniza el estado comercial de cada lead desde mau-web (bot.contatech.lat).

Por que existe: la pantalla /administrators de mau-web no puede responder "quien probo y
no compro". Lee user_plans, una fila por usuario que se sobrescribe, y su cron lleva a
'Expirado' tanto al que agoto la prueba gratis como al cliente que dejo de renovar. Lo que
si distingue es user_plan_history (append-only): una fila 'Aprobado' por cada pago. El
endpoint POST /api/leads/plan-state de mau-web ya devuelve ese estado derivado; aqui solo
se cruza con los leads y se escribe.

El cruce es por telefono con respaldo por correo: lead_id viene como '51987654321' y
mau-web guarda 9 digitos sin codigo de pais.

De las etiquetas, el sync manda SOLO sobre free_trial / cliente / perdido (PLAN_TAGS), y
unicamente en los leads que cruzaron. Las de flujo de venta (llamada, insistir, demo_*),
el responsable y el canal son trabajo del vendedor y quedan intactas.

Uso (dentro del contenedor del dashboard):
    docker compose exec dashboard python -m app.sync_planes --dry-run
    docker compose exec dashboard python -m app.sync_planes
"""

import argparse
import asyncio
import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Optional

import asyncpg

from app.main import PLAN_ESTADO_TAG, PLAN_TAGS, primary_outcome

MAUWEB_API_URL = os.getenv("MAUWEB_API_URL", "https://bot.contatech.lat")
MAUWEB_API_TOKEN = os.getenv("MAUWEB_API_TOKEN", "")

# El endpoint acepta como mucho 500 de cada lista por peticion (MAX_ITEMS del controlador).
LOTE = 500
TIMEOUT = 60


def telefono_local(valor: str) -> Optional[str]:
    """Normaliza a los 9 digitos peruanos: '+51 987 654 321', '51987654321' -> '987654321'.

    Se aplica a los dos lados del cruce. lead_id llega con el prefijo 51 y mau-web guarda
    9 digitos, pero no siempre: hay filas con el 51 incluido y otras mas cortas, porque
    solo el registro valida la longitud (los caminos de perfil y de admin no).
    """
    d = "".join(c for c in (valor or "") if c.isdigit())
    if len(d) == 11 and d.startswith("51"):
        d = d[2:]
    return d if len(d) == 9 else None


def consultar(telefonos: list[str], correos: list[str]) -> list[dict]:
    """POST /api/leads/plan-state. urllib y no httpx: el repo no tiene mas dependencias."""
    payload = json.dumps({"telefonos": telefonos, "correos": correos}).encode()
    req = urllib.request.Request(
        f"{MAUWEB_API_URL.rstrip('/')}/api/leads/plan-state",
        data=payload,
        headers={
            "Authorization": MAUWEB_API_TOKEN,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read()).get("data") or []
    except urllib.error.HTTPError as e:
        detalle = e.read().decode(errors="replace")[:300]
        raise RuntimeError(f"mau-web respondio {e.code}: {detalle}") from e


def _dia(valor: Any) -> Optional[datetime]:
    """'YYYY-MM-DD' (o datetime ISO) -> datetime con tz; None si viene vacio."""
    if not valor:
        return None
    txt = str(valor).replace("Z", "+00:00")
    try:
        d = datetime.fromisoformat(txt if len(txt) > 10 else f"{txt}T00:00:00")
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def fusionar_tags(actuales: list[str], estado: str) -> list[str]:
    """Sustituye la etiqueta comercial dejando intacto todo lo demas."""
    tag = PLAN_ESTADO_TAG.get(estado)
    resto = [t for t in (actuales or []) if t not in PLAN_TAGS]
    return resto + [tag] if tag else resto


async def sincronizar(conn: asyncpg.Connection, dry_run: bool = False,
                      limit: int = 0) -> dict:
    """Cruza los leads con mau-web y devuelve el resumen. Reutilizable desde la API."""
    if not MAUWEB_API_TOKEN:
        raise RuntimeError("Falta MAUWEB_API_TOKEN")

    leads = await conn.fetch(
        "SELECT lead_id, email, outcome_tags FROM leads_dataset WHERE is_test = false"
        + (f" LIMIT {int(limit)}" if limit else "")
    )

    def correo_norm(v) -> Optional[str]:
        return (v or "").strip().lower() or None

    telefonos_lead = {t for r in leads if (t := telefono_local(r["lead_id"]))}
    correos_lead = {c for r in leads if (c := correo_norm(r["email"]))}

    # consultar() es urllib, o sea bloqueante: va en un hilo para que la version que corre
    # desde la API del dashboard no congele el event loop mientras mau-web responde.
    # Se mandan las dos formas del telefono porque mau-web no las guarda de forma uniforme:
    # la mayoria son 9 digitos, pero hay filas con el 51 delante.
    telefonos = sorted({t for tel in telefonos_lead for t in (tel, "51" + tel)})
    correos = sorted(correos_lead)
    cuentas: list[dict] = []
    for i in range(0, max(len(telefonos), len(correos), 1), LOTE):
        cuentas += await asyncio.to_thread(
            consultar, telefonos[i:i + LOTE], correos[i:i + LOTE]
        )

    # Un mismo telefono o correo puede apuntar a varias cuentas: '999999999' es el relleno
    # de decenas de registros, y el indice unico de email se elimino hace tiempo. Cuando
    # pasa no hay forma de saber cual es la buena, asi que ese cruce se descarta en vez de
    # asignar un estado al azar; si el otro campo si es univoco, se resuelve por ahi.
    def indexar(campo, norm) -> tuple[dict[str, dict], set[str]]:
        vistos: dict[str, dict] = {}
        repetidos: set[str] = set()
        for c in cuentas:
            clave = norm(c.get(campo))
            if not clave:
                continue
            if clave in vistos and vistos[clave]["user_id"] != c["user_id"]:
                repetidos.add(clave)
            vistos.setdefault(clave, c)
        return {k: v for k, v in vistos.items() if k not in repetidos}, repetidos

    cuenta_por_tel, tel_repetidos = indexar("telefono", telefono_local)
    cuenta_por_correo, correo_repetidos = indexar("correo", correo_norm)

    # Telefono primero; el correo solo entra si ese lead no cruzo ya por telefono.
    hallado: dict[str, dict] = {}
    ambiguos = 0
    for r in leads:
        tel = telefono_local(r["lead_id"])
        correo = correo_norm(r["email"])
        cuenta, via = (cuenta_por_tel.get(tel) if tel else None), "telefono"
        if not cuenta and correo:
            cuenta, via = cuenta_por_correo.get(correo), "correo"
        if cuenta:
            hallado[r["lead_id"]] = {**cuenta, "match": via}
        elif (tel in tel_repetidos) or (correo in correo_repetidos):
            ambiguos += 1

    resumen = {
        "leads": len(leads), "cuentas": len(cuentas), "cruzados": len(hallado),
        "por_telefono": sum(1 for h in hallado.values() if h["match"] == "telefono"),
        "por_correo": sum(1 for h in hallado.values() if h["match"] == "correo"),
        "ambiguos": ambiguos,
        "sin_cuenta": len(leads) - len(hallado), "por_estado": {}, "dry_run": dry_run,
    }

    ahora = datetime.now(timezone.utc)
    for r in leads:
        cuenta = hallado.get(r["lead_id"])
        if not cuenta:
            continue   # sin cuenta en mau-web: no se toca su etiquetado
        estado = cuenta.get("estado") or "sin_cuenta"
        resumen["por_estado"][estado] = resumen["por_estado"].get(estado, 0) + 1
        if dry_run:
            continue
        tags = fusionar_tags(list(r["outcome_tags"] or []), estado)
        await conn.execute(
            """
            UPDATE leads_dataset
               SET plan_estado=$2, plan_nombre=$3, plan_inicia=$4, plan_expira=$5,
                   plan_pagos=$6, plan_user_id=$7, plan_match=$8, plan_sync_at=$9,
                   outcome_tags=$10, outcome=$11, updated_at=NOW()
             WHERE lead_id=$1
            """,
            r["lead_id"], estado, cuenta.get("plan"),
            _dia(cuenta.get("inicia")), _dia(cuenta.get("expira")),
            cuenta.get("pagos"), cuenta.get("user_id"), cuenta["match"], ahora,
            tags, primary_outcome(tags),
        )

    # Marca a los que no cruzaron, y limpia los datos de plan por si antes si cruzaban
    # (cuenta borrada o telefono corregido). Con --limit no se hace: solo se miro una
    # muestra, y marcar al resto como sin_cuenta seria mentira.
    if not dry_run and not limit:
        await conn.execute(
            """
            UPDATE leads_dataset
               SET plan_estado='sin_cuenta', plan_sync_at=$1, plan_nombre=NULL,
                   plan_inicia=NULL, plan_expira=NULL, plan_pagos=NULL,
                   plan_user_id=NULL, plan_match=NULL
             WHERE is_test = false AND NOT (lead_id = ANY($2::text[]))
            """,
            ahora, list(hallado),
        )
    return resumen


async def run(args: argparse.Namespace) -> None:
    conn = await asyncpg.connect(
        host=os.getenv("DB_HOST", "postgres"),
        port=int(os.getenv("DB_PORT", "5432")),
        database=os.getenv("DB_NAME", "n8n"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
    )
    try:
        r = await sincronizar(conn, dry_run=args.dry_run, limit=args.limit)
        print(f"\nLeads: {r['leads']}  |  cuentas devueltas por mau-web: {r['cuentas']}")
        print(f"Cruzados: {r['cruzados']}  (telefono={r['por_telefono']}  correo={r['por_correo']})")
        print(f"Sin cuenta en mau-web: {r['sin_cuenta']}"
              + (f"  |  de esos, {r['ambiguos']} tenian el dato repetido en varias cuentas"
                 " y se descartaron a proposito" if r["ambiguos"] else "") + "\n")
        for estado, n in sorted(r["por_estado"].items(), key=lambda kv: -kv[1]):
            etiqueta = PLAN_ESTADO_TAG.get(estado) or "(ninguna)"
            print(f"  {estado:<22} {n:>5}   -> etiqueta {etiqueta}")
        if args.dry_run:
            print("\n(dry-run: no se escribio nada)")
    finally:
        await conn.close()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Sincroniza el estado comercial de los leads desde mau-web")
    p.add_argument("--dry-run", action="store_true", help="No escribe en DB; solo imprime el resumen del cruce.")
    p.add_argument("--limit", type=int, default=0, help="Procesa como maximo N leads (0 = todos).")
    return p.parse_args()


if __name__ == "__main__":
    asyncio.run(run(parse_args()))
