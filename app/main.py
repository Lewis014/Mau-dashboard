import os
import json
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Depends, Query, Security
from fastapi.responses import FileResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
import anthropic

from app import db
from app.catalog import CATALOG

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


@app.get("/api/leads")
async def list_leads(
    outcome: Optional[str] = Query(None),
    qualified: Optional[str] = Query(None),
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

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    total = await pool.fetchval(f"SELECT COUNT(*) FROM leads_dataset {where}", *args)
    rows = await pool.fetch(
        f"SELECT * FROM leads_dataset {where} ORDER BY captured_at DESC "
        f"LIMIT ${len(args) + 1} OFFSET ${len(args) + 2}",
        *args,
        limit,
        offset,
    )

    return {"total": total, "items": [db.row_to_dict(r) for r in rows]}


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


@app.post("/api/leads/{lead_id}/brief")
async def generate_brief(lead_id: str, _=Depends(check_auth)):
    pool = db.get_pool()
    row = await pool.fetchrow("SELECT * FROM leads_dataset WHERE lead_id = $1", lead_id)
    if not row:
        raise HTTPException(status_code=404, detail="Lead not found")

    lead = db.row_to_dict(row)
    lead_clean = {k: v for k, v in lead.items() if v not in (None, "", [])}

    try:
        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            messages=[
                {
                    "role": "user",
                    "content": (
                        "Eres un asistente de ventas de Contatech. Genera un brief comercial conciso "
                        "(máximo 200 palabras) para preparar el seguimiento de este lead. "
                        "Incluye: perfil del lead, necesidad identificada, módulo(s) MAU recomendado(s), "
                        "y el siguiente paso concreto de acción. "
                        "Usa solo los datos disponibles, no inventes información.\n\n"
                        f"Datos del lead:\n{json.dumps(lead_clean, ensure_ascii=False, indent=2)}\n\n"
                        f"Catálogo MAU:\n{CATALOG}\n\n"
                        "Genera el brief directamente, sin introducción ni título."
                    ),
                }
            ],
        )
        return {"brief": msg.content[0].text}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error generando brief: {str(e)}")
