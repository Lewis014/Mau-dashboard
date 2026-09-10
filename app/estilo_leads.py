"""Rellena estilo_consulta y dominio_tecnico en los leads que ya estaban capturados.

El extractor (app/backfill.py) saca estos dos campos desde el 10/09/2026, pero solo para las
conversaciones que vuelve a leer. Este batch los rellena hacia atras SIN tocar ninguna otra
columna: es mucho mas barato que re-extraerlo todo (una llamada corta por lead en vez de la
extraccion completa) y no arriesga las features que ya estan bien.

Usa las MISMAS reglas y descripciones que el extractor (ESTILO_REGLAS / ESTILO_PROPS de
app/backfill.py), asi los leads viejos y los nuevos quedan clasificados con el mismo criterio.

Uso (dentro del contenedor del dashboard):
    docker compose exec dashboard python -m app.estilo_leads             # solo los que faltan
    docker compose exec dashboard python -m app.estilo_leads --all       # reclasifica todos
    docker compose exec dashboard python -m app.estilo_leads --only-lead 51999999999
    docker compose exec dashboard python -m app.estilo_leads --dry-run   # no escribe
    docker compose exec dashboard python -m app.estilo_leads --limit 30  # una prueba corta

Env: DB_* (Postgres compartido con n8n), ANTHROPIC_API_KEY, EXTRACT_MODEL.
"""
import argparse
import asyncio
import os
import re
from collections import Counter

import anthropic
import asyncpg

from app.backfill import ESTILO_PROPS, ESTILO_REGLAS

MODEL = os.getenv("EXTRACT_MODEL", "claude-sonnet-4-6")
# La plantilla del anuncio de Meta la escribe Meta, no el lead: 75% de las conversaciones
# empiezan con ella y clasificar por ella seria clasificar por el anuncio.
PLANTILLA = re.compile(
    r"^¡?hola!?\s*\S*\s*soy (un )?(estudio contable|contador independiente|empresa)", re.I)
# Turnos del bot recortados: dan contexto (que se le pregunto al lead) sin gastar tokens.
BOT_MAX = 90
MAX_LINEAS = 20

SYSTEM_PROMPT = (
    "Eres un analista de ventas de Contatech. Vas a leer una conversacion de WhatsApp entre "
    "un chatbot (BOT) y un posible cliente (LEAD) de un software contable en Peru, y a "
    "clasificar UNICAMENTE la forma en que el LEAD consulta. Reglas:\n"
    "- Juzga solo los turnos del LEAD; los del BOT son contexto.\n"
    + ESTILO_REGLAS + "\n"
    "- No inventes. Ante la duda, null: es preferible no clasificar a clasificar mal."
)

TOOL = {
    "name": "clasificar_estilo",
    "description": "Registra como consulta el lead.",
    "input_schema": {
        "type": "object",
        "properties": dict(ESTILO_PROPS, evidencia={
            "type": "string",
            "description": "Frase LITERAL del lead que lo justifica (<= 100 caracteres), o vacio.",
        }),
        "required": ["estilo_consulta", "dominio_tecnico", "evidencia"],
    },
}


def dialogo(transcript: str) -> tuple[str, int]:
    """Texto que se le manda al modelo y cuantos turnos libres escribio el lead.

    Devolver el numero de turnos libres es lo que permite no gastar una llamada en quien no
    escribio nada por su cuenta: sin texto libre la respuesta seria null de todas formas.
    """
    lineas: list[str] = []
    libres = 0
    for linea in (transcript or "").split("\n"):
        if linea.startswith("LEAD:"):
            texto = linea[5:].strip()
            if not texto or PLANTILLA.match(texto):
                continue
            libres += 1
            lineas.append("LEAD: " + texto)
        elif linea.startswith("BOT:"):
            lineas.append("BOT: " + linea[4:].strip()[:BOT_MAX])
    return "\n".join(lineas[:MAX_LINEAS]), libres


def clasificar(client: anthropic.Anthropic, texto: str) -> dict:
    msg = client.messages.create(
        model=MODEL,
        max_tokens=512,
        system=SYSTEM_PROMPT,
        tools=[TOOL],
        tool_choice={"type": "tool", "name": "clasificar_estilo"},
        messages=[{"role": "user", "content": texto}],
    )
    for bloque in msg.content:
        if bloque.type == "tool_use" and bloque.name == "clasificar_estilo":
            return dict(bloque.input)
    raise RuntimeError("El modelo no devolvio tool_use clasificar_estilo")


async def run(args: argparse.Namespace) -> dict:
    conn = await asyncpg.connect(
        host=os.getenv("DB_HOST", "postgres"),
        port=int(os.getenv("DB_PORT", "5432")),
        database=os.getenv("DB_NAME", "n8n"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
    )
    try:
        await conn.execute(
            "ALTER TABLE leads_dataset "
            "ADD COLUMN IF NOT EXISTS estilo_consulta text, "
            "ADD COLUMN IF NOT EXISTS dominio_tecnico text"
        )
        where = "is_test = false AND transcript IS NOT NULL AND length(transcript) > 0"
        params: list = []
        if args.only_lead:
            params.append(args.only_lead)
            where += f" AND lead_id = ${len(params)}"
        elif not args.all:
            where += " AND estilo_consulta IS NULL"
        limite = f" LIMIT {int(args.limit)}" if args.limit else ""
        filas = await conn.fetch(
            f"SELECT lead_id, transcript FROM leads_dataset WHERE {where} "
            f"ORDER BY conversion_prob DESC NULLS LAST, lead_id{limite}",
            *params,
        )
        print(f"Leads a clasificar: {len(filas)}  (dry_run={args.dry_run}, all={args.all})\n")

        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        cuenta = Counter()
        for f in filas:
            texto, libres = dialogo(f["transcript"])
            if libres == 0:
                # Sin una sola frase propia no hay nada que juzgar: se marca 'sin_texto' para
                # no volver a intentarlo en cada pasada y no se gasta la llamada.
                cuenta["sin texto libre"] += 1
                if not args.dry_run:
                    await conn.execute(
                        "UPDATE leads_dataset SET estilo_consulta='sin_texto', updated_at=NOW() "
                        "WHERE lead_id=$1", f["lead_id"])
                continue
            try:
                r = clasificar(client, texto)
            except Exception as e:  # noqa: BLE001 — un lead raro no puede parar la pasada
                print(f"[err ] {f['lead_id']}: {e}")
                cuenta["errores"] += 1
                continue

            estilo = r.get("estilo_consulta") or "sin_texto"
            tecnico = r.get("dominio_tecnico") if r.get("dominio_tecnico") == "alto" else None
            cuenta[f"estilo={estilo}"] += 1
            if tecnico:
                cuenta["tecnico=alto"] += 1
            marca = "dry " if args.dry_run else "ok  "
            print(f"[{marca}] {f['lead_id']}  estilo={estilo:<11} tecnico={tecnico or '-':<5} "
                  f"«{(r.get('evidencia') or '')[:70]}»")
            if not args.dry_run:
                await conn.execute(
                    "UPDATE leads_dataset SET estilo_consulta=$1, dominio_tecnico=$2, "
                    "updated_at=NOW() WHERE lead_id=$3",
                    estilo, tecnico, f["lead_id"])

        print("\nResumen: " + " · ".join(f"{k}={v}" for k, v in sorted(cuenta.items())))
        return dict(cuenta)
    finally:
        await conn.close()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Clasifica como consulta el lead (estilo y dominio tecnico).")
    p.add_argument("--all", action="store_true", help="Reclasifica todos, no solo los que faltan.")
    p.add_argument("--only-lead", type=str, default=None, help="Solo este lead_id.")
    p.add_argument("--limit", type=int, default=0, help="Como mucho N leads (0 = sin limite).")
    p.add_argument("--dry-run", action="store_true", help="No escribe en la base; solo imprime.")
    return p.parse_args()


if __name__ == "__main__":
    asyncio.run(run(parse_args()))
