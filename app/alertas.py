"""
Alertas automaticas: pruebas gratis por vencer y pagos atascados en verificacion.

Que problema resuelve: hoy nadie se entera de que una prueba vence pasado mañana salvo que
alguien entre al dashboard y mire. La alerta llega sola a un canal fijo, sin que nadie
consulte nada.

SOBRE "PAGO FALLIDO": mau-web NO reporta pagos rechazados. Su endpoint /api/leads/plan-state
devuelve seis estados y ninguno es "rechazado" — los pagos los aprueba un administrador a
mano y un rechazo no deja rastro consultable. Lo que si se puede detectar, y es el agujero
real de dinero, es el pago ATASCADO: el cliente subio su comprobante y lleva dias sin que
nadie lo revise. Eso es lo que avisa `pago_estancado`. Para el rechazo de verdad haria falta
que mau-web expusiera el estado 'Rechazado' en ese endpoint.

QUIEN ACTUA: el responsable sale de las etiquetas del lead (alyssa/diego/jhon). Un lead sin
responsable usa ALERTAS_DUENO_DEFECTO, y si esa variable esta vacia NO SE AVISA de ese lead:
una alerta que no tiene a quien ir dirigida no la atiende nadie, y solo sirve para acostumbrar
al grupo a ignorar avisos.

CON CUANTA ANTELACION: ALERTAS_TRIAL_DIAS (3 por defecto) para las pruebas y ALERTAS_PAGO_DIAS
(2 por defecto) para los pagos. Los dos numeros van escritos dentro del propio mensaje.

El canal es un webhook de n8n, que ya vive en el mismo stack y ya habla con WhatsApp: aqui
solo se manda el JSON con el texto ya formateado.
"""

import asyncio
import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Optional

import asyncpg

from app.main import LOCAL_TZ, SYNC_PLANES_HORA, TAG_GROUPS, hoy_local

ALERTAS_WEBHOOK_URL = os.getenv("ALERTAS_WEBHOOK_URL", "")
ALERTAS_WEBHOOK_TOKEN = os.getenv("ALERTAS_WEBHOOK_TOKEN", "")
ALERTAS_TRIAL_DIAS = int(os.getenv("ALERTAS_TRIAL_DIAS", "3"))
ALERTAS_PAGO_DIAS = int(os.getenv("ALERTAS_PAGO_DIAS", "2"))
ALERTAS_DUENO_DEFECTO = os.getenv("ALERTAS_DUENO_DEFECTO", "").strip().lower()
DASHBOARD_URL = os.getenv("DASHBOARD_URL", "").rstrip("/")

TIMEOUT = 30

RESPONSABLES = set(TAG_GROUPS["responsable"])

TIPOS = {
    "trial_por_vencer": "Prueba gratis por vencer",
    "pago_estancado": "Pago sin aprobar",
}


def estado_config() -> str:
    """Resumen de la configuracion, para el log de arranque."""
    if not ALERTAS_WEBHOOK_URL:
        return "desactivadas: falta ALERTAS_WEBHOOK_URL"
    dueno = ALERTAS_DUENO_DEFECTO or "(ninguno: los leads sin responsable no generan alerta)"
    return (f"activas | trial: {ALERTAS_TRIAL_DIAS} días antes | "
            f"pago: {ALERTAS_PAGO_DIAS} días sin aprobar | dueño por defecto: {dueno}")


def _responsable(tags: Optional[list[str]]) -> Optional[str]:
    """El responsable del lead segun sus etiquetas, o el de defecto, o None.

    None significa exactamente «nadie tiene asignado actuar»: ese lead no genera alerta.
    """
    for t in (tags or []):
        if t in RESPONSABLES:
            return t
    return ALERTAS_DUENO_DEFECTO or None


def _nombre(r: asyncpg.Record) -> str:
    return r["contact_name"] or r["wa_display_name"] or f"+{r['lead_id']}"


SQL_TRIAL = """
SELECT lead_id, contact_name, wa_display_name, company_name, outcome_tags,
       plan_nombre, siguiente_paso,
       'trial_por_vencer'                              AS tipo,
       (plan_expira AT TIME ZONE $1::text)::date       AS fecha,
       (plan_expira AT TIME ZONE $1::text)::date - $2::date AS dias
  FROM leads_dataset
 WHERE is_test = false
   AND plan_estado = 'trial_activo'
   AND plan_expira IS NOT NULL
   AND (plan_expira AT TIME ZONE $1::text)::date BETWEEN $2::date AND $2::date + $3::int
"""

SQL_PAGO = """
SELECT lead_id, contact_name, wa_display_name, company_name, outcome_tags,
       plan_nombre, siguiente_paso,
       'pago_estancado'                                     AS tipo,
       (plan_estado_desde AT TIME ZONE $1::text)::date       AS fecha,
       $2::date - (plan_estado_desde AT TIME ZONE $1::text)::date AS dias
  FROM leads_dataset
 WHERE is_test = false
   AND plan_estado = 'pago_en_verificacion'
   AND plan_estado_desde IS NOT NULL
   AND (plan_estado_desde AT TIME ZONE $1::text)::date <= $2::date - $3::int
"""


async def evaluar(conn: asyncpg.Connection) -> dict:
    """Evalua las reglas y devuelve las alertas que corresponderian HOY.

    No escribe ni envia nada: es lo que alimenta el boton «Ver alertas de hoy» y tambien el
    primer paso del envio real.
    """
    hoy = hoy_local()
    filas = list(await conn.fetch(SQL_TRIAL, LOCAL_TZ, hoy, ALERTAS_TRIAL_DIAS))
    filas += list(await conn.fetch(SQL_PAGO, LOCAL_TZ, hoy, ALERTAS_PAGO_DIAS))

    alertas, sin_dueno = [], 0
    for r in filas:
        dueno = _responsable(list(r["outcome_tags"] or []))
        if not dueno:
            sin_dueno += 1
            continue
        alertas.append({
            "tipo": r["tipo"],
            "lead_id": r["lead_id"],
            "telefono": f"+{r['lead_id']}",
            "nombre": _nombre(r),
            "empresa": r["company_name"],
            "responsable": dueno,
            "plan": r["plan_nombre"],
            "dias": int(r["dias"]),
            # Vencimiento del trial, o dia en que entro el pago en verificacion.
            "fecha": r["fecha"].isoformat(),
            "siguiente_paso": r["siguiente_paso"],
            "url": f"{DASHBOARD_URL}/#detail/{r['lead_id']}" if DASHBOARD_URL else None,
        })

    alertas.sort(key=lambda a: (a["responsable"], a["tipo"], a["dias"]))
    por_tipo = {t: sum(1 for a in alertas if a["tipo"] == t) for t in TIPOS}
    return {
        "generado": datetime.now(timezone.utc).isoformat(),
        "umbrales": {"trial_dias": ALERTAS_TRIAL_DIAS, "pago_dias": ALERTAS_PAGO_DIAS},
        # La hora y la zona salen de aqui para que la regla escrita en el dashboard sea la
        # que de verdad rige, y no unos numeros copiados a mano en el HTML.
        "hora": SYNC_PLANES_HORA,
        "zona": LOCAL_TZ,
        "por_tipo": por_tipo,
        "sin_dueno": sin_dueno,
        "alertas": alertas,
        "configurado": bool(ALERTAS_WEBHOOK_URL),
    }


def _linea(a: dict) -> str:
    quien = a["nombre"] + (f" ({a['empresa']})" if a["empresa"] else "")
    if a["tipo"] == "trial_por_vencer":
        cuando = "vence HOY" if a["dias"] == 0 else f"vence en {a['dias']} día{'s' if a['dias'] != 1 else ''}"
        detalle = f"{cuando} ({a['fecha']})"
    else:
        detalle = f"{a['dias']} días esperando aprobación (desde {a['fecha']})"
    paso = f"\n     Siguiente paso: {a['siguiente_paso']}" if a.get("siguiente_paso") else ""
    url = f"\n     {a['url']}" if a.get("url") else ""
    return f"  • {quien} — {a['telefono']}\n     {detalle}{paso}{url}"


def formatear(datos: dict, alertas: list[dict]) -> str:
    """Mensaje listo para reenviar tal cual al grupo, agrupado por responsable.

    La antelacion va escrita dentro del mensaje y no solo en la configuracion: quien lo
    recibe tiene que poder saber por que le esta llegando esto hoy y no ayer.
    """
    u = datos["umbrales"]
    partes = [
        f"*MAU · Alertas del {hoy_local().isoformat()}*",
        f"_Aviso automático: pruebas que vencen en {u['trial_dias']} días o menos, "
        f"y pagos que llevan {u['pago_dias']} días o más sin aprobarse._",
    ]
    for dueno in sorted({a["responsable"] for a in alertas}):
        suyas = [a for a in alertas if a["responsable"] == dueno]
        partes.append(f"\n*{dueno.capitalize()}* — {len(suyas)} por atender")
        for tipo, titulo in TIPOS.items():
            del_tipo = [a for a in suyas if a["tipo"] == tipo]
            if del_tipo:
                partes.append(f"\n_{titulo}_")
                partes += [_linea(a) for a in del_tipo]
    return "\n".join(partes)


def _enviar(payload: dict) -> None:
    """POST al webhook de n8n. urllib y no httpx: el repo no añade dependencias."""
    req = urllib.request.Request(
        ALERTAS_WEBHOOK_URL,
        data=json.dumps(payload, ensure_ascii=False).encode(),
        headers={
            "Authorization": ALERTAS_WEBHOOK_TOKEN,
            "Content-Type": "application/json; charset=utf-8",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            r.read()
    except urllib.error.HTTPError as e:
        detalle = e.read().decode(errors="replace")[:300]
        raise RuntimeError(f"el webhook respondio {e.code}: {detalle}") from e


def _clave(a: dict) -> str:
    """Identifica el HECHO del que se avisa, no el dia en que se avisa.

    Para un trial es su fecha de vencimiento y para un pago el dia en que entro en
    verificacion: asi el mismo aviso no se repite cada mañana, pero un trial renovado
    (vencimiento nuevo) o un pago que vuelve a atascarse si generan una alerta nueva.
    """
    return a["fecha"]


async def revisar_y_avisar(conn: asyncpg.Connection) -> dict:
    """Evalua, descarta lo ya avisado y manda lo que queda. Idempotente."""
    datos = await evaluar(conn)
    if not ALERTAS_WEBHOOK_URL:
        return {**datos, "enviadas": 0, "repetidas": 0, "omitido": "falta ALERTAS_WEBHOOK_URL"}

    # Se reserva ANTES de enviar para que dos ejecuciones solapadas no avisen dos veces:
    # solo la que se lleva la fila manda el mensaje.
    nuevas = []
    for a in datos["alertas"]:
        reservada = await conn.fetchval(
            "INSERT INTO lead_alertas (lead_id, tipo, clave) VALUES ($1, $2, $3) "
            "ON CONFLICT DO NOTHING RETURNING lead_id",
            a["lead_id"], a["tipo"], _clave(a),
        )
        if reservada:
            nuevas.append(a)

    resultado = {**datos, "enviadas": len(nuevas),
                 "repetidas": len(datos["alertas"]) - len(nuevas),
                 "por_tipo": {t: sum(1 for a in nuevas if a["tipo"] == t) for t in TIPOS}}
    if not nuevas:
        return resultado

    try:
        await asyncio.to_thread(_enviar, {
            "generado": datos["generado"],
            "umbrales": datos["umbrales"],
            "resumen": resultado["por_tipo"],
            "texto": formatear(datos, nuevas),
            "alertas": nuevas,
        })
    except Exception:
        # Si el envio falla se sueltan las reservas para reintentar mañana. Perder del todo
        # el aviso de una prueba que vence porque n8n estaba reiniciandose es peor que el
        # riesgo contrario, que es un unico duplicado si el webhook recibio y luego fallo.
        await conn.executemany(
            "DELETE FROM lead_alertas WHERE lead_id=$1 AND tipo=$2 AND clave=$3",
            [(a["lead_id"], a["tipo"], _clave(a)) for a in nuevas],
        )
        raise
    return resultado
