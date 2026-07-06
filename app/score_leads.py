"""Calcula conversion_prob para los leads con transcript usando el modelo entrenado.

Crea la columna leads_dataset.conversion_prob si no existe y la llena puntuando el
transcript de cada lead con el modelo portable (ver app/scoring.py).

Uso (dentro del contenedor del dashboard):
    docker compose exec dashboard python -m app.score_leads              # solo los que faltan (conversion_prob NULL)
    docker compose exec dashboard python -m app.score_leads --all        # recalcula TODOS
    docker compose exec dashboard python -m app.score_leads --only-lead 51999999999
    docker compose exec dashboard python -m app.score_leads --dry-run    # no escribe, solo imprime

Env: DB_* (Postgres compartido con n8n), OPENAI_API_KEY, MODEL_PATH, EMBED_MODEL.

NOTA: el modelo esta entrenado sobre el proxy (ingles) y aun NO validado sobre los
leads reales en espanol (bloqueado por el etiquetado de outcomes). El score que
escribe este batch es utilizable, pero su calidad se confirma con la validacion
(seccion 7 del notebook) una vez existan las etiquetas.
"""
import argparse
import asyncio
import os

import asyncpg

from app.scoring import score_text

ADD_COLUMN_SQL = "ALTER TABLE leads_dataset ADD COLUMN IF NOT EXISTS conversion_prob real"


async def run(args: argparse.Namespace) -> None:
    conn = await asyncpg.connect(
        host=os.getenv("DB_HOST", "postgres"),
        port=int(os.getenv("DB_PORT", "5432")),
        database=os.getenv("DB_NAME", "n8n"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
    )
    try:
        await conn.execute(ADD_COLUMN_SQL)

        where = "transcript IS NOT NULL AND length(transcript) > 0"
        params: list = []
        if args.only_lead:
            params.append(args.only_lead)
            where += f" AND lead_id = ${len(params)}"
        elif not args.all:
            where += " AND conversion_prob IS NULL"

        rows = await conn.fetch(
            f"SELECT lead_id, transcript FROM leads_dataset WHERE {where} ORDER BY lead_id",
            *params,
        )
        print(f"Leads a puntuar: {len(rows)}  (dry_run={args.dry_run}, all={args.all})\n")

        ok = err = 0
        for r in rows:
            try:
                p = await asyncio.to_thread(score_text, r["transcript"])
            except Exception as e:  # noqa: BLE001
                print(f"[err ] {r['lead_id']}: {e}")
                err += 1
                continue
            if p is None:
                continue
            if args.dry_run:
                print(f"[dry ] {r['lead_id']}  P(conv)={p:.3f}")
            else:
                await conn.execute(
                    "UPDATE leads_dataset SET conversion_prob=$1, updated_at=NOW() WHERE lead_id=$2",
                    p, r["lead_id"],
                )
                print(f"[ok  ] {r['lead_id']}  P(conv)={p:.3f}")
            ok += 1

        print(f"\nResumen: puntuados={ok}  errores={err}")
    finally:
        await conn.close()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Calcula conversion_prob para los leads con transcript.")
    p.add_argument("--all", action="store_true", help="Recalcula todos (por defecto solo los que tienen conversion_prob NULL).")
    p.add_argument("--only-lead", type=str, default=None, help="Puntua solo este lead_id.")
    p.add_argument("--dry-run", action="store_true", help="No escribe en DB; solo imprime.")
    return p.parse_args()


if __name__ == "__main__":
    asyncio.run(run(parse_args()))
