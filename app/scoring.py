"""Scoring de conversion: transcript -> P(conversion) con el modelo entrenado.

Modelo portable (`leads_model.json`): coeficientes de la regresion logistica +
metadatos del encoder. Se puntua en Python puro (sigmoid), SIN sklearn, para no
acoplar la version de sklearn del contenedor con la de Colab y evitar dependencias
pesadas (scipy). Para una LogReg binaria, sigmoid(w.x + b) reproduce EXACTAMENTE
`predict_proba(...)[:,1]`.

Pipeline (identico al de entrenamiento y validacion del notebook):
    transcript -> OpenAI text-embedding-3-large (3072d) -> L2-normalize -> sigmoid(w.x + b)

Formato de leads_model.json (lo exporta la seccion 6 del notebook):
    {"coef": [...3072 floats...], "intercept": float,
     "l2_normalize": true, "encoder": "text-embedding-3-large", "emb_dim": 3072}

Env:
    MODEL_PATH   -> ruta al json (default app/models/leads_model.json; en Docker /models/leads_model.json)
    EMBED_MODEL  -> encoder (default text-embedding-3-large; DEBE coincidir con el del entrenamiento)
    OPENAI_API_KEY
"""
import json
import math
import os
from functools import lru_cache
from typing import Optional

MODEL_PATH = os.getenv(
    "MODEL_PATH",
    os.path.join(os.path.dirname(__file__), "models", "leads_model.json"),
)
EMBED_MODEL = os.getenv("EMBED_MODEL", "text-embedding-3-large")
MAX_CHARS = 8000  # recorte defensivo por request de embeddings


@lru_cache(maxsize=1)
def _load_model() -> dict:
    with open(MODEL_PATH, "r", encoding="utf-8") as f:
        m = json.load(f)
    m["coef"] = [float(x) for x in m["coef"]]
    m["intercept"] = float(m["intercept"])
    return m


def _embed(text: str) -> list[float]:
    from openai import OpenAI  # import perezoso: la API solo se necesita al puntuar

    client = OpenAI()  # usa OPENAI_API_KEY del entorno
    resp = client.embeddings.create(model=EMBED_MODEL, input=[text[:MAX_CHARS]])
    return resp.data[0].embedding


def _l2(v: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norm for x in v]


def score_text(transcript: str) -> Optional[float]:
    """Devuelve P(conversion) en [0,1], o None si el transcript esta vacio."""
    if not transcript or not transcript.strip():
        return None
    m = _load_model()
    emb = _embed(transcript)
    if len(emb) != len(m["coef"]):
        raise ValueError(
            f"Dim del embedding ({len(emb)}) != dim del modelo ({len(m['coef'])}); "
            f"revisa EMBED_MODEL ({EMBED_MODEL})."
        )
    x = _l2(emb) if m.get("l2_normalize", True) else emb
    z = m["intercept"] + sum(w * xi for w, xi in zip(m["coef"], x))
    return 1.0 / (1.0 + math.exp(-z))
