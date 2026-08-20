import os
import json
import asyncio
import base64
import hashlib
import hmac
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import asyncpg
from fastapi import FastAPI, HTTPException, Depends, Query, Security
from fastapi.responses import FileResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel

from app import db
from app.catalog import MODULOS, LINK_TRIAL, LINK_DEMO
from app.scoring import score_text

APP_TOKEN = os.getenv("APP_TOKEN", "")
ADMIN_USER = os.getenv("ADMIN_USER", "admin").strip().lower()
ADMIN_PASS = os.getenv("ADMIN_PASS", "")
SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", str(12 * 3600)))
_SECRET = (os.getenv("SECRET_KEY") or APP_TOKEN or ADMIN_PASS).encode()
bearer_scheme = HTTPBearer(auto_error=False)


def parse_pares(raw: str) -> dict[str, str]:
    """'alyssa:clave,diego:otra' -> {'alyssa': 'clave', 'diego': 'otra'}.

    Cada vendedor entra con su propio usuario para que el autor de una nota sea un hecho
    del sistema y no una declaracion: el token de sesion ya lleva dentro quien lo pidio.
    Con el login compartido de antes, «quien escribio esto» no tenia respuesta posible.

    La coma separa entradas y el primer ':' separa usuario de clave: la clave puede llevar
    ':' pero no ','.
    """
    usuarios: dict[str, str] = {}
    for parte in raw.split(","):
        usuario, _, clave = parte.partition(":")
        usuario, clave = usuario.strip().lower(), clave.strip()
        if usuario and clave:
            usuarios[usuario] = clave
    return usuarios


DASHBOARD_USERS = parse_pares(os.getenv("DASHBOARD_USERS", ""))

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

# Hora local a la que se sincronizan los planes con mau-web cada dia. -1 lo desactiva.
# Una vez al dia sobra: las pruebas duran 15 dias y los pagos los aprueba un admin a mano,
# no hay nada que cambie mas rapido.
SYNC_PLANES_HORA = int(os.getenv("SYNC_PLANES_HORA", "7"))

# Limites de los campos de seguimiento. El siguiente paso es CORTO a proposito: si admite
# un parrafo deja de ser una accion y se convierte en otra nota.
NOTA_MAX = 2000
SIGUIENTE_PASO_MAX = 140

# Contrasenas. El maximo existe para que nadie pueda cargar el servidor mandando a derivar
# un texto enorme: scrypt es caro a proposito.
CLAVE_MIN = 8
CLAVE_MAX = 200
# Parametros de scrypt: ~16 MB de memoria por derivacion. Van DENTRO del hash guardado para
# poder subirlos mas adelante sin invalidar las claves que ya existan.
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1


def _tz_local():
    """Zona horaria del negocio, con respaldo si la imagen no trae la base de zonas.

    python:3.12-slim no incluye tzdata; va en requirements.txt, pero si faltara no puede
    tumbar el arranque por una tarea de fondo. Peru es UTC-5 fijo y sin horario de verano,
    asi que el respaldo es exacto para el valor por defecto.
    """
    try:
        return ZoneInfo(LOCAL_TZ)
    except (ZoneInfoNotFoundError, KeyError, ValueError):
        return timezone(timedelta(hours=-5))


def hoy_local() -> date:
    """El dia de hoy en la zona del negocio.

    El contenedor corre en UTC: a las 20:00 de Lima ya es el dia siguiente en UTC, y un
    seguimiento para hoy figuraria como vencido cinco horas antes de tiempo.
    """
    return datetime.now(_tz_local()).date()


def _segundos_hasta(hora: int) -> float:
    """Segundos hasta la proxima vez que den las `hora`:00 locales (manana si ya paso)."""
    ahora = datetime.now(_tz_local())
    objetivo = ahora.replace(hour=hora, minute=0, second=0, microsecond=0)
    if objetivo <= ahora:
        objetivo += timedelta(days=1)
    return (objetivo - ahora).total_seconds()


def _en_hilo(corutina_factory):
    """Corre una corutina bloqueante en su propio hilo y bucle.

    backfill y score_leads usan urllib y los clientes sincronos de Anthropic/OpenAI: si se
    ejecutaran en el bucle de la app, el dashboard se quedaria colgado los minutos que dura
    la pasada por Chatwoot. Cada uno abre ademas su propia conexion a Postgres, asi que
    aislarlos en un hilo con su bucle propio no comparte nada con el de la app.
    """
    return asyncio.to_thread(lambda: asyncio.run(corutina_factory()))


async def _tareas_diarias():
    """Pasada diaria: traer conversaciones -> puntuarlas -> estado comercial -> alertas.

    El orden no es casual, cada paso alimenta al siguiente: el backfill trae lo nuevo de
    Chatwoot y anula el score de las conversaciones que siguieron, el scoring rellena justo
    esos huecos, el sync actualiza planes y etiquetas, y las alertas se evaluan al final
    sobre datos frescos. Avisar de vencimientos leyendo la foto de ayer es peor que no avisar.

    Cada paso va en su propio try: que Chatwoot este caido no debe impedir sincronizar con
    mau-web ni mandar las alertas. Los fallos se registran y se reintenta al dia siguiente;
    nunca tumban la app, porque todas estas tareas son accesorias.
    """
    import argparse

    from app.alertas import revisar_y_avisar
    from app.backfill import run as correr_backfill
    from app.score_leads import run as correr_scoring
    from app.sync_planes import sincronizar

    while True:
        await asyncio.sleep(_segundos_hasta(SYNC_PLANES_HORA))

        # Sin ANTHROPIC_API_KEY no hay extraccion posible; se salta en vez de fallar 583 veces.
        if os.getenv("ANTHROPIC_API_KEY"):
            try:
                opciones = argparse.Namespace(source="chatwoot", dry_run=False, limit=0,
                                              only_lead=None, skip_existing=False, reextraer=False)
                c = await _en_hilo(lambda: correr_backfill(opciones))
                print(f"[backfill] nuevos/cambiados={c['ok']} sin-cambios={c.get('igual', 0)} "
                      f"descartados={c['skip']} errores={c['err']}", flush=True)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — Chatwoot caido, cuota de Claude...
                print(f"[backfill] fallo: {e}", flush=True)

        # Puntua solo los que tienen conversion_prob NULL: los nuevos y aquellos cuyo
        # transcript cambio (el upsert del backfill se lo acaba de anular).
        if os.getenv("OPENAI_API_KEY"):
            try:
                s = await _en_hilo(lambda: correr_scoring(
                    argparse.Namespace(all=False, only_lead=None, dry_run=False)))
                print(f"[scoring] puntuados={s['puntuados']} errores={s['errores']}", flush=True)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — OpenAI caido, modelo ausente...
                print(f"[scoring] fallo: {e}", flush=True)

        if os.getenv("MAUWEB_API_TOKEN"):
            try:
                async with db.get_pool().acquire() as conn:
                    r = await sincronizar(conn)
                print(f"[sync-planes] {r['cruzados']}/{r['leads']} leads cruzados "
                      f"(telefono={r['por_telefono']} correo={r['por_correo']} "
                      f"sin_cuenta={r['sin_cuenta']}) | {r['por_estado']}", flush=True)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — mau-web caido, token malo, red...
                print(f"[sync-planes] fallo: {e}", flush=True)

        try:
            async with db.get_pool().acquire() as conn:
                a = await revisar_y_avisar(conn)
            print(f"[alertas] enviadas={a['enviadas']} {a['por_tipo']} "
                  f"| ya avisadas antes={a['repetidas']} "
                  f"| sin responsable (no se avisa)={a['sin_dueno']}", flush=True)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — webhook caido, n8n reiniciandose...
            print(f"[alertas] fallo: {e}", flush=True)


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


def hash_clave(clave: str) -> str:
    """Deriva una contrasena con scrypt. Formato: scrypt$n$r$p$sal$hash.

    Los parametros van DENTRO del resultado para poder subirlos mas adelante sin invalidar
    las contrasenas ya guardadas: al verificar se usan los del propio hash, no los de ahora.

    scrypt es de la biblioteca estandar; este repo evita dependencias nuevas a proposito.
    """
    sal = os.urandom(16)
    h = hashlib.scrypt(clave.encode(), salt=sal, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${sal.hex()}${h.hex()}"


def verificar_clave(clave: str, guardado: str) -> bool:
    """Comprueba una contrasena contra su hash. Un hash corrupto es un 'no', no un 500."""
    try:
        algo, n, r, p, sal, h = guardado.split("$")
        if algo != "scrypt":
            return False
        calc = hashlib.scrypt(clave.encode(), salt=bytes.fromhex(sal),
                              n=int(n), r=int(r), p=int(p), dklen=len(h) // 2)
        return hmac.compare_digest(calc.hex(), h)
    except Exception:  # noqa: BLE001 — hash ilegible = no autorizado
        return False


async def _clave_valida(usuario: str, clave: str) -> bool:
    """¿Es esa la contrasena del usuario?

    Manda la base de datos: si la persona ya cambio la suya, la de DASHBOARD_USERS deja de
    servir. Esa variable es la credencial INICIAL que reparte el administrador.

    Pero DASHBOARD_USERS si manda sobre QUIEN puede entrar, incluso si tiene fila propia:
    asi dar de baja a alguien es un solo sitio, y no dos de los que siempre se olvida el
    segundo. Para devolverle la contrasena inicial basta con borrar su fila.
    """
    if not ((usuario == ADMIN_USER and ADMIN_PASS) or usuario in DASHBOARD_USERS):
        return False
    fila = await db.get_pool().fetchrow(
        "SELECT clave_hash FROM dashboard_usuarios WHERE usuario = $1", usuario
    )
    if fila:
        # scrypt bloquea (esa es la gracia): va a un hilo para no congelar el event loop,
        # igual que score_text y que el consultar() del sync.
        return await asyncio.to_thread(verificar_clave, clave, fila["clave_hash"])
    esperada = ADMIN_PASS if usuario == ADMIN_USER else DASHBOARD_USERS.get(usuario, "")
    return bool(esperada) and hmac.compare_digest(clave, esperada)


def _sign(payload: str) -> str:
    return hmac.new(_SECRET, payload.encode(), hashlib.sha256).hexdigest()


def make_session_token(user: str) -> str:
    payload = f"{user}:{int(time.time()) + SESSION_TTL_SECONDS}"
    return base64.urlsafe_b64encode(f"{payload}:{_sign(payload)}".encode()).decode()


def session_user(tok: str) -> Optional[str]:
    """Usuario que hay dentro del token, o None si es invalido, caducado o falsificado.

    La firma cubre usuario y caducidad juntos, asi que el nombre que sale de aqui no se
    puede manipular desde el navegador: es el que se guarda como autor de las notas.
    """
    try:
        payload, sig = base64.urlsafe_b64decode(tok.encode()).decode().rsplit(":", 1)
        user, exp = payload.rsplit(":", 1)
        if hmac.compare_digest(sig, _sign(payload)) and int(exp) > time.time():
            return user
    except Exception:  # noqa: BLE001 — token malformado = no autorizado
        pass
    return None


def check_auth(creds: Optional[HTTPAuthorizationCredentials] = Security(bearer_scheme)) -> str:
    """Autoriza y devuelve QUIEN hace la peticion. Las notas lo usan como autor."""
    tok = creds.credentials if creds else ""
    # Acepta el APP_TOKEN legado (scripts/batch) o un token de sesion del login.
    if tok and APP_TOKEN and hmac.compare_digest(tok, APP_TOKEN):
        return "api"
    if tok and _SECRET and (user := session_user(tok)):
        return user
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
    # Desde cuando el lead esta en su plan_estado actual. Sin esta columna no se puede
    # saber cuanto lleva un pago sin aprobarse, que es la unica forma de detectar un pago
    # atascado: mau-web no expone pagos rechazados (ver app/alertas.py).
    await pool.execute("ALTER TABLE leads_dataset ADD COLUMN IF NOT EXISTS plan_estado_desde timestamptz")

    # Seguimiento: el siguiente paso concreto y cuando toca. Va en leads_dataset y no en
    # una tabla aparte porque hay UNO por lead (se reemplaza, no se acumula) y asi se puede
    # filtrar y ordenar sin join. El autor y la fecha de edicion salen de la sesion.
    await pool.execute(
        """
        ALTER TABLE leads_dataset
          ADD COLUMN IF NOT EXISTS siguiente_paso       text,
          ADD COLUMN IF NOT EXISTS siguiente_paso_fecha date,
          ADD COLUMN IF NOT EXISTS siguiente_paso_autor text,
          ADD COLUMN IF NOT EXISTS siguiente_paso_at    timestamptz
        """
    )
    # Parcial: solo interesan los leads que SI tienen fecha, que son los que pueden vencer.
    await pool.execute(
        "CREATE INDEX IF NOT EXISTS idx_leads_sig_paso ON leads_dataset (siguiente_paso_fecha) "
        "WHERE siguiente_paso_fecha IS NOT NULL"
    )

    # Contrasenas cambiadas por cada persona. Solo guarda el hash, y solo de quien la haya
    # cambiado: quien no aparezca aqui sigue entrando con la de DASHBOARD_USERS. Borrar una
    # fila es, justamente, devolverle a esa persona su contrasena inicial.
    await pool.execute(
        """
        CREATE TABLE IF NOT EXISTS dashboard_usuarios (
          usuario        text PRIMARY KEY,
          clave_hash     text NOT NULL,
          actualizado_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )

    # Notas por lead: varias, en orden cronologico, con autor y fecha automaticos.
    # ON DELETE CASCADE es lo que garantiza que las notas mueren con el lead a nivel de
    # base de datos, no por confianza en que el codigo se acuerde de borrarlas.
    await pool.execute(
        """
        CREATE TABLE IF NOT EXISTS lead_notas (
          id        bigserial PRIMARY KEY,
          lead_id   varchar(255) NOT NULL REFERENCES leads_dataset(lead_id) ON DELETE CASCADE,
          texto     text NOT NULL,
          autor     text NOT NULL,
          creado_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    await pool.execute(
        "CREATE INDEX IF NOT EXISTS idx_lead_notas_lead ON lead_notas (lead_id, creado_at DESC)"
    )

    # Alertas ya enviadas. La PK es el antiduplicado: el mismo aviso no se repite cada dia,
    # pero un trial renovado (clave = fecha de vencimiento nueva) si vuelve a avisar.
    await pool.execute(
        """
        CREATE TABLE IF NOT EXISTS lead_alertas (
          lead_id    varchar(255) NOT NULL REFERENCES leads_dataset(lead_id) ON DELETE CASCADE,
          tipo       text NOT NULL,
          clave      text NOT NULL,
          enviada_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (lead_id, tipo, clave)
        )
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

    # Los usuarios del login deben ser gente del equipo: asi el autor de una nota y la
    # etiqueta Responsable hablan el mismo idioma y «Alyssa» significa lo mismo en los dos.
    if (ajenos := sorted(set(DASHBOARD_USERS) - set(TAG_GROUPS["responsable"]))):
        print(f"[login] aviso: {', '.join(ajenos)} no figuran como responsables; "
              f"sus notas apareceran firmadas con ese nombre igual", flush=True)

    # Pasada diaria: backfill -> scoring -> sync -> alertas. Se programa siempre; cada paso
    # decide por su cuenta si puede correr segun la clave que necesite, porque no dependen
    # entre si: que falte el token de mau-web no es motivo para dejar de traer conversaciones.
    tarea_sync = None
    if SYNC_PLANES_HORA >= 0:
        tarea_sync = asyncio.create_task(_tareas_diarias())
        pasos = [n for n, ok in (("backfill", os.getenv("ANTHROPIC_API_KEY")),
                                 ("scoring", os.getenv("OPENAI_API_KEY")),
                                 ("sync-planes", os.getenv("MAUWEB_API_TOKEN")),
                                 ("alertas", True)) if ok]
        print(f"[diario] programado a las {SYNC_PLANES_HORA:02d}:00 {LOCAL_TZ} "
              f"-> {' > '.join(pasos)}", flush=True)
    else:
        print("[diario] desactivado (SYNC_PLANES_HORA=-1)", flush=True)

    from app.alertas import estado_config
    print(f"[alertas] {estado_config()}", flush=True)

    yield

    if tarea_sync:
        tarea_sync.cancel()
    await db.close_pool()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)


class OutcomeBody(BaseModel):
    # El dashboard manda siempre el conjunto completo (reemplazo, no incremento).
    # outcome es el formato legado de un solo valor; se acepta para no romper a n8n/scripts.
    tags: Optional[list[str]] = None
    outcome: Optional[str] = None


class NotaBody(BaseModel):
    # Solo el texto: el autor sale de la sesion y la fecha de la base de datos. Si el
    # cliente pudiera mandarlos, la firma de la nota no valdria nada.
    texto: str


class SiguientePasoBody(BaseModel):
    texto: Optional[str] = None
    fecha: Optional[str] = None   # YYYY-MM-DD


class LoginBody(BaseModel):
    username: str
    password: str


class ClaveBody(BaseModel):
    # No lleva 'usuario': quien cambia la contrasena sale del token de sesion.
    actual: str
    nueva: str


@app.post("/api/login")
async def login(body: LoginBody):
    """Admite la cuenta de administracion y una por vendedor (DASHBOARD_USERS)."""
    if not ADMIN_PASS and not DASHBOARD_USERS:
        raise HTTPException(status_code=503,
                            detail="Login no configurado (falta ADMIN_PASS o DASHBOARD_USERS)")
    usuario = body.username.strip().lower()
    if not await _clave_valida(usuario, body.password):
        raise HTTPException(status_code=401, detail="Credenciales inválidas")
    return {"token": make_session_token(usuario), "usuario": usuario,
            "expires_in": SESSION_TTL_SECONDS}


@app.post("/api/password")
async def cambiar_clave(body: ClaveBody, usuario: str = Depends(check_auth)):
    """Cambia la contrasena de quien la pide.

    El usuario sale de la SESION, nunca del cuerpo: nadie puede cambiar la de otro. Es el
    mismo principio que el autor de las notas — lo que se firma no lo elige el cliente.
    """
    if usuario == "api":
        raise HTTPException(status_code=403,
                            detail="El token de scripts no tiene contraseña que cambiar.")
    if not await _clave_valida(usuario, body.actual):
        raise HTTPException(status_code=401, detail="La contraseña actual no es correcta")
    nueva = body.nueva
    if len(nueva) < CLAVE_MIN:
        raise HTTPException(status_code=400,
                            detail=f"La nueva contraseña debe tener al menos {CLAVE_MIN} caracteres")
    # El maximo no es capricho: sin el, cualquiera con sesion puede poner al servidor a
    # derivar un texto enorme, y scrypt es caro a proposito.
    if len(nueva) > CLAVE_MAX:
        raise HTTPException(status_code=400,
                            detail=f"La nueva contraseña no puede pasar de {CLAVE_MAX} caracteres")
    if nueva == body.actual:
        raise HTTPException(status_code=400, detail="La nueva contraseña es igual a la actual")

    await db.get_pool().execute(
        "INSERT INTO dashboard_usuarios (usuario, clave_hash) VALUES ($1, $2) "
        "ON CONFLICT (usuario) DO UPDATE "
        "SET clave_hash = EXCLUDED.clave_hash, actualizado_at = NOW()",
        usuario, await asyncio.to_thread(hash_clave, nueva),
    )
    return {"ok": True, "usuario": usuario}


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


def _dia_iso(raw: str) -> date:
    """'YYYY-MM-DD' -> date, con 400 si no lo es."""
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Fecha inválida: {raw!r} (se espera YYYY-MM-DD)")


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
        args.append(_dia_iso(raw))
        conditions.append(f"(captured_at AT TIME ZONE {tz})::date {op} ${len(args)}")


# Columnas sobre las que busca la caja del header. Es la identidad del lead, no su
# conversacion: el transcript se deja fuera a proposito porque buscar "Concar" devolveria
# media base y obligaria a un indice trigram sobre la columna mas pesada de la tabla.
BUSQUEDA_COLS = ("contact_name", "company_name", "email", "tax_id", "wa_display_name", "lead_id")


def _busqueda(q: Optional[str], conditions: list[str], args: list) -> None:
    """Agrega el filtro de texto libre a conditions/args (los modifica in-place)."""
    termino = (q or "").strip()
    if not termino:
        return
    args.append(f"%{termino}%")
    like = f"${len(args)}"
    partes = [f"{c} ILIKE {like}" for c in BUSQUEDA_COLS]
    # El telefono se guarda como '51987654321' y la gente lo pega como '+51 987 654 321'.
    # Sin esto, copiar un numero de WhatsApp y buscarlo no encuentra nada, que es
    # justamente lo primero que alguien intenta hacer con un buscador.
    digitos = "".join(c for c in termino if c.isdigit())
    if len(digitos) >= 6:
        args.append(f"%{digitos}%")
        partes.append(f"lead_id LIKE ${len(args)}")
    conditions.append("(" + " OR ".join(partes) + ")")


# Filtros por la fecha del siguiente paso. 'hoy' es siempre el dia de Lima (hoy_local).
SQL_VENCIDO = "siguiente_paso_fecha < {hoy}::date"
SEGUIMIENTO_SQL = {
    "vencido": SQL_VENCIDO,
    "hoy":     "siguiente_paso_fecha = {hoy}::date",
    "semana":  "siguiente_paso_fecha BETWEEN {hoy}::date AND {hoy}::date + 7",
}


def _seguimiento(valor: Optional[str], conditions: list[str], args: list) -> None:
    """Agrega el filtro de seguimiento a conditions/args (los modifica in-place)."""
    if valor == "sin_definir":
        conditions.append("siguiente_paso_fecha IS NULL")
        return
    plantilla = SEGUIMIENTO_SQL.get(valor or "")
    if not plantilla:
        return
    args.append(hoy_local())
    conditions.append(plantilla.format(hoy=f"${len(args)}"))


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

    # El dia de hoy va como argumento extra SOLO en esta consulta; las de abajo siguen
    # recibiendo *args tal cual, que asyncpg exige exactamente los que la consulta usa.
    vencido = SQL_VENCIDO.format(hoy=f"${len(args) + 1}")
    row = await pool.fetchrow(
        f"""
        SELECT
          COUNT(*)                                            AS total,
          COUNT(*) FILTER (WHERE qualified)                   AS calificados,
          COUNT(*) FILTER (WHERE outcome_tags @> ARRAY['cliente'])  AS clientes,
          COUNT(*) FILTER (WHERE cardinality(outcome_tags) = 0)     AS sin_etiquetas,
          AVG(conversion_prob)                                AS prob_promedio,
          COUNT(*) FILTER (WHERE conversion_prob IS NOT NULL) AS con_score,
          COUNT(*) FILTER (WHERE transcript IS NOT NULL)      AS con_transcript,
          COUNT(*) FILTER (WHERE {vencido})                   AS seguimientos_vencidos
        FROM leads_dataset
        {where}
        """,
        *args,
        hoy_local(),
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
        "seguimientos_vencidos": row["seguimientos_vencidos"],
        "por_tag": {r["tag"]: r["n"] for r in por_tag},
        "por_plan_estado": {r["plan_estado"]: r["n"] for r in planes},
        "por_outcome": {r["outcome"]: r["n"] for r in outcomes},
        "primer_lead": primer.isoformat() if primer else None,
    }


@app.get("/api/leads")
async def list_leads(
    q: Optional[str] = Query(None, description="Busqueda libre sobre los datos de identidad del lead"),
    tags: Optional[str] = Query(None, description="Etiquetas separadas por coma; el lead debe tenerlas todas"),
    sin_etiquetas: Optional[str] = Query(None),
    plan_estado: Optional[str] = Query(None),
    outcome: Optional[str] = Query(None),
    qualified: Optional[str] = Query(None),
    has_transcript: Optional[str] = Query(None),
    seguimiento: Optional[str] = Query(None, description="vencido | hoy | semana | sin_definir"),
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
    _busqueda(q, conditions, args)
    _seguimiento(seguimiento, conditions, args)
    # Buscar IGNORA el rango de fechas. Que un telefono no aparezca porque el dashboard
    # tenia puesto "Agosto" es el fallo que hace que nadie vuelva a usar el buscador.
    if not (q or "").strip():
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


@app.get("/api/leads/{lead_id}/notas")
async def list_notas(lead_id: str, _=Depends(check_auth)):
    """Notas del lead, la mas reciente primero: es la que se lee antes de llamar."""
    rows = await db.get_pool().fetch(
        "SELECT id, texto, autor, creado_at FROM lead_notas WHERE lead_id = $1 "
        "ORDER BY creado_at DESC, id DESC",
        lead_id,
    )
    return {"items": [db.row_to_dict(r) for r in rows]}


@app.post("/api/leads/{lead_id}/notas")
async def add_nota(lead_id: str, body: NotaBody, usuario: str = Depends(check_auth)):
    """Añade una nota firmada por el usuario de la sesion y fechada por la base de datos."""
    texto = (body.texto or "").strip()
    if not texto:
        raise HTTPException(status_code=400, detail="La nota está vacía")
    if len(texto) > NOTA_MAX:
        raise HTTPException(status_code=400, detail=f"La nota supera los {NOTA_MAX} caracteres")
    try:
        row = await db.get_pool().fetchrow(
            "INSERT INTO lead_notas (lead_id, texto, autor) VALUES ($1, $2, $3) "
            "RETURNING id, texto, autor, creado_at",
            lead_id, texto, usuario,
        )
    except asyncpg.ForeignKeyViolationError:
        raise HTTPException(status_code=404, detail="Lead not found")
    return db.row_to_dict(row)


@app.patch("/api/leads/{lead_id}/siguiente-paso")
async def update_siguiente_paso(lead_id: str, body: SiguientePasoBody,
                                usuario: str = Depends(check_auth)):
    """Fija (o limpia, con texto vacio) el siguiente paso del lead y su fecha."""
    texto = (body.texto or "").strip()
    if len(texto) > SIGUIENTE_PASO_MAX:
        raise HTTPException(
            status_code=400,
            detail=f"El siguiente paso debe caber en {SIGUIENTE_PASO_MAX} caracteres. "
                   "Si necesitas más espacio, eso es una nota, no un siguiente paso.",
        )
    fecha = _dia_iso(body.fecha) if body.fecha else None
    # La fecha es obligatoria cuando hay texto: un siguiente paso que no puede vencer no
    # aparece en ninguna vista de vencidos, y ese es justo el seguimiento que se cae.
    if texto and not fecha:
        raise HTTPException(status_code=400,
                            detail="El siguiente paso necesita fecha: sin fecha no puede vencer.")

    result = await db.get_pool().execute(
        "UPDATE leads_dataset SET siguiente_paso=$1, siguiente_paso_fecha=$2, "
        "siguiente_paso_autor=$3, siguiente_paso_at=NOW(), updated_at=NOW() WHERE lead_id=$4",
        texto or None, fecha, usuario if texto else None, lead_id,
    )
    if result == "UPDATE 0":
        raise HTTPException(status_code=404, detail="Lead not found")
    return {"ok": True, "siguiente_paso": texto or None,
            "siguiente_paso_fecha": fecha, "siguiente_paso_autor": usuario if texto else None}


def _es_lead_grande(lead: dict) -> bool:
    """Regla de negocio: lead mediano/grande (>10 RUCs, >=1000 comprobantes o pidió demo)."""
    rucs = lead.get("num_rucs") or 0
    vol = lead.get("volumen_comprobantes") or 0
    return bool(lead.get("pidio_demo")) or rucs > 10 or vol >= 1000


def build_brief(lead: dict, ultima_nota: Optional[dict] = None) -> dict:
    """Arma el brief por plantilla + reglas, sin LLM. Devuelve un documento estructurado
    (secciones clave-valor) que el frontend renderiza y exporta a PDF.

    El seguimiento y la ultima nota van dentro a proposito: si no salieran aqui, habria que
    abrir otra pantalla antes de cada llamada y los campos se abandonarian en dos semanas.
    """
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

    # El seguimiento que escribio el equipo. No se mezcla con `siguiente_paso` de arriba,
    # que es una sugerencia derivada del tamaño del lead: uno es lo que alguien decidio
    # hacer, el otro lo que la regla propone. En el documento van separados.
    seguimiento = None
    if lead.get("siguiente_paso"):
        fecha = lead.get("siguiente_paso_fecha")
        seguimiento = {
            "texto": lead["siguiente_paso"],
            "fecha": fecha.isoformat() if isinstance(fecha, date) else fecha,
            "autor": lead.get("siguiente_paso_autor"),
            "vencido": bool(isinstance(fecha, date) and fecha < hoy_local()),
        }

    nota = None
    if ultima_nota:
        nota = {
            "texto": ultima_nota["texto"],
            "autor": ultima_nota["autor"],
            "fecha": ultima_nota["creado_at"].isoformat(),
        }

    return {
        "seguimiento": seguimiento,
        "ultima_nota": nota,
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
    nota = await pool.fetchrow(
        "SELECT texto, autor, creado_at FROM lead_notas WHERE lead_id = $1 "
        "ORDER BY creado_at DESC, id DESC LIMIT 1",
        lead_id,
    )
    return {"brief": build_brief(db.row_to_dict(row), dict(nota) if nota else None)}


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


@app.post("/api/alertas/preview")
async def alertas_preview(_=Depends(check_auth)):
    """Que alertas se enviarian ahora mismo, sin enviar ni registrar nada.

    Existe para poder mirar el criterio antes de confiar en el: una alerta automatica que
    nadie ha visto funcionar en seco no se cree cuando llega, y se ignora.
    """
    from app.alertas import evaluar

    async with db.get_pool().acquire() as conn:
        return await evaluar(conn)


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
