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

from app import correo
from app.main import LOCAL_TZ, SYNC_PLANES_HORA, TAG_GROUPS, hoy_local, parse_pares

ALERTAS_WEBHOOK_URL = os.getenv("ALERTAS_WEBHOOK_URL", "")
ALERTAS_WEBHOOK_TOKEN = os.getenv("ALERTAS_WEBHOOK_TOKEN", "")
ALERTAS_TRIAL_DIAS = int(os.getenv("ALERTAS_TRIAL_DIAS", "3"))
ALERTAS_PAGO_DIAS = int(os.getenv("ALERTAS_PAGO_DIAS", "2"))
ALERTAS_DUENO_DEFECTO = os.getenv("ALERTAS_DUENO_DEFECTO", "").strip().lower()
DASHBOARD_URL = os.getenv("DASHBOARD_URL", "").rstrip("/")

# Correo de cada responsable: 'alyssa:uno@x.com;otro@y.com,diego:d@x.com'. La coma separa
# personas (mismo formato que DASHBOARD_USERS, por eso reusa parse_pares) y el ';' las
# direcciones de una misma persona, que pueden ser varias.
ALERTAS_CORREOS = parse_pares(os.getenv("ALERTAS_CORREOS", ""))

TIMEOUT = 30

RESPONSABLES = set(TAG_GROUPS["responsable"])

TIPOS = {
    "trial_por_vencer": "Prueba gratis por vencer",
    "pago_estancado": "Pago sin aprobar",
}


def correos_de(dueno: str) -> list[str]:
    """Direcciones de esa persona. Varias separadas por ';' (una del trabajo, otra personal)."""
    return [c.strip() for c in ALERTAS_CORREOS.get(dueno, "").split(";") if c.strip()]


def estado_config() -> str:
    """Resumen de la configuracion, para el log de arranque.

    Dice explicitamente QUIEN se queda sin recibir nada: una alerta que no sale no da error
    en ningun sitio, y sin esta linea el fallo solo se nota cuando ya se perdio un cliente.
    """
    canales = []
    if correo.configurado():
        con = sorted(d for d in TAG_GROUPS["responsable"] if correos_de(d))
        sin = sorted(d for d in TAG_GROUPS["responsable"] if not correos_de(d))
        canales.append(f"correo a {', '.join(con) or 'nadie'}"
                       + (f" (SIN correo: {', '.join(sin)})" if sin else ""))
    if ALERTAS_WEBHOOK_URL:
        canales.append("webhook")
    if not canales:
        return ("desactivadas: no hay canal (falta ALERTAS_CORREOS + MAIL_* , "
                "o ALERTAS_WEBHOOK_URL). Las reglas se evalúan pero nadie recibe nada.")
    dueno = ALERTAS_DUENO_DEFECTO or "(ninguno: los leads sin responsable no generan alerta)"
    return (f"{' + '.join(canales)} | trial: {ALERTAS_TRIAL_DIAS} días antes | "
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
        # Canales que de verdad pueden entregar algo. El dashboard lo usa para no prometer
        # en pantalla un aviso que en realidad no sale de aqui.
        "canales": ([f"correo ({correo.MAIL_FROM})"] if correo.configurado() else [])
                   + (["webhook"] if ALERTAS_WEBHOOK_URL else []),
        "sin_correo": sorted(d for d in TAG_GROUPS["responsable"] if not correos_de(d)),
        "configurado": bool(ALERTAS_WEBHOOK_URL) or correo.configurado(),
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


def _pie(datos: dict) -> str:
    """Por que te llega esto y por que hoy. Va en TODOS los mensajes.

    El encargo pedia que quedara escrito con cuanta antelacion se avisa y quien actua. En la
    configuracion no vale: quien recibe el aviso no lee el .env.
    """
    u = datos["umbrales"]
    return (f"Aviso automático de MAU: pruebas que vencen en {u['trial_dias']} días o menos, "
            f"y pagos que llevan {u['pago_dias']} días o más sin aprobarse.\n"
            "Te llega a ti porque figuras como responsable de estos leads en el dashboard.")


def formatear(datos: dict, alertas: list[dict], dueno: str) -> str:
    """Mensaje de UNA persona, con sus leads y nada mas.

    Un solo mensaje con los leads de todos no tiene destinatario: en un grupo de cuatro,
    cada uno da por hecho que lo mira otro. Por eso se reparte por responsable.
    """
    partes = [f"*MAU · {len(alertas)} por atender — {hoy_local().isoformat()}*"]
    for tipo, titulo in TIPOS.items():
        del_tipo = [a for a in alertas if a["tipo"] == tipo]
        if del_tipo:
            partes.append(f"\n_{titulo}_")
            partes += [_linea(a) for a in del_tipo]
    partes.append("\n" + _pie(datos))
    return "\n".join(partes)


def _asunto(alertas: list[dict]) -> str:
    """Asunto que se entiende sin abrir el correo, desde la lista de la bandeja."""
    t = sum(1 for a in alertas if a["tipo"] == "trial_por_vencer")
    p = len(alertas) - t
    trozos = []
    if t:
        trozos.append(f"{t} prueba{'s' if t != 1 else ''} por vencer")
    if p:
        trozos.append(f"{p} pago{'s' if p != 1 else ''} sin aprobar")
    return "MAU · " + " y ".join(trozos)


def _cuantos(n: int, html: bool = False) -> str:
    """«Este lead necesita» / «Estos 3 leads necesitan», sin el clasico 'Estos 1 leads'."""
    if n == 1:
        return "este lead necesita" if html else "Este lead necesita"
    num = f"<b>{n}</b>" if html else str(n)
    return f"{'estos' if html else 'Estos'} {num} leads necesitan"


def texto_correo(datos: dict, alertas: list[dict], dueno: str) -> str:
    """Version en texto plano. No es relleno: es lo que se ve en la previsualizacion del
    movil y en los clientes que bloquean el HTML, que es justo cuando se mira de reojo."""
    partes = [f"Hola {dueno.capitalize()}:", "",
              f"{_cuantos(len(alertas))} que hagas algo hoy.", ""]
    for tipo, titulo in TIPOS.items():
        del_tipo = [a for a in alertas if a["tipo"] == tipo]
        if del_tipo:
            partes.append(titulo.upper())
            partes += [_linea(a) for a in del_tipo]
            partes.append("")
    partes += ["--", _pie(datos)]
    return "\n".join(partes)


def _esc(v) -> str:
    """Escapa para HTML. El nombre de un lead sale de WhatsApp y puede traer < o &."""
    return (str(v or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def html_correo(datos: dict, alertas: list[dict], dueno: str) -> str:
    """Version HTML. Estilos en linea y nada de CSS externo ni imagenes: los clientes de
    correo descartan lo primero y bloquean lo segundo."""
    bloques = []
    for tipo, titulo in TIPOS.items():
        del_tipo = [a for a in alertas if a["tipo"] == tipo]
        if not del_tipo:
            continue
        bloques.append(
            f'<h3 style="font:600 12px/1.4 Arial,sans-serif;letter-spacing:.08em;'
            f'text-transform:uppercase;color:#1e3a8a;margin:22px 0 8px">{_esc(titulo)}</h3>'
        )
        for a in del_tipo:
            if a["tipo"] == "trial_por_vencer":
                detalle = ("vence <b>HOY</b>" if a["dias"] == 0
                           else f"vence en <b>{a['dias']} día{'s' if a['dias'] != 1 else ''}</b>")
                detalle += f" ({_esc(a['fecha'])})"
                color = "#dc2626" if a["dias"] <= 1 else "#d97706"
            else:
                detalle = (f"<b>{a['dias']} días</b> esperando aprobación "
                           f"(desde {_esc(a['fecha'])})")
                color = "#dc2626"
            empresa = f" · {_esc(a['empresa'])}" if a["empresa"] else ""
            paso = (f'<div style="font:13px/1.5 Arial,sans-serif;color:#475569;margin-top:4px">'
                    f'Siguiente paso: {_esc(a["siguiente_paso"])}</div>') if a.get("siguiente_paso") else ""
            enlace = (f'<div style="margin-top:6px"><a href="{_esc(a["url"])}" '
                      f'style="font:13px Arial,sans-serif;color:#2563eb">Abrir en el dashboard</a></div>'
                      ) if a.get("url") else ""
            bloques.append(
                f'<div style="border-left:3px solid {color};padding:2px 0 2px 12px;margin-bottom:14px">'
                f'<div style="font:600 15px/1.4 Arial,sans-serif;color:#1e293b">'
                f'{_esc(a["nombre"])}{empresa}</div>'
                f'<div style="font:13px/1.5 Arial,sans-serif;color:#64748b">{_esc(a["telefono"])}</div>'
                f'<div style="font:14px/1.5 Arial,sans-serif;color:#1e293b;margin-top:3px">{detalle}</div>'
                f'{paso}{enlace}</div>'
            )
    return (
        '<div style="max-width:600px;margin:0 auto;padding:24px;background:#ffffff">'
        '<div style="font:800 20px Georgia,serif;color:#1e3a8a">MAU</div>'
        '<div style="font:10px/1.4 Arial,sans-serif;letter-spacing:.12em;text-transform:uppercase;'
        'color:#64748b;margin-bottom:20px">Lead Scoring · Contatech</div>'
        f'<p style="font:15px/1.6 Arial,sans-serif;color:#1e293b">Hola <b>{_esc(dueno.capitalize())}</b>: '
        f'{_cuantos(len(alertas), html=True)} que hagas algo hoy.</p>'
        + "".join(bloques) +
        '<p style="font:12px/1.6 Arial,sans-serif;color:#94a3b8;border-top:1px solid #e2e8f0;'
        f'padding-top:12px;margin-top:24px">{_esc(_pie(datos)).replace(chr(10), "<br>")}</p>'
        '</div>'
    )


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


async def _reservar(conn: asyncpg.Connection, alertas: list[dict]) -> list[dict]:
    """Aparta las que todavia no se han avisado. Reservar ANTES de enviar es lo que evita
    que dos ejecuciones solapadas manden el mismo aviso dos veces."""
    nuevas = []
    for a in alertas:
        if await conn.fetchval(
            "INSERT INTO lead_alertas (lead_id, tipo, clave) VALUES ($1, $2, $3) "
            "ON CONFLICT DO NOTHING RETURNING lead_id",
            a["lead_id"], a["tipo"], _clave(a),
        ):
            nuevas.append(a)
    return nuevas


async def _soltar(conn: asyncpg.Connection, alertas: list[dict]) -> None:
    """Devuelve las reservas para reintentar mañana.

    Se hace cuando NINGUN canal pudo entregar. Perder del todo el aviso de una prueba que
    vence porque el correo estaba caido es peor que el riesgo contrario, que es un unico
    duplicado si el servidor recibio el mensaje y luego dio error.
    """
    await conn.executemany(
        "DELETE FROM lead_alertas WHERE lead_id=$1 AND tipo=$2 AND clave=$3",
        [(a["lead_id"], a["tipo"], _clave(a)) for a in alertas],
    )


async def revisar_y_avisar(conn: asyncpg.Connection) -> dict:
    """Evalua, descarta lo ya avisado y reparte lo que queda. Idempotente.

    Se manda UN mensaje por responsable, con sus leads y nada mas. Un unico mensaje con los
    de todos no tiene destinatario: cada uno da por hecho que lo mira otro.

    Cada persona se procesa por separado, asi que si falla el correo de una, las demas se
    envian igual y solo se reintenta la suya.
    """
    datos = await evaluar(conn)
    r = {**datos, "enviadas": 0, "repetidas": 0, "sin_canal": 0,
         "por_tipo": {t: 0 for t in TIPOS}, "fallos": []}

    for dueno in sorted({a["responsable"] for a in datos["alertas"]}):
        suyas = [a for a in datos["alertas"] if a["responsable"] == dueno]
        destinos = correos_de(dueno)

        canales = []
        if correo.configurado() and destinos:
            canales.append("correo")
        if ALERTAS_WEBHOOK_URL:
            canales.append("webhook")
        if not canales:
            # Sin correo ni webhook no hay por donde avisarle: no se reserva nada, para que
            # el aviso siga pendiente el dia que si haya canal.
            r["sin_canal"] += len(suyas)
            continue

        nuevas = await _reservar(conn, suyas)
        r["repetidas"] += len(suyas) - len(nuevas)
        if not nuevas:
            continue

        entregado = False
        for canal in canales:
            try:
                if canal == "correo":
                    await asyncio.to_thread(
                        correo.enviar, destinos, _asunto(nuevas),
                        texto_correo(datos, nuevas, dueno), html_correo(datos, nuevas, dueno),
                    )
                else:
                    await asyncio.to_thread(_enviar, {
                        "generado": datos["generado"],
                        "umbrales": datos["umbrales"],
                        "responsable": dueno,
                        "resumen": {t: sum(1 for a in nuevas if a["tipo"] == t) for t in TIPOS},
                        "texto": formatear(datos, nuevas, dueno),
                        "alertas": nuevas,
                    })
                entregado = True
            except Exception as e:  # noqa: BLE001 — SMTP caido, n8n reiniciandose, red...
                r["fallos"].append(f"{dueno}/{canal}: {e}")

        if entregado:
            r["enviadas"] += len(nuevas)
            for a in nuevas:
                r["por_tipo"][a["tipo"]] += 1
        else:
            await _soltar(conn, nuevas)

    return r
