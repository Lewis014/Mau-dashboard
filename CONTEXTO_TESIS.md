# Contexto de tesis — Sistema de Lead Scoring + Briefs para MAU

> Documento de handoff para Claude Code. Resume el proyecto, lo construido, los esquemas y los pendientes.

---

## 1. Contexto académico

- **Autor:** Piero — último año de Ingeniería de Sistemas, UNSA (Arequipa, Perú). Advisor: Dr. Víctor Manuel Cornejo Aparicio. También CTO/Fullstack en Mau Comunica.
- **Tesis:** sistema inteligente de *lead scoring* + generación de *briefs* de venta para **MAU**, el chatbot comercial B2B de **Contatech** (Asistente Contable Virtual, Perú/LATAM), desplegado en WhatsApp vía Chatwoot.
- **Workflow del sistema:**
  1. Entrenar un modelo de predicción de conversión.
  2. Extraer y estructurar features desde conversaciones reales de Chatwoot/WhatsApp.
  3. Puntuar leads por probabilidad de conversión.
  4. Generar briefs personalizados cruzando leads de alto score contra el catálogo de servicios.
- **Marco de defensa:** cada decisión (dataset, features, metodología) debe ser defendible ante jurado. El **dataset propio etiquetado de Chatwoot es la contribución empírica central**.

---

## 2. Enfoque de ML (estado actual)

- Pivote: de XGBoost sobre features lexicales hechas a mano → **transfer learning con embeddings semánticos multilingües**.
- Encoder: `text-embedding-3-large` (3072 dims) → PCA a ~100. Entrenamiento sobre el dataset sintético en inglés `DeepMostInnovations/saas-sales-conversations` (HuggingFace, Apache-2.0), transferido a conversaciones reales en español.
- Justificación: el encoder multilingüe mapea frases equivalentes ("¿precio?" ≈ "pricing?") al mismo espacio → basta **validar** con 30–50 conversaciones reales etiquetadas, no reentrenar desde cero.
- **Criterio de éxito (vara de la tesis):** que funcione, no que sea óptimo. Si en la muestra real da AUC > ~0.65–0.70 → transfiere, se defiende tal cual. Si da ~0.50 → plan B.

### Aprendizajes que restringen las decisiones (no re-litigar)
- Features lexicales hechas a mano ≈ aleatorias (AUC ~0.55). Embeddings semánticos solos ≈ AUC 0.86. La fusión aporta poco.
- Métricas de engagement pre-calculadas (`customer_engagement`, `sales_effectiveness`) = **data leakage** (AUC 0.99 irreproducible en producción). No usar.
- La etiqueta de entrenamiento debe ser el **resultado de negocio**, no un heurístico del LLM. Entrenar contra el `qualified` del LLM = predecir el juicio del LLM, no la conversión (fallo vulnerable ante jurado).
- **Paridad feature-despliegue:** las features de entrenamiento deben ser extraíbles de un chat en vivo. Esto descartó X Education (analytics web) e IBM Watson (métricas de pipeline CRM) como datasets directos.
- **Leads abiertos se excluyen del entrenamiento** (sesgo de censura).
- `num_rucs`, `volumen_comprobantes` (proxy de deal size) y `canal` (route to market) son features defendibles, respaldadas por la investigación de predictores de IBM Watson.

---

## 3. Arquitectura del sistema

**Infra (Docker en VM `mau-vm-bot-procesa-005`, proyecto `~/contatech-aegis`):** n8n, Chatwoot, WhatsApp, **Postgres** (compartido con n8n Chat Memory), Redis (debounce), Qdrant (RAG/KB), MinIO, Telegram, HubSpot.

**Flujo n8n (4 fases):**
1. Sincronización RAG (MinIO → Qdrant, embeddings OpenAI).
2. Recepción + debounce (Chatwoot → Redis, ventana 8s).
3. Cerebro del agente: `AI Sales Agent` (Postgres Chat Memory + Claude Chat Model + tools Qdrant) → `Parse Agent Output` (separa texto de respuesta del JSON de seguimiento).
4. Calificación + CRM (gate `Lead Qualified?` → Telegram + HubSpot upsert/deal/note + nota en Chatwoot).

**Capa de datos para la tesis (lo nuevo):**
- **`leads_dataset`** (Postgres, misma instancia): un snapshot de features por lead, upsert por `lead_id`.
- **Nodo Upsert** insertado **después de `Parse Agent Output`, antes del gate `Lead Qualified?`** → registra TODA conversación (no solo las calificadas; se necesita la clase negativa).
- **Dashboard** (FastAPI + Vue 3): lista leads, etiqueta `outcome` manualmente, genera briefs. Desplegado tras el mismo nginx que n8n, unido a la red Docker de n8n, alcanza Postgres por el nombre de contenedor `postgres`.

### Clave de join
- `n8n_chat_histories.session_id` = `sales-bot-<telefono>`.
- `lead_id` = teléfono = `session_id` sin el prefijo `sales-bot-`.

---

## 4. Decisiones clave de esta iteración

1. **El JSON viejo era para handoff a CRM, no para predecir.** Capturaba identidad (PII) pero tiraba las señales crudas de tamaño (`num_rucs`, `volumen_comprobantes`), guardando solo el resultado digerido (`ticket_estimado`, `tipo_lead`, que son juicios del LLM). Por eso los 97 históricos son débiles.
2. **`qualified` ≠ `converted`.** `qualified` es un FEATURE (heurístico del agente / "ya hay contacto"), no la etiqueta. La etiqueta real es el resultado downstream.
3. **Schema de features rediseñado:** +11 campos estructurados con enums/números/booleanos, regla anti-alucinación (nunca inventar `num_rucs`/`volumen_comprobantes`), y se eliminó el ambiguo `domain`.
4. **`outcome` como enum multi-estado:** `nuevo → demo_agendada → trial_iniciado → cliente | perdido | en_seguimiento`. Los `en_seguimiento` (abiertos) se excluyen del entrenamiento. Etiqueta atada a hecho objetivo cuando se pueda.
5. **`is_test`** para excluir conversaciones de prueba (el primer lead capturado, `51934226756` "Douglas" con el correo del propio Piero, ya está marcado).
6. **Backfill re-extrae desde texto crudo**, no confía en el JSON viejo: los 97 son heterogéneos (algunos de un prompt B2B genérico anterior, ni siquiera contable; otros basura tipo "Hola"/"Hola"). Filtra sesiones con < 2 turnos humanos sustantivos.

---

## 5. Esquemas y comandos

### `leads_dataset` (DDL aplicado)
```sql
CREATE TABLE IF NOT EXISTS leads_dataset (
  lead_id VARCHAR(255) PRIMARY KEY,        -- telefono (session_id sin 'sales-bot-')
  session_id VARCHAR(255),
  -- Identidad / CRM
  contact_name TEXT, email TEXT, company_name TEXT, tax_id TEXT, wa_display_name TEXT,
  -- Features de contenido (extraccion NLP)
  industry TEXT,
  segmento TEXT,                  -- independiente | estudio | empresa
  num_rucs INTEGER,
  volumen_comprobantes INTEGER,
  modulos_interes TEXT[],         -- {comunica,valida,procesa}
  solucion_actual TEXT,           -- manual | concar | exige | contasis | starsoft | odoo | otro | ninguno
  objecion TEXT,                  -- precio | tiene_personal | integracion | pensarlo | ninguna
  urgencia TEXT,                  -- alta | media | baja
  pidio_demo BOOLEAN,
  dolor_principal TEXT,
  -- Features de comportamiento
  canal TEXT DEFAULT 'whatsapp',
  message_count INTEGER,
  was_debounced BOOLEAN,
  -- Juicios del agente (features, NO label)
  tipo_lead TEXT, ticket_estimado TEXT, qualified BOOLEAN,
  -- Etiqueta (se llena desde el dashboard)
  outcome TEXT DEFAULT 'nuevo',   -- nuevo|demo_agendada|trial_iniciado|cliente|perdido|en_seguimiento
  outcome_date TIMESTAMPTZ, outcome_source TEXT,   -- manual | auto
  captured_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now(),
  is_test BOOLEAN DEFAULT false
);
```

### `n8n_chat_histories` (existente, de n8n)
```
id          integer  PK
session_id  varchar(255)   -- 'sales-bot-<telefono>'
message     jsonb          -- formato LangChain: {"type":"human"|"ai","content":"..."}
```

### Dashboard — estructura del proyecto
```
mau-dashboard/
├── docker-compose.yml      # une la red externa de n8n (N8N_NETWORK, ~ contatech-aegis_default)
├── Dockerfile
├── requirements.txt        # fastapi, uvicorn, psycopg2-binary, anthropic, pydantic, python-dotenv
├── .env                    # DB_*, ANTHROPIC_API_KEY, BRIEF_MODEL, APP_TOKEN, N8N_NETWORK
└── app/
    ├── main.py             # GET /api/leads · PATCH /api/leads/{id}/outcome · POST /api/leads/{id}/brief · static
    ├── db.py               # get_conn() via env vars (DB_HOST=postgres)
    ├── catalog.py          # catalogo MAU seed (3 modulos) — TODO: poblar desde Qdrant
    ├── backfill.py         # re-extraccion de features desde transcripts -> upsert leads_dataset
    │                       #   (Claude tool-use / JSON forzado, default claude-sonnet-4-6,
    │                       #    anti-alucinacion num_rucs/volumen, filtro <2 turnos sustantivos,
    │                       #    preserva outcome/is_test en el upsert)
    └── static/index.html   # Vue 3 (CDN), tabla + dropdown outcome + modal brief
```

### Comandos
```bash
# Correr el backfill dentro del contenedor del dashboard (ya tiene deps, env y red):
docker compose up -d --build                                   # rebuild con backfill.py
docker compose exec dashboard python -m app.backfill --dry-run --limit 5   # ensayo, no escribe
docker compose exec dashboard python -m app.backfill           # corrida real (upsert)
# Opcional para depurar una sola conversacion:
# docker compose exec dashboard python -m app.backfill --only-session sales-bot-51999999999

# Diagnostico de senal aprovechable en los historicos:
SELECT count(*) total,
       count(num_rucs) con_rucs,
       count(*) FILTER (WHERE segmento <> '') con_segmento,
       count(volumen_comprobantes) con_volumen,
       count(*) FILTER (WHERE dolor_principal <> '') con_dolor
FROM leads_dataset WHERE is_test = false;
```

---

## 6. Pendientes (orden de ataque)

1. **Correr el backfill** de los ~97 históricos (`docker compose exec dashboard python backfill.py`) y ejecutar el `SELECT` de diagnóstico. → Decidir si los 97 sirven como set de **validación** o solo de prueba.
2. **Definir y cablear el `outcome`:** cómo se sabe quién convirtió — Calendly (demo agendada), registro en `bot.contatech.lat` (trial), o stage del Deal en HubSpot. Hoy se etiqueta **manualmente** desde el dashboard; evaluar wiring automático.
3. **Etiquetar** outcomes (los 97 + leads nuevos) hasta juntar dataset entrenable. Resolver censura (no tratar `en_seguimiento` como negativo).
4. **Entrenar el modelo:** embeddings multilingües sobre el proxy sintético → transferir → validar en los reales etiquetados. Objetivo AUC > 0.65–0.70.
5. **Dashboard v2:** agregar columna de **score** (cuando el modelo exista) + integrar la generación de briefs con la **KB de Qdrant** en vez del catálogo hardcodeado.
6. **Documento de tesis — inconsistencias a resolver:** dos versiones del objetivo principal, dos definiciones de la variable dependiente, conflicto "exploratorio" vs otro tipo de investigación.
7. **Defensa — interpretabilidad:** SHAP sobre componentes PCA no es interpretable → alternativa recomendada: **trayectoria de probabilidad por turno** de la conversación.
8. ~~**Confirmar** el encoder exacto que genera los embeddings de 3072 dims.~~ ✅ **RESUELTO**: el proxy `saas-sales-conversations` trae `embedding_0..3071` precalculados → encoder = `text-embedding-3-large` (3072 dims). La validación real debe usar el MISMO encoder.

### Progreso de entrenamiento (ml/Leads_Tesis.ipynb)
- **Cambio de modelo (decisión metodológica):** se descartó XGBoost como modelo final. En embeddings densos la señal es linealmente separable y un **linear probe** transfiere mejor ante el cambio de dominio inglés→español (menos varianza, menos sobreajuste a quirks del proxy sintético). XGBoost ≈ 0.864 ≈ "embeddings solos" → no aporta sobre un lineal.
- **Pipeline final:** `text-embedding-3-large` (3072) **L2-normalizado** → **regresión logística L2 calibrada** (`C` por CV). XGBoost y SVM lineal quedan como **baselines de comparación** en una tabla (LogReg/SVM/XGB × full-dim/PCA-100) que **justifica empíricamente** la elección. PCA pasa a ablación opcional, no obligatoria.
- **Mejoras de rigor añadidas:** curva de calibración + Brier (score = probabilidad real, no ranking); **IC 95% bootstrap del AUC** (crítico por N real chico); **evaluación por prefijos + trayectoria de probabilidad por turno** (refleja despliegue real, resuelve que `full_text` "filtra" el cierre, y cubre interpretabilidad #7 sin SHAP-sobre-PCA).
- **Resultado proxy previo (baseline XGBoost):** AUC 0.864 / AP 0.856 — confirma que el pipeline embeddings→clasificador aprende la conversión. La tabla comparativa del notebook nuevo fija cuál lineal gana.
- **Pendiente para cerrar #4:** etiquetar `outcome` real (lista de convertidos vía HubSpot/Calendly/registro) y correr la sección 7 → AUC real con IC = métrica central.

### Hallazgo crítico: sesgo de censura por atención humana (resuelto)
- El flujo n8n muere en `Agent Assigned?` (output[0] vacío) cuando un humano toma la conversación (~20% de los casos). En ese estado **no corre el Upsert ni el Postgres Chat Memory** → lo que el lead dice durante atención humana **no se guarda en `leads_dataset` ni en `n8n_chat_histories`**. Solo vive en Chatwoot.
- Sesgo serio: las conversaciones escaladas a humano suelen ser las más valiosas (las que convierten) → el dataset quedaba truncado justo en la clase positiva.
- **Fix aplicado:** `backfill.py` ahora lee los transcripts **completos desde la API de Chatwoot** (`--source chatwoot`, default; lead+bot+agente), no de la memoria del bot. Guarda el transcript en la nueva columna `leads_dataset.transcript`, de la que la validación (notebook §7.1) toma el `full_text` → features y embeddings salen de la misma fuente completa.
- Requiere: `ALTER TABLE leads_dataset ADD COLUMN transcript TEXT;` y `CHATWOOT_API_TOKEN` en el `.env`.
- Consistencia con el proxy: el proxy embeddea la conversación completa (incluye turnos del vendedor), así que validar sobre el transcript completo es lo correcto. Puntuar solo la fase-bot (deployment real) queda como refinamiento/trabajo futuro.
- Pendiente complementario (no urgente): captura incondicional en n8n (nodo antes de `Agent Assigned?`) para no perder mensajes hacia adelante.
- Hallazgos del proxy: `outcome` (0/1) es el target de conversión; `customer_engagement`/`sales_effectiveness` confirmados como leakage (excluidos); `probability_trajectory` por turno disponible → base para interpretabilidad (pendiente #7).
- Backfill ejecutado: 53 leads entrenables de 97 (44 basura con <2 turnos sustantivos). Cobertura: segmento 94%, dolor 83%, num_rucs 57%. Balance qualified ~2:1.
- Pendiente para cerrar #4: etiquetar `outcome` real en el dashboard y correr la sección 7 del notebook.

---

## 7. Referencias

- **Dataset proxy:** `DeepMostInnovations/saas-sales-conversations` (HF, 100k sintéticas, Apache-2.0).
- **Citas académicas:** UCI Bank Marketing (Moro et al. 2014); COLING 2025 (arXiv:2412.19490); investigación de predictores de IBM Watson.
- **Stack ML:** Python, XGBoost, SHAP, PCA, HuggingFace `datasets`, Google Colab (`Leads_Tesis.ipynb`).
- **Stack app:** FastAPI + Vue 3 (dashboard); Postgres (compartido), n8n, Chatwoot, WhatsApp, Anthropic SDK (extracción).
