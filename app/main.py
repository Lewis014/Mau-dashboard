import os
import json
import asyncio
import base64
import hashlib
import hmac
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, Depends, Query, Security
from fastapi.responses import FileResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel

from app import db
from app.catalog import MODULOS, LINK_TRIAL, LINK_DEMO
from app.scoring import score_text

APP_TOKEN = os.getenv("APP_TOKEN", "")
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASS", "")
SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", str(12 * 3600)))
_SECRET = (os.getenv("SECRET_KEY") or APP_TOKEN or ADMIN_PASS).encode()
bearer_scheme = HTTPBearer(auto_error=False)

# Etiquetas de lead, agrupadas por categoria. Un lead puede llevar varias a la vez y de
# grupos distintos ("demo agendada" + "diego" + "meta"): por eso no son un enum sino un
# text[]. Este dict es la unica fuente de verdad del backend; el orden importa porque es
# el que se usa al normalizar la lista que manda la UI.
TAG_GROUPS: dict[str, list[str]] = {
    "estado": [
        "lead_interesado", "llamada", "llamada_no_responde", "insistir",
        "demo_agendada", "demo_realizada", "cotizacion", "free_trial",
        "cliente", "perdido",
    ],
    "responsable": ["alyssa", "diego", "jhon"],
    "canal": ["meta", "organico", "tiktok"],
}
VALID_TAGS = {t for tags in TAG_GROUPS.values() for t in tags}

# Embudo, del final hacia atras: la primera etiqueta que aparezca aqui es el "estado
# principal" que se guarda en outcome. 'perdido' va segundo a proposito — un lead con
# free_trial + perdido se fugo, y el modelo de la tesis tiene que contarlo como negativo.
OUTCOME_PRIORITY = (
    "cliente", "perdido", "free_trial", "cotizacion", "demo_realizada",
    "demo_agendada", "llamada", "insistir", "lead_interesado", "llamada_no_responde",
)

# Equivalencias de las etiquetas viejas (columna outcome de un solo valor) a las nuevas.
# Las que no aparecen aqui conservan su slug: demo_agendada, cliente y perdido.
OUTCOME_LEGACY = {"trial_iniciado": "free_trial", "en_seguimiento": "insistir"}

# Estado comercial que trae el sync desde mau-web, y la etiqueta que le corresponde.
# El sync manda SOLO sobre estas tres etiquetas (PLAN_TAGS) y solo en los leads que
# cruzaron con una cuenta: el resto es trabajo del vendedor y no se toca.
#
# 'ex_cliente' lleva cliente y no perdido a proposito: convirtio, y eso es un hecho que el
# modelo de la tesis cuenta como positivo. Que se haya ido se ve en la columna Plan.
PLAN_ESTADO_TAG = {
    "cliente_activo": "cliente",
    "ex_cliente": "cliente",
    "pago_en_verificacion": None,   # pago subido sin aprobar: todavia no es conversion
    "trial_activo": "free_trial",
    "trial_vencido": "perdido",     # probo y no compro: el negativo que le faltaba al modelo
    "sin_cuenta": None,             # nunca se registro en la web; no se toca su etiquetado
}
PLAN_TAGS = {t for t in PLAN_ESTADO_TAG.values() if t}

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
# Zona horaria del negocio: captured_at es timestamptz y el contenedor corre en UTC,
# asi que los filtros por dia se evaluan aqui para que "12 de agosto" sea el dia peruano.
LOCAL_TZ = os.getenv("LOCAL_TZ", "America/Lima")


def primary_outcome(tags: list[str]) -> str:
    """Estado principal derivado de las etiquetas (el mas avanzado del embudo).

    Se guarda en la columna outcome, que deja de editarse a mano: existe para que el KPI
    de clientes y el pipeline de la tesis sigan teniendo un valor unico por lead.
    Sin ninguna etiqueta de estado el lead esta 'nuevo' (pendiente de etiquetar).
    """
    return next((o for o in OUTCOME_PRIORITY if o in tags), "nuevo")


def normalize_tags(tags: list[str]) -> list[str]:
    """Valida contra VALID_TAGS y devuelve la lista sin duplicados, en orden de TAG_GROUPS."""
    invalidas = sorted(set(tags) - VALID_TAGS)
    if invalidas:
        raise HTTPException(status_code=400, detail=f"Etiquetas inválidas: {', '.join(invalidas)}")
    elegidas = set(tags)
    return [t for grupo in TAG_GROUPS.values() for t in grupo if t in elegidas]


def _sign(payload: str) -> str:
    return hmac.new(_SECRET, payload.encode(), hashlib.sha256).hexdigest()


def make_session_token(user: str) -> str:
    payload = f"{user}:{int(time.time()) + SESSION_TTL_SECONDS}"
    return base64.urlsafe_b64encode(f"{payload}:{_sign(payload)}".encode()).decode()


def valid_session_token(tok: str) -> bool:
    try:
        payload, sig = base64.urlsafe_b64decode(tok.encode()).decode().rsplit(":", 1)
        _user, exp = payload.rsplit(":", 1)
        return hmac.compare_digest(sig, _sign(payload)) and int(exp) > time.time()
    except Exception:  # noqa: BLE001 — token malformado = no autorizado
        return False


def check_auth(creds: Optional[HTTPAuthorizationCredentials] = Security(bearer_scheme)):
    tok = creds.credentials if creds else ""
    # Acepta el APP_TOKEN legado (scripts/batch) o un token de sesion del login.
    if tok and APP_TOKEN and hmac.compare_digest(tok, APP_TOKEN):
        return
    if tok and _SECRET and valid_session_token(tok):
        return
    raise HTTPException(
        status_code=401,
        detail="Unauthorized",
        headers={"WWW-Authenticate": "Bearer"},
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.create_pool()
    pool = db.get_pool()
    # Asegura la columna del score ML (idempotente) para que la lista y el scoring la usen.
    await pool.execute("ALTER TABLE leads_dataset ADD COLUMN IF NOT EXISTS conversion_prob real")
    # Etiquetas multiples. El GIN es para el operador @> del filtro de la tabla de leads.
    await pool.execute(
        "ALTER TABLE leads_dataset ADD COLUMN IF NOT EXISTS outcome_tags text[] NOT NULL DEFAULT '{}'"
    )
    await pool.execute(
        "CREATE INDEX IF NOT EXISTS idx_leads_outcome_tags ON leads_dataset USING GIN (outcome_tags)"
    )
    # Estado comercial que trae el sync desde mau-web (ver app/sync_planes.py).
    await pool.execute(
        """
        ALTER TABLE leads_dataset
          ADD COLUMN IF NOT EXISTS plan_estado  text,
          ADD COLUMN IF NOT EXISTS plan_nombre  text,
          ADD COLUMN IF NOT EXISTS plan_inicia  timestamptz,
          ADD COLUMN IF NOT EXISTS plan_expira  timestamptz,
          ADD COLUMN IF NOT EXISTS plan_pagos   integer,
          ADD COLUMN IF NOT EXISTS plan_user_id integer,
          ADD COLUMN IF NOT EXISTS plan_match   text,
          ADD COLUMN IF NOT EXISTS plan_sync_at timestamptz
        """
    )
    # Migra el outcome unico de cada lead a su etiqueta equivalente. Es idempotente: solo
    # toca filas todavia sin etiquetar, y vaciar las etiquetas desde el dashboard devuelve
    # outcome a 'nuevo', asi que un borrado deliberado no revive en el siguiente arranque.
    # El ->> busca el outcome viejo en OUTCOME_LEGACY (pasado como jsonb) y cae al propio
    # valor cuando no esta, en vez de repetir aqui el mapeo que ya vive en Python.
    await pool.execute(
        """
        UPDATE leads_dataset
           SET outcome_tags = ARRAY[COALESCE($1::jsonb->>outcome, outcome)]
         WHERE cardinality(outcome_tags) = 0
           AND outcome = ANY($2::text[])
        """,
        json.dumps(OUTCOME_LEGACY),
        list(OUTCOME_LEGACY) + ["demo_agendada", "cliente", "perdido"],
    )
    yield
    await db.close_pool()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)


class OutcomeBody(BaseModel):
    # El dashboard manda siempre el conjunto completo (reemplazo, no incremento).
    # outcome es el formato legado de un solo valor; se acepta para no romper a n8n/scripts.
    tags: Optional[list[str]] = None
    outcome: Optional[str] = None


class LoginBody(BaseModel):
    username: str
    password: str


@app.post("/api/login")
async def login(body: LoginBody):
    if not ADMIN_PASS:
        raise HTTPException(status_code=503, detail="Login no configurado (falta ADMIN_PASS)")
    ok = hmac.compare_digest(body.username, ADMIN_USER) and hmac.compare_digest(body.password, ADMIN_PASS)
    if not ok:
        raise HTTPException(status_code=401, detail="Credenciales inválidas")
    return {"token": make_session_token(body.username), "expires_in": SESSION_TTL_SECONDS}


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


def _rango_captura(desde: Optional[str], hasta: Optional[str],
                   conditions: list[str], args: list) -> None:
    """Agrega el filtro por fecha de captura a conditions/args (los modifica in-place).

    Fechas en formato YYYY-MM-DD; 'hasta' es inclusivo (el dia completo). La comparacion
    se hace sobre la fecha local (LOCAL_TZ) y no sobre el timestamptz crudo: un lead de
    las 21:00 de Lima es 02:00 UTC del dia siguiente, y sin convertir caeria en el dia
    equivocado. El coste de la expresion funcional es irrelevante a esta escala.
    """
    if not desde and not hasta:
        return
    args.append(LOCAL_TZ)
    tz = f"${len(args)}::text"  # el cast evita la ambiguedad text/interval de AT TIME ZONE
    for raw, op in ((desde, ">="), (hasta, "<=")):
        if not raw:
            continue
        try:
            dia = datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Fecha inválida: {raw!r} (se espera YYYY-MM-DD)")
        args.append(dia)
        conditions.append(f"(captured_at AT TIME ZONE {tz})::date {op} ${len(args)}")


@app.get("/api/stats")
async def stats(
    desde: Optional[str] = Query(None),
    hasta: Optional[str] = Query(None),
    _=Depends(check_auth),
):
    """KPIs para el dashboard: totales, calificados, clientes, prob promedio y conteo por outcome.

    desde/hasta (YYYY-MM-DD, ambos inclusivos) acotan por fecha de captura.
    """
    pool = db.get_pool()
    conditions = ["is_test = false"]
    args: list = []
    _rango_captura(desde, hasta, conditions, args)
    where = "WHERE " + " AND ".join(conditions)

    row = await pool.fetchrow(
        f"""
        SELECT
          COUNT(*)                                            AS total,
          COUNT(*) FILTER (WHERE qualified)                   AS calificados,
          COUNT(*) FILTER (WHERE outcome_tags @> ARRAY['cliente'])  AS clientes,
          COUNT(*) FILTER (WHERE cardinality(outcome_tags) = 0)     AS sin_etiquetas,
          AVG(conversion_prob)                                AS prob_promedio,
          COUNT(*) FILTER (WHERE conversion_prob IS NOT NULL) AS con_score,
          COUNT(*) FILTER (WHERE transcript IS NOT NULL)      AS con_transcript
        FROM leads_dataset
        {where}
        """,
        *args,
    )
    # unnest descarta las filas con array vacio: eso es correcto para contar etiquetas
    # (el pendiente de etiquetar se cuenta aparte, en sin_etiquetas).
    por_tag = await pool.fetch(
        f"SELECT t AS tag, COUNT(*) AS n FROM leads_dataset, unnest(outcome_tags) AS t {where} GROUP BY t",
        *args,
    )
    planes = await pool.fetch(
        f"SELECT plan_estado, COUNT(*) AS n FROM leads_dataset {where} "
        "AND plan_estado IS NOT NULL GROUP BY plan_estado", *args
    )
    outcomes = await pool.fetch(
        f"SELECT outcome, COUNT(*) AS n FROM leads_dataset {where} GROUP BY outcome", *args
    )
    # Sin el filtro de fecha: alimenta el selector de años del dashboard, que debe
    # ofrecer todo el historico aunque el rango activo sea de un solo mes.
    primer = await pool.fetchval(
        "SELECT MIN(captured_at) FROM leads_dataset WHERE is_test = false"
    )
    return {
        "total": row["total"],
        "calificados": row["calificados"],
        "clientes": row["clientes"],
        "sin_etiquetas": row["sin_etiquetas"],
        "prob_promedio": float(row["prob_promedio"]) if row["prob_promedio"] is not None else None,
        "con_score": row["con_score"],
        "con_transcript": row["con_transcript"],
        "por_tag": {r["tag"]: r["n"] for r in por_tag},
        "por_plan_estado": {r["plan_estado"]: r["n"] for r in planes},
        "por_outcome": {r["outcome"]: r["n"] for r in outcomes},
        "primer_lead": primer.isoformat() if primer else None,
    }


@app.get("/api/leads")
async def list_leads(
    tags: Optional[str] = Query(None, description="Etiquetas separadas por coma; el lead debe tenerlas todas"),
    sin_etiquetas: Optional[str] = Query(None),
    plan_estado: Optional[str] = Query(None),
    outcome: Optional[str] = Query(None),
    qualified: Optional[str] = Query(None),
    has_transcript: Optional[str] = Query(None),
    desde: Optional[str] = Query(None),
    hasta: Optional[str] = Query(None),
    sort: str = Query("recientes"),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
    _=Depends(check_auth),
):
    pool = db.get_pool()
    # is_test se excluye igual que en /api/stats: con el rango de fechas compartido entre
    # ambas vistas, los conteos del dashboard y de la tabla tienen que cuadrar.
    conditions: list[str] = ["is_test = false"]
    args: list = []

    # AND entre etiquetas: los desplegables de la barra son de grupos distintos, y
    # "Demo agendada" + "Diego" se lee como los leads que cumplen ambas.
    pedidas = [t for t in (tags or "").split(",") if t in VALID_TAGS]
    if pedidas:
        conditions.append(f"outcome_tags @> ${len(args) + 1}::text[]")
        args.append(pedidas)
    if sin_etiquetas == "true":
        conditions.append("cardinality(outcome_tags) = 0")
    if plan_estado in PLAN_ESTADO_TAG:
        conditions.append(f"plan_estado = ${len(args) + 1}")
        args.append(plan_estado)
    if outcome:  # filtro por el estado principal derivado
        conditions.append(f"outcome = ${len(args) + 1}")
        args.append(outcome)
    if qualified in ("true", "false"):
        conditions.append(f"qualified = ${len(args) + 1}")
        args.append(qualified == "true")
    if has_transcript == "true":
        conditions.append("transcript IS NOT NULL")
    _rango_captura(desde, hasta, conditions, args)

    where = "WHERE " + " AND ".join(conditions)
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
    """Reemplaza el conjunto de etiquetas del lead y recalcula su estado principal."""
    if body.tags is not None:
        crudas = body.tags
    elif body.outcome in (None, "nuevo"):  # 'nuevo' legado = sin etiquetas
        crudas = []
    else:  # formato legado de un solo valor
        crudas = [OUTCOME_LEGACY.get(body.outcome, body.outcome)]
    tags = normalize_tags(crudas)

    pool = db.get_pool()
    result = await pool.execute(
        "UPDATE leads_dataset SET outcome_tags=$1, outcome=$2, outcome_date=NOW(), "
        "outcome_source='manual', updated_at=NOW() WHERE lead_id=$3",
        tags,
        primary_outcome(tags),
        lead_id,
    )
    if result == "UPDATE 0":
        raise HTTPException(status_code=404, detail="Lead not found")
    return {"ok": True, "tags": tags, "outcome": primary_outcome(tags)}


def _es_lead_grande(lead: dict) -> bool:
    """Regla de negocio: lead mediano/grande (>10 RUCs, >=1000 comprobantes o pidió demo)."""
    rucs = lead.get("num_rucs") or 0
    vol = lead.get("volumen_comprobantes") or 0
    return bool(lead.get("pidio_demo")) or rucs > 10 or vol >= 1000


def build_brief(lead: dict) -> dict:
    """Arma el brief por plantilla + reglas, sin LLM. Devuelve un documento estructurado
    (secciones clave-valor) que el frontend renderiza y exporta a PDF."""
    nombre = lead.get("contact_name") or lead.get("wa_display_name") or f"+{lead.get('lead_id', '')}"

    perfil: list[dict] = []
    if lead.get("segmento"):
        perfil.append({"label": "Segmento", "value": str(lead["segmento"])})
    if lead.get("industry"):
        perfil.append({"label": "Industria", "value": str(lead["industry"])})
    if lead.get("num_rucs"):
        perfil.append({"label": "N° de RUCs", "value": str(lead["num_rucs"])})
    if lead.get("volumen_comprobantes"):
        perfil.append({"label": "Volumen", "value": f"{lead['volumen_comprobantes']} comprobantes/mes"})

    contexto: list[dict] = []
    if lead.get("solucion_actual"):
        contexto.append({"label": "Solución actual", "value": str(lead["solucion_actual"])})
    if lead.get("dolor_principal"):
        contexto.append({"label": "Dolor principal", "value": str(lead["dolor_principal"])})
    if lead.get("objecion") and lead["objecion"] != "ninguna":
        contexto.append({"label": "Objeción a manejar", "value": str(lead["objecion"])})
    if lead.get("urgencia"):
        contexto.append({"label": "Urgencia", "value": str(lead["urgencia"])})
    if lead.get("pidio_demo"):
        contexto.append({"label": "Pidió demo", "value": "Sí"})

    modulos = [{"nombre": MODULOS[m]["nombre"], "desc": MODULOS[m]["desc"]}
               for m in (lead.get("modulos_interes") or []) if m in MODULOS]

    if _es_lead_grande(lead):
        siguiente = {"accion": "Agendar demo de 45 minutos", "link": LINK_DEMO}
    else:
        siguiente = {"accion": "Invitar a la prueba gratuita", "link": LINK_TRIAL}

    return {
        "titulo": f"Brief de venta — {nombre}",
        "generado": datetime.now(timezone.utc).isoformat(),
        "lead": {
            "telefono": f"+{lead.get('lead_id', '')}",
            "nombre": lead.get("contact_name") or lead.get("wa_display_name"),
            "empresa": lead.get("company_name"),
            "email": lead.get("email"),
            "ruc": lead.get("tax_id"),
        },
        "conversion_prob": lead.get("conversion_prob"),
        "qualified": bool(lead.get("qualified")),
        "perfil": perfil,
        "contexto": contexto,
        "modulos": modulos,
        "siguiente_paso": siguiente,
    }


@app.post("/api/leads/{lead_id}/brief")
async def generate_brief(lead_id: str, _=Depends(check_auth)):
    pool = db.get_pool()
    row = await pool.fetchrow("SELECT * FROM leads_dataset WHERE lead_id = $1", lead_id)
    if not row:
        raise HTTPException(status_code=404, detail="Lead not found")
    return {"brief": build_brief(db.row_to_dict(row))}


@app.post("/api/sync-planes")
async def sync_planes(_=Depends(check_auth)):
    """Trae de mau-web el estado comercial de cada lead y reetiqueta en consecuencia.

    El import va aqui dentro y no arriba: sync_planes importa de este modulo, y al nivel
    del fichero seria un ciclo.
    """
    from app.sync_planes import sincronizar

    try:
        async with db.get_pool().acquire() as conn:
            return await sincronizar(conn)
    except RuntimeError as e:  # falta el token, o mau-web respondio con error
        raise HTTPException(status_code=502, detail=str(e))


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
