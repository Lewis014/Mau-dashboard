import os
import json
import asyncio
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Depends, Query, Security
from fastapi.responses import FileResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel

from app import db
from app.catalog import MODULOS, LINK_TRIAL, LINK_DEMO
from app.scoring import score_text

APP_TOKEN = os.getenv("APP_TOKEN", "")
bearer_scheme = HTTPBearer(auto_error=False)

VALID_OUTCOMES = {"nuevo", "en_seguimiento", "demo_agendada", "trial_iniciado", "cliente", "perdido"}
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


def check_auth(creds: Optional[HTTPAuthorizationCredentials] = Security(bearer_scheme)):
    if not creds or not APP_TOKEN or creds.credentials != APP_TOKEN:
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Bearer"},
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.create_pool()
    # Asegura la columna del score ML (idempotente) para que la lista y el scoring la usen.
    await db.get_pool().execute(
        "ALTER TABLE leads_dataset ADD COLUMN IF NOT EXISTS conversion_prob real"
    )
    yield
    await db.close_pool()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)


class OutcomeBody(BaseModel):
    outcome: str


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/")
async def root():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


ORDER_BY = {
    "recientes": "captured_at DESC",
    "score": "conversion_prob DESC NULLS LAST, captured_at DESC",
}


@app.get("/api/stats")
async def stats(_=Depends(check_auth)):
    """KPIs para el dashboard: totales, calificados, clientes, prob promedio y conteo por outcome."""
    pool = db.get_pool()
    row = await pool.fetchrow(
        """
        SELECT
          COUNT(*)                                            AS total,
          COUNT(*) FILTER (WHERE qualified)                   AS calificados,
          COUNT(*) FILTER (WHERE outcome = 'cliente')         AS clientes,
          AVG(conversion_prob)                                AS prob_promedio,
          COUNT(*) FILTER (WHERE conversion_prob IS NOT NULL) AS con_score,
          COUNT(*) FILTER (WHERE transcript IS NOT NULL)      AS con_transcript
        FROM leads_dataset
        WHERE is_test = false
        """
    )
    outcomes = await pool.fetch(
        "SELECT outcome, COUNT(*) AS n FROM leads_dataset WHERE is_test = false GROUP BY outcome"
    )
    return {
        "total": row["total"],
        "calificados": row["calificados"],
        "clientes": row["clientes"],
        "prob_promedio": float(row["prob_promedio"]) if row["prob_promedio"] is not None else None,
        "con_score": row["con_score"],
        "con_transcript": row["con_transcript"],
        "por_outcome": {r["outcome"]: r["n"] for r in outcomes},
    }


@app.get("/api/leads")
async def list_leads(
    outcome: Optional[str] = Query(None),
    qualified: Optional[str] = Query(None),
    has_transcript: Optional[str] = Query(None),
    sort: str = Query("recientes"),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
    _=Depends(check_auth),
):
    pool = db.get_pool()
    conditions: list[str] = []
    args: list = []

    if outcome and outcome in VALID_OUTCOMES:
        conditions.append(f"outcome = ${len(args) + 1}")
        args.append(outcome)
    if qualified in ("true", "false"):
        conditions.append(f"qualified = ${len(args) + 1}")
        args.append(qualified == "true")
    if has_transcript == "true":
        conditions.append("transcript IS NOT NULL")

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    order_by = ORDER_BY.get(sort, ORDER_BY["recientes"])

    total = await pool.fetchval(f"SELECT COUNT(*) FROM leads_dataset {where}", *args)
    rows = await pool.fetch(
        f"SELECT * FROM leads_dataset {where} ORDER BY {order_by} "
        f"LIMIT ${len(args) + 1} OFFSET ${len(args) + 2}",
        *args,
        limit,
        offset,
    )

    items = []
    for r in rows:
        d = db.row_to_dict(r)
        # No mandamos el transcript completo al navegador; solo si existe (para decidir si es puntuable).
        d["has_transcript"] = bool(d.get("transcript"))
        d.pop("transcript", None)
        items.append(d)

    return {"total": total, "items": items}


@app.get("/api/leads/{lead_id}")
async def get_lead(lead_id: str, _=Depends(check_auth)):
    pool = db.get_pool()
    row = await pool.fetchrow("SELECT * FROM leads_dataset WHERE lead_id = $1", lead_id)
    if not row:
        raise HTTPException(status_code=404, detail="Lead not found")
    return db.row_to_dict(row)


@app.patch("/api/leads/{lead_id}/outcome")
async def update_outcome(lead_id: str, body: OutcomeBody, _=Depends(check_auth)):
    if body.outcome not in VALID_OUTCOMES:
        raise HTTPException(status_code=400, detail="Invalid outcome value")

    pool = db.get_pool()
    result = await pool.execute(
        "UPDATE leads_dataset SET outcome=$1, outcome_date=NOW(), outcome_source='manual', updated_at=NOW() "
        "WHERE lead_id=$2",
        body.outcome,
        lead_id,
    )
    if result == "UPDATE 0":
        raise HTTPException(status_code=404, detail="Lead not found")
    return {"ok": True}


def _es_lead_grande(lead: dict) -> bool:
    """Regla de negocio: lead mediano/grande (>10 RUCs, >=1000 comprobantes o pidió demo)."""
    rucs = lead.get("num_rucs") or 0
    vol = lead.get("volumen_comprobantes") or 0
    return bool(lead.get("pidio_demo")) or rucs > 10 or vol >= 1000


def build_brief(lead: dict) -> str:
    """Arma el brief por plantilla + reglas, sin LLM: perfil, necesidad, módulos y siguiente paso."""
    nombre = lead.get("contact_name") or lead.get("wa_display_name") or f"+{lead.get('lead_id', '')}"
    partes: list[str] = []

    perfil = []
    if lead.get("segmento"):
        perfil.append(str(lead["segmento"]))
    if lead.get("num_rucs"):
        perfil.append(f"{lead['num_rucs']} RUCs")
    if lead.get("volumen_comprobantes"):
        perfil.append(f"{lead['volumen_comprobantes']} comprobantes/mes")
    if lead.get("industry"):
        perfil.append(str(lead["industry"]))
    partes.append(f"Perfil: {nombre} — {', '.join(perfil) if perfil else 'sin datos de perfil'}.")

    if lead.get("solucion_actual"):
        partes.append(f"Solución actual: {lead['solucion_actual']}.")
    if lead.get("dolor_principal"):
        partes.append(f"Necesidad: {lead['dolor_principal']}.")
    if lead.get("objecion") and lead["objecion"] != "ninguna":
        partes.append(f"Objeción a manejar: {lead['objecion']}.")

    recs = [f"- {MODULOS[m]['nombre']}: {MODULOS[m]['desc']}"
            for m in (lead.get("modulos_interes") or []) if m in MODULOS]
    if recs:
        partes.append("Módulo(s) recomendado(s):\n" + "\n".join(recs))

    if _es_lead_grande(lead):
        partes.append(f"Siguiente paso: agendar demo de 45 min → {LINK_DEMO}")
    else:
        partes.append(f"Siguiente paso: invitar a la prueba gratuita → {LINK_TRIAL}")

    return "\n\n".join(partes)


@app.post("/api/leads/{lead_id}/brief")
async def generate_brief(lead_id: str, _=Depends(check_auth)):
    pool = db.get_pool()
    row = await pool.fetchrow("SELECT * FROM leads_dataset WHERE lead_id = $1", lead_id)
    if not row:
        raise HTTPException(status_code=404, detail="Lead not found")
    return {"brief": build_brief(db.row_to_dict(row))}


@app.post("/api/leads/{lead_id}/score")
async def score_lead(lead_id: str, _=Depends(check_auth)):
    """Puntua el transcript del lead con el modelo y guarda conversion_prob."""
    pool = db.get_pool()
    row = await pool.fetchrow("SELECT transcript FROM leads_dataset WHERE lead_id = $1", lead_id)
    if not row:
        raise HTTPException(status_code=404, detail="Lead not found")
    if not row["transcript"]:
        raise HTTPException(status_code=400, detail="Lead has no transcript to score")
    try:
        prob = await asyncio.to_thread(score_text, row["transcript"])
    except FileNotFoundError:
        raise HTTPException(status_code=503, detail="Model file not available (MODEL_PATH)")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Scoring failed: {e}")
    await pool.execute(
        "UPDATE leads_dataset SET conversion_prob=$1, updated_at=NOW() WHERE lead_id=$2",
        prob, lead_id,
    )
    return {"lead_id": lead_id, "conversion_prob": prob}
