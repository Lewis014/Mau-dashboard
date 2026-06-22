"""
Backfill de leads_dataset desde los transcripts crudos de las conversaciones.

FUENTE POR DEFECTO: Chatwoot (API). Es la fuente de verdad — contiene TODOS los
mensajes (lead + bot + agente humano). La memoria del bot (`n8n_chat_histories`)
solo guarda lead+bot y NO registra lo que pasa cuando un humano toma la
conversacion (~20% de los casos), introduciendo un sesgo de censura justo en los
leads mas valiosos. Por eso el dataset se reconstruye desde Chatwoot.
Fuente alterna `--source n8n` queda como fallback.

Re-extrae las features estructuradas con Claude (tool-use, JSON forzado).
Decisiones del doc de tesis que respeta:
  - Re-extraccion desde texto crudo, no del JSON viejo de handoff.
  - Anti-alucinacion: num_rucs / volumen_comprobantes solo si el lead dio cifra explicita.
  - Filtra sesiones con < 2 turnos humanos sustantivos (basura tipo "Hola"/"Hola").
  - Upsert preserva columnas manuales (outcome*, captured_at, is_test) y las del flujo n8n.
  - Marca el lead de prueba conocido (51934226756) como is_test.

Uso (dentro del contenedor del dashboard):
    docker compose exec dashboard python -m app.backfill                 # Chatwoot
    docker compose exec dashboard python -m app.backfill --dry-run --limit 5
    docker compose exec dashboard python -m app.backfill --source n8n    # fallback
    docker compose exec dashboard python -m app.backfill --only-lead 51999999999

Env relevante:
    DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD  -> Postgres compartido con n8n
    ANTHROPIC_API_KEY                            -> extraccion
    EXTRACT_MODEL (default claude-sonnet-4-6)    -> modelo del extractor
    CHATWOOT_HOST (default http://chatwoot_web:3000)
    CHATWOOT_ACCOUNT_ID (default 1), CHATWOOT_INBOX_ID (default 1)
    CHATWOOT_API_TOKEN                           -> token de acceso a la API de Chatwoot
"""

import argparse
import asyncio
import json
import os
import urllib.parse
import urllib.request
from typing import Any, Optional

import asyncpg
import anthropic

# ── Constantes de negocio ────────────────────────────────────────────────────

SESSION_PREFIX = "sales-bot-"
TEST_LEADS = {"51934226756"}          # primer lead capturado (Douglas / correo de Piero)
MIN_SUBSTANTIVE_HUMAN_TURNS = 2       # filtro anti-basura
SUBSTANTIVE_MIN_CHARS = 15            # un turno humano "cuenta" si supera esto

EXTRACT_MODEL = os.getenv("EXTRACT_MODEL", "claude-sonnet-4-6")

CHATWOOT_HOST = os.getenv("CHATWOOT_HOST", "http://chatwoot_web:3000")
CHATWOOT_ACCOUNT = os.getenv("CHATWOOT_ACCOUNT_ID", "1")
CHATWOOT_INBOX = int(os.getenv("CHATWOOT_INBOX_ID", "1"))
CHATWOOT_TOKEN = os.getenv("CHATWOOT_API_TOKEN", "")

SYSTEM_PROMPT = (
    "Eres un analista de datos de Contatech. MAU es el asistente contable virtual "
    "B2B (Peru/LATAM) que vende 3 modulos: COMUNICA (notificaciones SUNAT multi-RUC), "
    "VALIDA (validacion masiva de comprobantes) y PROCESA (registro contable automatico "
    "con integracion a CONCAR/EXIGE/Contasis/Starsoft/Odoo).\n\n"
    "Recibiras la transcripcion de una conversacion real de WhatsApp entre un lead y el "
    "equipo de ventas (turnos LEAD, BOT y AGENTE humano). Extrae UNICAMENTE las features "
    "estructuradas respaldadas por el texto. Reglas estrictas:\n"
    "- NUNCA inventes ni infieras numeros. 'num_rucs' y 'volumen_comprobantes' solo se "
    "llenan si el lead dio una cifra explicita; si no, dejalos en null.\n"
    "- Usa unicamente los valores permitidos en cada enum; si no aplica o no se sabe, deja null.\n"
    "- 'dolor_principal' e 'industry' son texto libre corto (<= 8 palabras), solo si el lead lo expreso.\n"
    "- 'pidio_demo' es true solo si el lead pidio explicitamente una demo/reunion/llamada.\n"
    "- 'qualified' es un heuristico: true si el lead mostro intencion real (dio datos de "
    "contacto, pidio precio/demo, describio su operacion). No es el resultado de venta."
)

EXTRACT_TOOL = {
    "name": "registrar_lead",
    "description": "Registra las features estructuradas extraidas de la conversacion del lead.",
    "input_schema": {
        "type": "object",
        "properties": {
            "contact_name": {"type": ["string", "null"], "description": "Nombre de la persona, si lo dio."},
            "email": {"type": ["string", "null"]},
            "company_name": {"type": ["string", "null"], "description": "Razon social o nombre comercial, si lo dio."},
            "tax_id": {"type": ["string", "null"], "description": "RUC (11 digitos) SOLO si el lead lo escribio."},
            "industry": {"type": ["string", "null"], "description": "Rubro/sector, texto corto, solo si se menciona."},
            "segmento": {
                "type": ["string", "null"],
                "enum": ["independiente", "estudio", "empresa", None],
                "description": "Contador independiente, estudio contable, o empresa.",
            },
            "num_rucs": {"type": ["integer", "null"], "description": "Cantidad de RUCs que maneja. SOLO si dio numero explicito."},
            "volumen_comprobantes": {"type": ["integer", "null"], "description": "Comprobantes/mes. SOLO si dio numero explicito."},
            "modulos_interes": {
                "type": "array",
                "items": {"type": "string", "enum": ["comunica", "valida", "procesa"]},
                "description": "Modulos por los que el lead mostro interes. Vacio si ninguno claro.",
            },
            "solucion_actual": {
                "type": ["string", "null"],
                "enum": ["manual", "concar", "exige", "contasis", "starsoft", "odoo", "otro", "ninguno", None],
                "description": "Que usa hoy el lead.",
            },
            "objecion": {
                "type": ["string", "null"],
                "enum": ["precio", "tiene_personal", "integracion", "pensarlo", "ninguna", None],
            },
            "urgencia": {"type": ["string", "null"], "enum": ["alta", "media", "baja", None]},
            "pidio_demo": {"type": "boolean"},
            "dolor_principal": {"type": ["string", "null"], "description": "Problema concreto que expreso el lead, texto corto."},
            "tipo_lead": {"type": ["string", "null"], "description": "Juicio cualitativo libre del agente (ej. 'caliente', 'curioso')."},
            "ticket_estimado": {"type": ["string", "null"], "description": "Estimacion de ticket si se infiere del contexto, ej. '< S/300/mes'."},
            "qualified": {"type": "boolean"},
        },
        "required": ["modulos_interes", "pidio_demo", "qualified"],
    },
}

FEATURE_COLS = [
    "contact_name", "email", "company_name", "tax_id", "industry", "segmento",
    "num_rucs", "volumen_comprobantes", "modulos_interes", "solucion_actual",
    "objecion", "urgencia", "pidio_demo", "dolor_principal", "tipo_lead",
    "ticket_estimado", "qualified",
]


# ── Fuente Chatwoot (API) ────────────────────────────────────────────────────

def _cw_get(path: str, params: Optional[dict] = None) -> dict:
    url = f"{CHATWOOT_HOST}/api/v1/accounts/{CHATWOOT_ACCOUNT}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"api_access_token": CHATWOOT_TOKEN})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def cw_list_conversations() -> list[dict]:
    """Lista todas las conversaciones del inbox de WhatsApp (paginado)."""
    convs, page = [], 1
    while True:
        data = _cw_get("/conversations", {
            "status": "all", "assignee_type": "all",
            "inbox_id": CHATWOOT_INBOX, "page": page,
        })
        payload = (data.get("data") or {}).get("payload") or []
        if not payload:
            break
        convs.extend(payload)
        page += 1
    return convs


def cw_messages(conv_id: int) -> list[dict]:
    """Trae TODOS los mensajes de una conversacion (paginado hacia atras)."""
    msgs: list[dict] = []
    before: Optional[int] = None
    while True:
        params = {"before": before} if before else None
        data = _cw_get(f"/conversations/{conv_id}/messages", params)
        payload = data.get("payload") or []
        if not payload:
            break
        msgs = payload + msgs
        before = min(m.get("id", 0) for m in payload)
        if len(payload) < 20:
            break
    return msgs


def transcript_from_chatwoot(messages: list[dict]) -> tuple[str, int, int]:
    """Reconstruye (texto, total_mensajes, turnos_humanos_sustantivos) desde Chatwoot.

    message_type: 0=incoming(LEAD), 1=outgoing(BOT/AGENTE), 2=activity, 3=template.
    Se omiten notas privadas (private=true) y mensajes de actividad/sistema.
    """
    lines: list[str] = []
    total = substantive = 0
    for m in sorted(messages, key=lambda x: x.get("id", 0)):
        if m.get("private"):
            continue
        mt = m.get("message_type")
        content = (m.get("content") or "").strip()
        if not content or mt == 2:
            continue
        total += 1
        if mt == 0:
            speaker = "LEAD"
            if len(content) >= SUBSTANTIVE_MIN_CHARS:
                substantive += 1
        else:
            stype = ((m.get("sender") or {}).get("type") or "").lower()
            speaker = "AGENTE" if stype in ("user", "agent") else "BOT"
        lines.append(f"{speaker}: {content}")
    return "\n".join(lines), total, substantive


# ── Fuente n8n (fallback) ────────────────────────────────────────────────────

def _msg_role_content(message: Any) -> tuple[Optional[str], str]:
    if not isinstance(message, dict):
        return None, ""
    role = message.get("type") or message.get("role")
    data = message.get("data") if isinstance(message.get("data"), dict) else {}
    if not role:
        role = data.get("type") or data.get("role")
    content = message.get("content")
    if content is None:
        content = data.get("content")
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content if isinstance(b, dict)]
        content = " ".join(p for p in parts if p)
    if not isinstance(content, str):
        content = "" if content is None else str(content)
    return role, content.strip()


def transcript_from_n8n(rows: list[asyncpg.Record]) -> tuple[str, int, int]:
    lines: list[str] = []
    total = substantive = 0
    for r in rows:
        raw = r["message"]
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                continue
        role, content = _msg_role_content(raw)
        if not content:
            continue
        total += 1
        if role in ("human", "user"):
            speaker = "LEAD"
            if len(content) >= SUBSTANTIVE_MIN_CHARS:
                substantive += 1
        elif role in ("ai", "assistant"):
            speaker = "BOT"
        else:
            speaker = (role or "?").upper()
        lines.append(f"{speaker}: {content}")
    return "\n".join(lines), total, substantive


# ── Extraccion ───────────────────────────────────────────────────────────────

def extract_features(client: anthropic.Anthropic, transcript: str) -> dict:
    msg = client.messages.create(
        model=EXTRACT_MODEL,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        tools=[EXTRACT_TOOL],
        tool_choice={"type": "tool", "name": "registrar_lead"},
        messages=[{"role": "user", "content": f"Transcripcion:\n\n{transcript}"}],
    )
    for block in msg.content:
        if block.type == "tool_use" and block.name == "registrar_lead":
            return dict(block.input)
    raise RuntimeError("El modelo no devolvio tool_use registrar_lead")


def normalize(features: dict, *, message_count: int) -> dict:
    out: dict[str, Any] = {col: features.get(col) for col in FEATURE_COLS}
    out["modulos_interes"] = out.get("modulos_interes") or []
    out["pidio_demo"] = bool(out.get("pidio_demo"))
    out["qualified"] = bool(out.get("qualified"))
    out["message_count"] = message_count
    out["canal"] = "whatsapp"
    return out


# ── Upsert ───────────────────────────────────────────────────────────────────

UPSERT_SQL = """
INSERT INTO leads_dataset (
    lead_id, session_id,
    contact_name, email, company_name, tax_id, industry, segmento,
    num_rucs, volumen_comprobantes, modulos_interes, solucion_actual,
    objecion, urgencia, pidio_demo, dolor_principal, tipo_lead,
    ticket_estimado, qualified, message_count, canal, transcript, is_test, updated_at
) VALUES (
    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16,
    $17, $18, $19, $20, $21, $22, $23, NOW()
)
ON CONFLICT (lead_id) DO UPDATE SET
    -- Identidad y juicios del agente: el flujo n8n en vivo es autoritativo.
    session_id           = COALESCE(NULLIF(EXCLUDED.session_id, ''), leads_dataset.session_id),
    contact_name         = COALESCE(NULLIF(EXCLUDED.contact_name, ''), leads_dataset.contact_name),
    email                = COALESCE(NULLIF(EXCLUDED.email, ''), leads_dataset.email),
    company_name         = COALESCE(NULLIF(EXCLUDED.company_name, ''), leads_dataset.company_name),
    tax_id               = COALESCE(NULLIF(EXCLUDED.tax_id, ''), leads_dataset.tax_id),
    industry             = COALESCE(NULLIF(EXCLUDED.industry, ''), leads_dataset.industry),
    tipo_lead            = COALESCE(NULLIF(EXCLUDED.tipo_lead, ''), leads_dataset.tipo_lead),
    ticket_estimado      = COALESCE(NULLIF(EXCLUDED.ticket_estimado, ''), leads_dataset.ticket_estimado),
    qualified            = EXCLUDED.qualified OR COALESCE(leads_dataset.qualified, false),
    -- Features analiticas nuevas: el backfill es la UNICA fuente -> sobrescribe.
    segmento             = EXCLUDED.segmento,
    num_rucs             = EXCLUDED.num_rucs,
    volumen_comprobantes = EXCLUDED.volumen_comprobantes,
    modulos_interes      = EXCLUDED.modulos_interes,
    solucion_actual      = EXCLUDED.solucion_actual,
    objecion             = EXCLUDED.objecion,
    urgencia             = EXCLUDED.urgencia,
    pidio_demo           = EXCLUDED.pidio_demo,
    dolor_principal      = EXCLUDED.dolor_principal,
    message_count        = EXCLUDED.message_count,
    canal                = COALESCE(NULLIF(EXCLUDED.canal, ''), leads_dataset.canal),
    transcript           = EXCLUDED.transcript,
    is_test              = leads_dataset.is_test OR EXCLUDED.is_test,
    updated_at           = NOW()
-- NO se tocan: outcome, outcome_date, outcome_source, captured_at (etiquetas manuales),
-- ni las columnas del flujo n8n (interest, domain, score, meet_url, utm_*, job_title...).
"""


async def upsert_lead(conn: asyncpg.Connection, lead_id: str, session_id: str,
                      data: dict, transcript: str) -> None:
    await conn.execute(
        UPSERT_SQL,
        lead_id, session_id,
        data["contact_name"], data["email"], data["company_name"], data["tax_id"],
        data["industry"], data["segmento"], data["num_rucs"], data["volumen_comprobantes"],
        data["modulos_interes"], data["solucion_actual"], data["objecion"], data["urgencia"],
        data["pidio_demo"], data["dolor_principal"], data["tipo_lead"], data["ticket_estimado"],
        data["qualified"], data["message_count"], data["canal"], transcript,
        lead_id in TEST_LEADS,
    )


# ── Procesamiento comun (independiente de la fuente) ─────────────────────────

async def process_one(args, conn, client, lead_id, transcript, total, substantive, c) -> None:
    session_id = SESSION_PREFIX + lead_id
    if substantive < MIN_SUBSTANTIVE_HUMAN_TURNS:
        print(f"[skip] {lead_id}: {substantive} turnos humanos sustantivos (< {MIN_SUBSTANTIVE_HUMAN_TURNS})")
        c["skip"] += 1
        return
    try:
        data = normalize(extract_features(client, transcript), message_count=total)
    except Exception as e:  # noqa: BLE001
        print(f"[err ] {lead_id}: extraccion fallo -> {e}")
        c["err"] += 1
        return

    tag = " [TEST]" if lead_id in TEST_LEADS else ""
    if args.dry_run:
        print(f"[dry ] {lead_id}{tag}  msgs={total}  ->")
        print("       " + json.dumps(data, ensure_ascii=False, default=str))
    else:
        await upsert_lead(conn, lead_id, session_id, data, transcript)
        mods = ",".join(data["modulos_interes"]) or "-"
        print(f"[ok  ] {lead_id}{tag}  msgs={total}  seg={data['segmento'] or '-'}  "
              f"rucs={data['num_rucs']}  mods={mods}  qual={data['qualified']}")
    c["ok"] += 1


# ── Orquestacion por fuente ──────────────────────────────────────────────────

async def run_chatwoot(args, conn, client, c) -> None:
    if not CHATWOOT_TOKEN:
        raise SystemExit("Falta CHATWOOT_API_TOKEN en el entorno.")
    convs = cw_list_conversations()
    by_phone: dict[str, list[int]] = {}
    for cv in convs:
        if cv.get("inbox_id") != CHATWOOT_INBOX:
            continue
        sender = (cv.get("meta") or {}).get("sender") or {}
        phone = (sender.get("phone_number") or "").replace("+", "").strip()
        if phone:
            by_phone.setdefault(phone, []).append(cv["id"])

    print(f"Contactos (telefonos) en inbox {CHATWOOT_INBOX}: {len(by_phone)}  "
          f"(fuente=chatwoot, modelo={EXTRACT_MODEL}, dry_run={args.dry_run})\n")

    for phone, conv_ids in by_phone.items():
        if args.only_lead and phone != args.only_lead:
            continue
        if args.limit and c["ok"] >= args.limit:
            break
        all_msgs: list[dict] = []
        for cid in conv_ids:
            try:
                all_msgs += cw_messages(cid)
            except Exception as e:  # noqa: BLE001
                print(f"[warn] {phone}: fallo trayendo conv {cid} -> {e}")
        transcript, total, substantive = transcript_from_chatwoot(all_msgs)
        await process_one(args, conn, client, phone, transcript, total, substantive, c)


async def run_n8n(args, conn, client, c) -> None:
    if args.only_lead:
        rows = await conn.fetch(
            "SELECT session_id, id, message FROM n8n_chat_histories "
            "WHERE session_id = $1 ORDER BY id", SESSION_PREFIX + args.only_lead)
    else:
        rows = await conn.fetch(
            "SELECT session_id, id, message FROM n8n_chat_histories ORDER BY session_id, id")
    sessions: dict[str, list] = {}
    for r in rows:
        sessions.setdefault(r["session_id"], []).append(r)

    print(f"Sesiones en n8n_chat_histories: {len(sessions)}  "
          f"(fuente=n8n, modelo={EXTRACT_MODEL}, dry_run={args.dry_run})\n")

    for session_id, srows in sessions.items():
        if args.limit and c["ok"] >= args.limit:
            break
        if not session_id or not session_id.startswith(SESSION_PREFIX):
            print(f"[skip] {session_id!r}: sin prefijo {SESSION_PREFIX}")
            c["skip"] += 1
            continue
        lead_id = session_id[len(SESSION_PREFIX):]
        transcript, total, substantive = transcript_from_n8n(srows)
        await process_one(args, conn, client, lead_id, transcript, total, substantive, c)


async def run(args: argparse.Namespace) -> None:
    conn = await asyncpg.connect(
        host=os.getenv("DB_HOST", "postgres"),
        port=int(os.getenv("DB_PORT", "5432")),
        database=os.getenv("DB_NAME", "n8n"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
    )
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    c = {"ok": 0, "skip": 0, "err": 0}
    try:
        if args.source == "chatwoot":
            await run_chatwoot(args, conn, client, c)
        else:
            await run_n8n(args, conn, client, c)

        if not args.dry_run and not args.only_lead:
            for tl in TEST_LEADS:
                await conn.execute("UPDATE leads_dataset SET is_test = true WHERE lead_id = $1", tl)

        print(f"\nResumen: procesados={c['ok']}  saltados={c['skip']}  errores={c['err']}")
    finally:
        await conn.close()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Backfill de leads_dataset (fuente: Chatwoot o n8n)")
    p.add_argument("--source", choices=["chatwoot", "n8n"], default="chatwoot",
                   help="Fuente de los transcripts (default: chatwoot, la completa).")
    p.add_argument("--dry-run", action="store_true", help="No escribe en DB; imprime lo que extraeria.")
    p.add_argument("--limit", type=int, default=0, help="Procesa como maximo N leads (0 = todos).")
    p.add_argument("--only-lead", type=str, default=None, help="Procesa solo este telefono (lead_id).")
    return p.parse_args()


if __name__ == "__main__":
    asyncio.run(run(parse_args()))
