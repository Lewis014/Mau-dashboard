"""
Backfill de leads_dataset desde los transcripts crudos de n8n_chat_histories.

Re-extrae las features estructuradas conversacion por conversacion usando
Claude (tool-use con JSON forzado), en vez de confiar en el JSON viejo de
handoff a CRM (que tiraba las senales crudas de tamano). Decisiones del doc
de tesis que este script respeta:

  - Re-extraccion desde texto crudo, no del JSON viejo (los 97 son heterogeneos).
  - Regla anti-alucinacion: num_rucs / volumen_comprobantes solo si el lead dio
    un numero explicito. Nunca inferir.
  - Filtra sesiones con < 2 turnos humanos sustantivos (basura tipo "Hola"/"Hola").
  - Preserva en el upsert las columnas que se llenan a mano desde el dashboard
    (outcome, outcome_date, outcome_source, captured_at, is_test).
  - Marca el lead de prueba conocido (51934226756) como is_test.

Uso (dentro del contenedor del dashboard, que ya tiene deps/env/red):
    docker compose exec dashboard python -m app.backfill
    docker compose exec dashboard python -m app.backfill --dry-run --limit 5
    docker compose exec dashboard python -m app.backfill --only-session sales-bot-519...

Env relevante:
    DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD  -> Postgres compartido con n8n
    ANTHROPIC_API_KEY                            -> extraccion
    EXTRACT_MODEL (default claude-sonnet-4-6)    -> modelo del extractor
"""

import argparse
import asyncio
import json
import os
from typing import Any, Optional

import asyncpg
import anthropic

# ── Constantes de negocio ────────────────────────────────────────────────────

SESSION_PREFIX = "sales-bot-"
TEST_LEADS = {"51934226756"}          # primer lead capturado (Douglas / correo de Piero)
MIN_SUBSTANTIVE_HUMAN_TURNS = 2       # filtro anti-basura
SUBSTANTIVE_MIN_CHARS = 15            # un turno humano "cuenta" si supera esto

EXTRACT_MODEL = os.getenv("EXTRACT_MODEL", "claude-sonnet-4-6")

SYSTEM_PROMPT = (
    "Eres un analista de datos de Contatech. MAU es el asistente contable virtual "
    "B2B (Peru/LATAM) que vende 3 modulos: COMUNICA (notificaciones SUNAT multi-RUC), "
    "VALIDA (validacion masiva de comprobantes) y PROCESA (registro contable automatico "
    "con integracion a CONCAR/EXIGE/Contasis/Starsoft/Odoo).\n\n"
    "Recibiras la transcripcion de una conversacion real de WhatsApp entre un lead y el "
    "bot de ventas. Extrae UNICAMENTE las features estructuradas que esten respaldadas "
    "por el texto. Reglas estrictas:\n"
    "- NUNCA inventes ni infieras numeros. 'num_rucs' y 'volumen_comprobantes' solo se "
    "llenan si el lead dio una cifra explicita; si no, dejalos en null.\n"
    "- Usa unicamente los valores permitidos en cada enum; si no aplica o no se sabe, deja null.\n"
    "- 'dolor_principal' e 'industry' son texto libre corto (<= 8 palabras), solo si el lead lo expreso.\n"
    "- 'pidio_demo' es true solo si el lead pidio explicitamente una demo/reunion/llamada.\n"
    "- 'qualified' es un heuristico: true si el lead mostro intencion real (dio datos de "
    "contacto, pidio precio/demo, describio su operacion). No es el resultado de venta."
)

# Tool-use: fuerza salida JSON con enums cerrados.
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
            "ticket_estimado": {"type": ["string", "null"], "description": "Estimacion de ticket si se puede inferir del contexto, ej. '< S/300/mes'."},
            "qualified": {"type": "boolean"},
        },
        "required": ["modulos_interes", "pidio_demo", "qualified"],
    },
}

# Columnas que el extractor llena (orden estable para el INSERT).
FEATURE_COLS = [
    "contact_name", "email", "company_name", "tax_id", "industry", "segmento",
    "num_rucs", "volumen_comprobantes", "modulos_interes", "solucion_actual",
    "objecion", "urgencia", "pidio_demo", "dolor_principal", "tipo_lead",
    "ticket_estimado", "qualified",
]


# ── Parsing de transcripts ───────────────────────────────────────────────────

def _msg_role_content(message: Any) -> tuple[Optional[str], str]:
    """Normaliza un registro `message` jsonb a (role, content).

    Soporta tanto el formato plano {"type","content"} como el anidado de
    LangChain {"type","data":{"content":...}}. content puede venir como lista
    de bloques.
    """
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


def build_transcript(rows: list[asyncpg.Record]) -> tuple[str, int, int]:
    """Devuelve (texto_transcript, total_messages, turnos_humanos_sustantivos)."""
    lines: list[str] = []
    total = 0
    substantive_human = 0
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
                substantive_human += 1
        elif role in ("ai", "assistant"):
            speaker = "BOT"
        else:
            speaker = (role or "?").upper()
        lines.append(f"{speaker}: {content}")
    return "\n".join(lines), total, substantive_human


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
    """Rellena defaults y normaliza tipos para el upsert."""
    out: dict[str, Any] = {}
    for col in FEATURE_COLS:
        out[col] = features.get(col)
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
    ticket_estimado, qualified, message_count, canal, is_test, updated_at
) VALUES (
    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16,
    $17, $18, $19, $20, $21, $22, NOW()
)
ON CONFLICT (lead_id) DO UPDATE SET
    session_id           = EXCLUDED.session_id,
    contact_name         = EXCLUDED.contact_name,
    email                = EXCLUDED.email,
    company_name         = EXCLUDED.company_name,
    tax_id               = EXCLUDED.tax_id,
    industry             = EXCLUDED.industry,
    segmento             = EXCLUDED.segmento,
    num_rucs             = EXCLUDED.num_rucs,
    volumen_comprobantes = EXCLUDED.volumen_comprobantes,
    modulos_interes      = EXCLUDED.modulos_interes,
    solucion_actual      = EXCLUDED.solucion_actual,
    objecion             = EXCLUDED.objecion,
    urgencia             = EXCLUDED.urgencia,
    pidio_demo           = EXCLUDED.pidio_demo,
    dolor_principal      = EXCLUDED.dolor_principal,
    tipo_lead            = EXCLUDED.tipo_lead,
    ticket_estimado      = EXCLUDED.ticket_estimado,
    qualified            = EXCLUDED.qualified,
    message_count        = EXCLUDED.message_count,
    canal                = EXCLUDED.canal,
    is_test              = leads_dataset.is_test OR EXCLUDED.is_test,
    updated_at           = NOW()
-- NO se tocan: outcome, outcome_date, outcome_source, captured_at (etiquetas manuales)
"""


async def upsert_lead(conn: asyncpg.Connection, lead_id: str, session_id: str, data: dict) -> None:
    await conn.execute(
        UPSERT_SQL,
        lead_id, session_id,
        data["contact_name"], data["email"], data["company_name"], data["tax_id"],
        data["industry"], data["segmento"], data["num_rucs"], data["volumen_comprobantes"],
        data["modulos_interes"], data["solucion_actual"], data["objecion"], data["urgencia"],
        data["pidio_demo"], data["dolor_principal"], data["tipo_lead"], data["ticket_estimado"],
        data["qualified"], data["message_count"], data["canal"],
        lead_id in TEST_LEADS,
    )


# ── Orquestacion ─────────────────────────────────────────────────────────────

async def fetch_sessions(conn: asyncpg.Connection, only_session: Optional[str]) -> dict[str, list]:
    if only_session:
        rows = await conn.fetch(
            "SELECT session_id, id, message FROM n8n_chat_histories "
            "WHERE session_id = $1 ORDER BY id",
            only_session,
        )
    else:
        rows = await conn.fetch(
            "SELECT session_id, id, message FROM n8n_chat_histories ORDER BY session_id, id"
        )
    sessions: dict[str, list] = {}
    for r in rows:
        sessions.setdefault(r["session_id"], []).append(r)
    return sessions


async def run(args: argparse.Namespace) -> None:
    conn = await asyncpg.connect(
        host=os.getenv("DB_HOST", "postgres"),
        port=int(os.getenv("DB_PORT", "5432")),
        database=os.getenv("DB_NAME", "n8n"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
    )
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    try:
        sessions = await fetch_sessions(conn, args.only_session)
        print(f"Sesiones encontradas: {len(sessions)}  (modelo={EXTRACT_MODEL}, dry_run={args.dry_run})\n")

        processed = skipped = errored = 0
        for i, (session_id, rows) in enumerate(sessions.items(), 1):
            if args.limit and processed >= args.limit:
                break
            if not session_id or not session_id.startswith(SESSION_PREFIX):
                print(f"[skip] {session_id!r}: sin prefijo {SESSION_PREFIX}")
                skipped += 1
                continue

            lead_id = session_id[len(SESSION_PREFIX):]
            transcript, total, substantive = build_transcript(rows)

            if substantive < MIN_SUBSTANTIVE_HUMAN_TURNS:
                print(f"[skip] {lead_id}: {substantive} turnos humanos sustantivos (< {MIN_SUBSTANTIVE_HUMAN_TURNS})")
                skipped += 1
                continue

            try:
                features = extract_features(client, transcript)
                data = normalize(features, message_count=total)
            except Exception as e:  # noqa: BLE001
                print(f"[err ] {lead_id}: extraccion fallo -> {e}")
                errored += 1
                continue

            tag = " [TEST]" if lead_id in TEST_LEADS else ""
            if args.dry_run:
                print(f"[dry ] {lead_id}{tag}  msgs={total}  ->")
                print("       " + json.dumps(data, ensure_ascii=False, default=str))
            else:
                await upsert_lead(conn, lead_id, session_id, data)
                mods = ",".join(data["modulos_interes"]) or "-"
                print(f"[ok  ] {lead_id}{tag}  msgs={total}  seg={data['segmento'] or '-'}  "
                      f"rucs={data['num_rucs']}  mods={mods}  qual={data['qualified']}")
            processed += 1

        # Asegura que los leads de prueba queden marcados aunque ya existieran.
        if not args.dry_run and args.only_session is None:
            for tl in TEST_LEADS:
                await conn.execute("UPDATE leads_dataset SET is_test = true WHERE lead_id = $1", tl)

        print(f"\nResumen: procesados={processed}  saltados={skipped}  errores={errored}")
    finally:
        await conn.close()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Backfill de leads_dataset desde n8n_chat_histories")
    p.add_argument("--dry-run", action="store_true", help="No escribe en DB; imprime lo que extraeria.")
    p.add_argument("--limit", type=int, default=0, help="Procesa como maximo N sesiones (0 = todas).")
    p.add_argument("--only-session", type=str, default=None, help="Procesa solo este session_id.")
    return p.parse_args()


if __name__ == "__main__":
    asyncio.run(run(parse_args()))
