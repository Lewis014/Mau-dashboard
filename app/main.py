import os
import io
import csv
import json
import time
import hmac
import codecs
import base64
import asyncio
import hashlib
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import asyncpg
from fastapi import FastAPI, HTTPException, Depends, Query, Security
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import db
from app.catalog import MODULOS, LINK_TRIAL, LINK_DEMO
from app.scoring import score_text
from app import reparto
from app import afinidad

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

# Horas sin que nadie escriba en el inbox a partir de las cuales se considera que la fuente
# esta muda: el dashboard lo dice en pantalla y sale un aviso (app/alertas.py, revisar_fuente).
# Con el ritmo normal de ~7 leads al dia, un dia entero en blanco ya es una anomalia; 0 lo
# fuerza siempre, que es como se prueba que el aviso llega de verdad.
FUENTE_MUDA_HORAS = int(os.getenv("ALERTAS_FUENTE_HORAS", "24"))

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


# Estado de la ultima pasada de backfill lanzada desde este proceso, para que el boton
# «Traer leads nuevos» pueda informar del avance: la pasada tarda minutos y no cabe en el
# tiempo de una peticion HTTP.
_backfill_estado: dict = {"estado": "inactivo", "origen": None, "desde": None,
                          "hasta": None, "resultado": None, "detalle": None}
# Referencia viva a la pasada lanzada desde el boton. asyncio solo guarda referencias debiles
# a las tareas: sin esto, el recolector puede llevarse una pasada de veinte minutos a la mitad,
# y el sintoma seria un backfill que "a veces" no termina.
_backfill_tarea: Optional[asyncio.Task] = None


def backfill_estado() -> dict:
    """Copia del estado, para que quien lo lea no pueda modificarlo sin querer."""
    return {**_backfill_estado, "corriendo": _backfill_estado["estado"] == "corriendo"}


def _marcar_backfill_iniciado(origen: str) -> bool:
    """Reserva el turno, o devuelve False si ya hay una pasada en marcha.

    Es SINCRONA a proposito: sin un await por medio, entre la comprobacion y la reserva no
    puede colarse nadie, asi que hace de exclusion mutua sin necesidad de un lock. Y tiene que
    reservarse aqui y no dentro de la pasada, porque el endpoint responde ANTES de que la
    tarea llegue a ejecutarse: si el estado se marcara alla, la primera consulta de avance
    leeria «inactivo» y el dashboard daria por terminada una pasada que aun no ha empezado.

    El turno importa: dos barridos a la vez sobre las mismas conversaciones pagarian dos veces
    la extraccion con Claude y competirian por escribir las mismas filas.
    """
    if _backfill_estado["estado"] == "corriendo":
        return False
    _backfill_estado.update(estado="corriendo", origen=origen,
                            desde=datetime.now(timezone.utc).isoformat(),
                            hasta=None, resultado=None, detalle=None)
    return True


async def _correr_backfill(origen: str) -> dict:
    """La pasada en si. El turno ya viene reservado por _marcar_backfill_iniciado().

    `origen` queda anotado en pasadas_backfill para poder distinguir despues, mirando el
    historial, la automatica de las 07:00 de un boton pulsado a mano.
    """
    # TODO dentro del try, importaciones incluidas: si algo revienta antes de empezar (por
    # ejemplo el import de anthropic) y el estado se quedara en «corriendo», el turno no se
    # soltaria nunca y el boton quedaria muerto hasta reiniciar el contenedor, sin que nada
    # lo explicara en pantalla.
    try:
        import argparse

        from app.backfill import run as correr_backfill

        opciones = argparse.Namespace(source="chatwoot", dry_run=False, limit=0,
                                      only_lead=None, skip_existing=False,
                                      reextraer=False, origen=origen)
        c = await _en_hilo(lambda: correr_backfill(opciones))
    except BaseException as e:  # noqa: BLE001 — Chatwoot caido, cuota de Claude, cancelacion...
        _backfill_estado.update(estado="error", hasta=datetime.now(timezone.utc).isoformat(),
                                detalle=f"{type(e).__name__}: {e}")
        raise
    resumen = {"nuevos": c["ok"], "sin_cambios": c.get("igual", 0),
               "descartados": c["skip"], "errores": c["err"],
               "conversaciones": c.get("conversaciones")}
    _backfill_estado.update(estado="listo", hasta=datetime.now(timezone.utc).isoformat(),
                            resultado=resumen)
    return resumen


async def correr_backfill_una_vez(origen: str) -> dict:
    """Reserva el turno y corre la pasada. Es la puerta para quien la espera de verdad."""
    if not _marcar_backfill_iniciado(origen):
        raise RuntimeError("Ya hay una pasada en curso")
    return await _correr_backfill(origen)


SQL_ULTIMA_PASADA = """
SELECT corrida_at, origen, conversaciones, ultima_actividad,
       nuevos, sin_cambios, descartados, errores, fallo
  FROM pasadas_backfill
 ORDER BY corrida_at DESC
 LIMIT 1
"""


async def estado_fuente(conn) -> dict:
    """¿Sigue entrando algo por WhatsApp, o el dashboard esta mostrando una foto congelada?

    Una sola definicion de «muda» para la pantalla y para el correo: si cada uno tuviera la
    suya, el dia que discrepen nadie sabria a cual creer.
    """
    fila = await conn.fetchrow(SQL_ULTIMA_PASADA)
    if not fila:
        # Todavia no ha corrido ninguna pasada completa (recien desplegado). No saberlo no es
        # lo mismo que estar muda: alarmar aqui seria un falso positivo garantizado en cada
        # despliegue, y la primera alerta que llega siendo mentira ya no se cree ninguna.
        return {"conocido": False, "muda": False, "umbral_horas": FUENTE_MUDA_HORAS,
                "ultima_actividad": None, "horas_sin_actividad": None, "ultima_pasada": None,
                "origen": None, "conversaciones": None, "nuevos": None, "fallo": None}

    ua = fila["ultima_actividad"]
    horas = (datetime.now(timezone.utc) - ua).total_seconds() / 3600 if ua else None
    return {
        "conocido": True,
        "ultima_actividad": ua.isoformat() if ua else None,
        "horas_sin_actividad": round(horas, 1) if horas is not None else None,
        # Sin ultima_actividad y sin fallo significa que el inbox se leyo entero y no habia ni
        # una conversacion: no es un dia tranquilo, es que la fuente no esta.
        "muda": ua is None or horas >= FUENTE_MUDA_HORAS,
        "umbral_horas": FUENTE_MUDA_HORAS,
        "ultima_pasada": fila["corrida_at"].isoformat(),
        "origen": fila["origen"],
        "conversaciones": fila["conversaciones"],
        "nuevos": fila["nuevos"],
        "fallo": fila["fallo"],
    }


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

    from app.alertas import revisar_fuente, revisar_y_avisar
    from app.score_leads import run as correr_scoring
    from app.sync_planes import sincronizar

    while True:
        await asyncio.sleep(_segundos_hasta(SYNC_PLANES_HORA))

        # Sin ANTHROPIC_API_KEY no hay extraccion posible; se salta en vez de fallar 583 veces.
        if os.getenv("ANTHROPIC_API_KEY"):
            try:
                r = await correr_backfill_una_vez("diaria")
                print(f"[backfill] nuevos/cambiados={r['nuevos']} sin-cambios={r['sin_cambios']} "
                      f"descartados={r['descartados']} errores={r['errores']}", flush=True)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — Chatwoot caido, cuota de Claude...
                print(f"[backfill] fallo: {e}", flush=True)

        # Va JUSTO despues del backfill para leer la fila que este acaba de anotar. Avisa de lo
        # que ninguna otra alerta puede ver: que no entra nada. Las demas miran leads, y cuando
        # la fuente se calla no hay ningun lead nuevo del que sospechar — el dashboard se queda
        # con la foto de ayer y todo parece normal. Ya paso: tres dias congelado sin un aviso.
        try:
            async with db.get_pool().acquire() as conn:
                f = await revisar_fuente(conn)
            print(f"[fuente] {f['resumen']}", flush=True)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — SMTP caido, tabla recien creada...
            print(f"[fuente] fallo: {e}", flush=True)

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

    # Historial de pasadas del backfill. `ultima_actividad` es la razon de ser de la tabla: la
    # ultima vez que alguien escribio en el inbox de Chatwoot. Ninguna columna de leads_dataset
    # responde eso — updated_at lo mueven el scoring y el sync de planes cada mañana aunque no
    # entre un solo mensaje, y captured_at solo cambia con leads nuevos. Sin este dato, una
    # fuente muerta y un dia tranquilo se ven exactamente igual desde el dashboard.
    await pool.execute(
        """
        CREATE TABLE IF NOT EXISTS pasadas_backfill (
          id               bigserial PRIMARY KEY,
          corrida_at       timestamptz NOT NULL DEFAULT now(),
          origen           text NOT NULL,
          conversaciones   integer,
          ultima_actividad timestamptz,
          nuevos           integer,
          sin_cambios      integer,
          descartados      integer,
          errores          integer,
          fallo            text
        )
        """
    )
    # Solo se consulta la ultima, y siempre por fecha: el indice descendente la deja en O(1)
    # por mucho que crezca el historial.
    await pool.execute(
        "CREATE INDEX IF NOT EXISTS idx_pasadas_backfill_at ON pasadas_backfill (corrida_at DESC)"
    )

    # Antiduplicado de las alertas de sistema. Espejo de lead_alertas pero SIN clave ajena: la
    # fuente muda no es el problema de ningun lead concreto, y colgarla de uno la borraria el
    # dia que ese lead desapareciera.
    await pool.execute(
        """
        CREATE TABLE IF NOT EXISTS sistema_alertas (
          tipo       text NOT NULL,
          clave      text NOT NULL,
          enviada_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (tipo, clave)
        )
        """
    )
    # Hitos del embudo, uno por lead y estado, CON SU FECHA. Las etiquetas dicen el estado
    # actual del lead pero no cuando llego a el, y el Scoreboard semanal pregunta justo lo
    # contrario: cuantas demos se agendaron ESTA semana. Filtrar por captured_at no lo
    # responde — el 97% de los leads etiquetados se etiquetaron en una semana distinta a la
    # de su captura, asi que los dos numeros no se parecen.
    #
    # UNIQUE(lead_id, evento): un lead no puede tener dos veces «demo agendada», igual que no
    # puede llevar la etiqueta dos veces. Si algun dia hay que contar dos demos al mismo lead,
    # esta es la restriccion que habra que levantar.
    await pool.execute(
        """
        CREATE TABLE IF NOT EXISTS lead_eventos (
          id        bigserial PRIMARY KEY,
          lead_id   varchar(255) NOT NULL REFERENCES leads_dataset(lead_id) ON DELETE CASCADE,
          evento    text NOT NULL,
          fecha     date NOT NULL,
          autor     text NOT NULL,
          creado_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE (lead_id, evento)
        )
        """
    )
    # El Scoreboard agrupa por evento dentro de un rango de fechas: este indice es su consulta.
    await pool.execute(
        "CREATE INDEX IF NOT EXISTS idx_lead_eventos_semana ON lead_eventos (evento, fecha)"
    )
    # Siembra los eventos de las etiquetas que ya estaban puestas. Sin esto el Scoreboard
    # arrancaria vacio y las semanas pasadas dirian cero, que se lee como «no hubo demos» en
    # vez de «no se registraba». La fecha es la mejor disponible: el ultimo cambio manual
    # (outcome_date) y la captura como respaldo. autor='migracion' las deja identificables,
    # porque son aproximadas y quien lea el Scoreboard tiene derecho a saberlo.
    await pool.execute(
        """
        INSERT INTO lead_eventos (lead_id, evento, fecha, autor)
        SELECT l.lead_id, t,
               (COALESCE(l.outcome_date, l.captured_at) AT TIME ZONE $2::text)::date,
               'migracion'
          FROM leads_dataset l, unnest(l.outcome_tags) AS t
         WHERE t = ANY($1::text[])
           AND COALESCE(l.outcome_date, l.captured_at) IS NOT NULL
        ON CONFLICT (lead_id, evento) DO NOTHING
        """,
        TAG_GROUPS["estado"], LOCAL_TZ,
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

    # Como escribe y consulta el lead, del extractor (app/backfill.py). Son dos dimensiones
    # del reparto por afinidad; se rellenan hacia atras con app/estilo_leads.py.
    await pool.execute(
        """
        ALTER TABLE leads_dataset
          ADD COLUMN IF NOT EXISTS estilo_consulta text,
          ADD COLUMN IF NOT EXISTS dominio_tecnico text
        """
    )

    # Historial de repartos (app/reparto.py). Existe por dos razones. Una: el cursor de
    # rotacion de la serpiente — que vendedor abrio el ultimo lote — sale de aqui, asi que
    # no hace falta una tabla de ajustes. Dos: la etiqueta de responsable dice quien lleva
    # el lead HOY, pero se sobrescribe, y con ella se pierde quien lo recibio, cuando y en
    # que puesto del ranking. Sin eso no se puede evaluar despues si el reparto funciono.
    await pool.execute(
        """
        CREATE TABLE IF NOT EXISTS reparto_lotes (
          id          bigserial PRIMARY KEY,
          metodo      text NOT NULL,
          creado_at   timestamptz NOT NULL DEFAULT now(),
          autor       text NOT NULL,
          inicio      integer NOT NULL,
          abrio       text,
          vendedores  text[] NOT NULL DEFAULT '{}',
          capacidad   integer,
          leads       integer NOT NULL DEFAULT 0,
          sin_asignar integer NOT NULL DEFAULT 0
        )
        """
    )
    await pool.execute(
        """
        CREATE TABLE IF NOT EXISTS reparto_asignaciones (
          id          bigserial PRIMARY KEY,
          lote_id     bigint NOT NULL REFERENCES reparto_lotes(id) ON DELETE CASCADE,
          lead_id     varchar(255) NOT NULL REFERENCES leads_dataset(lead_id) ON DELETE CASCADE,
          vendedor    text NOT NULL,
          ronda       integer NOT NULL,
          posicion    integer NOT NULL,
          score       real,
          asignado_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    await pool.execute(
        "CREATE INDEX IF NOT EXISTS idx_reparto_asig_lote ON reparto_asignaciones (lote_id)"
    )
    # Por lead y fecha: es la consulta de «desde cuando lo tiene», que es la base del SLA.
    await pool.execute(
        "CREATE INDEX IF NOT EXISTS idx_reparto_asig_lead "
        "ON reparto_asignaciones (lead_id, asignado_at DESC)"
    )
    # Lo que hace evaluable el metodo 2: la afinidad EN EL MOMENTO del reparto (el perfil
    # cambia despues) y el brazo que decidio ese lead. Hoy brazo = metodo del lote; cuando
    # haya brazo aleatorio por lead, sera por asignacion y esta columna es la que lo guarda.
    await pool.execute(
        """
        ALTER TABLE reparto_asignaciones
          ADD COLUMN IF NOT EXISTS afinidad real,
          ADD COLUMN IF NOT EXISTS brazo    text
        """
    )

    # Perfil de afinidad de cada vendedor (metodo 2, app/afinidad.py): con que clientes
    # trabaja mejor, dimension por dimension, de 0 a 1. Lo rellena el administrador con las
    # respuestas del propio vendedor. `origen` esta en la clave primaria para que, cuando se
    # calcule desde sus conversaciones cerradas, conviva con lo que se puso a mano sin pisarlo.
    await pool.execute(
        """
        CREATE TABLE IF NOT EXISTS vendedor_perfil (
          vendedor        text NOT NULL,
          dimension       text NOT NULL,
          clave           text NOT NULL,
          afinidad        real NOT NULL CHECK (afinidad >= 0 AND afinidad <= 1),
          origen          text NOT NULL DEFAULT 'manual',
          actualizado_at  timestamptz NOT NULL DEFAULT now(),
          actualizado_por text,
          PRIMARY KEY (vendedor, dimension, clave, origen)
        )
        """
    )
    # Ajustes por vendedor que no son afinidad: el tope de leads abiertos (NULL = el global
    # REPARTO_CAPACIDAD; 0 = sin tope) y notas libres. Va aparte porque es UN valor por
    # persona, no uno por dimension.
    await pool.execute(
        """
        CREATE TABLE IF NOT EXISTS vendedor_ajustes (
          vendedor        text PRIMARY KEY,
          capacidad       integer CHECK (capacidad IS NULL OR capacidad >= 0),
          notas           text,
          actualizado_at  timestamptz NOT NULL DEFAULT now(),
          actualizado_por text
        )
        """
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

    # Quien entra en el reparto, dicho en voz alta al arrancar: un nombre mal escrito aqui
    # deja a esa persona sin su parte del lote y no lo notaria nadie hasta contar los leads.
    equipo = equipo_reparto()
    if (ajenos := sorted(set(REPARTO_VENDEDORES) - set(TAG_GROUPS["responsable"]))):
        print(f"[reparto] AVISO: {', '.join(ajenos)} no son etiquetas de responsable validas "
              f"y se ignoran; revisa REPARTO_VENDEDORES", flush=True)
    fuera = [v for v in TAG_GROUPS["responsable"] if v not in equipo]
    print(f"[reparto] {reparto.METODO} entre {', '.join(equipo) or '(nadie: no se puede repartir)'}"
          + (f" | fuera del reparto: {', '.join(fuera)}" if fuera else "")
          + (f" | tope {REPARTO_CAPACIDAD} abiertos" if REPARTO_CAPACIDAD else " | sin tope"),
          flush=True)

    from app.alertas import estado_config
    print(f"[alertas] {estado_config()}", flush=True)

    yield

    if tarea_sync:
        tarea_sync.cancel()
    await db.close_pool()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)


@app.middleware("http")
async def no_indexar(request, call_next):
    """Marca cada respuesta como no indexable.

    El meta del HTML solo protege al HTML; esta cabecera tambien cubre las respuestas
    JSON de /leads y compania, que un buscador puede alcanzar igual de bien. Y a
    diferencia de robots.txt, que solo pide no rastrear, noindex pide no publicar:
    una URL enlazada desde fuera puede acabar en el indice sin haberse rastreado nunca.

    Nada de esto sustituye a poner autenticacion delante del HTML; son capas distintas.
    """
    respuesta = await call_next(request)
    respuesta.headers["X-Robots-Tag"] = "noindex, nofollow"
    return respuesta


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


class EventoBody(BaseModel):
    # Solo la fecha: el hito existe porque existe la etiqueta, y el autor sale de la sesion.
    fecha: str   # YYYY-MM-DD


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


class _Estaticos(StaticFiles):
    """CSS y JS del dashboard, sin cache en el navegador.

    No hay paso de build ni versionado en las URLs: tras un despliegue, el equipo seguiria
    viendo el CSS/JS anterior hasta vaciar la cache. Con no-cache el navegador revalida
    con el ETag en cada carga, que es barato y siempre trae lo ultimo.
    """

    def file_response(self, *args, **kwargs):
        respuesta = super().file_response(*args, **kwargs)
        respuesta.headers["Cache-Control"] = "no-cache"
        return respuesta


app.mount("/static", _Estaticos(directory=STATIC_DIR), name="static")


@app.get("/robots.txt")
async def robots():
    """Le pide a los rastreadores que no entren a ninguna ruta."""
    return PlainTextResponse("User-agent: *\nDisallow: /\n")


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
          -- Pendientes de etiquetar: sin etiquetas Y con conversacion. El transcript no es un
          -- adorno del filtro, es la condicion para poder etiquetar — sin conversacion que
          -- leer no hay nada que decidir, y la cola de etiquetado ya los excluye. Contarlos
          -- aqui hacia que la tarjeta dijera 399 y la cola mostrara 186: 213 de diferencia,
          -- justo los leads sin transcript.
          COUNT(*) FILTER (WHERE cardinality(outcome_tags) = 0
                             AND transcript IS NOT NULL)            AS sin_etiquetas,
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
    # (el pendiente de etiquetar se cuenta aparte, en sin_etiquetas, que ademas solo
    # cuenta los que tienen conversacion — los unicos que se pueden etiquetar).
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
        # Tampoco lleva el filtro de fecha: la frescura es del sistema entero, no del rango
        # que se este mirando. Acotar agosto no puede hacer que unos datos viejos parezcan
        # recientes, que es justo el espejismo que este bloque viene a evitar.
        "fuente": await estado_fuente(pool),
    }


@app.get("/api/scoreboard")
async def scoreboard(
    desde: Optional[str] = Query(None),
    hasta: Optional[str] = Query(None),
    _=Depends(check_auth),
):
    """Cuantos hitos del embudo OCURRIERON en el rango. No es lo mismo que /api/stats.

    /api/stats responde «de los leads que ENTRARON en este periodo, cuantos son X», y filtra
    por captured_at. Esto responde «cuantas demos se agendaron ESTA semana», sin importar
    cuando llego el lead, y filtra por la fecha del hito. Son dos preguntas distintas, y de ahi
    que sean dos endpoints: el 97% de los leads etiquetados se etiquetaron en una semana
    distinta a la de su captura, asi que confundirlas da numeros que no se parecen.

    `aproximados` cuenta los hitos que vienen de la migracion, cuya fecha es una estimacion
    (el ultimo cambio manual, o la captura). Se devuelve para que la pantalla pueda advertirlo
    en vez de presentar como exacto un numero que no lo es.
    """
    cond, args = [], []
    for raw, op in ((desde, ">="), (hasta, "<=")):
        if raw:
            args.append(_dia_iso(raw))
            cond.append(f"fecha {op} ${len(args)}")
    where = ("WHERE " + " AND ".join(cond)) if cond else ""

    filas = await db.get_pool().fetch(
        f"""SELECT evento, COUNT(*) AS n,
                   COUNT(*) FILTER (WHERE autor = 'migracion') AS aprox
              FROM lead_eventos {where} GROUP BY evento""",
        *args,
    )

    # Los leads nuevos SI se cuentan por fecha de captura: ese es su hito, el momento en que
    # entraron. Es la unica fila del Scoreboard que no sale de lead_eventos.
    cond_cap, args_cap = ["is_test = false"], []
    _rango_captura(desde, hasta, cond_cap, args_cap)
    nuevos = await db.get_pool().fetchval(
        "SELECT COUNT(*) FROM leads_dataset WHERE " + " AND ".join(cond_cap), *args_cap
    )

    return {
        "desde": desde, "hasta": hasta,
        "leads_nuevos": nuevos,
        "por_evento": {r["evento"]: r["n"] for r in filas},
        "aproximados": {r["evento"]: r["aprox"] for r in filas if r["aprox"]},
        "orden": list(TAG_GROUPS["estado"]),
    }


def _filtros_leads(
    q: Optional[str], tags: Optional[str], sin_etiquetas: Optional[str],
    plan_estado: Optional[str], outcome: Optional[str], qualified: Optional[str],
    has_transcript: Optional[str], seguimiento: Optional[str],
    desde: Optional[str], hasta: Optional[str],
) -> tuple[str, list]:
    """Traduce los filtros de la barra a un WHERE con sus argumentos.

    Vive aparte porque lo usan la tabla y la exportacion a CSV, y el CSV solo sirve si
    dice exactamente lo mismo que la pantalla. Si cada uno armara su propio WHERE, el dia
    que se toque un filtro se tocaria uno solo y nadie lo notaria: el CSV seguiria
    descargandose, con otras filas.
    """
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

    return "WHERE " + " AND ".join(conditions), args


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
    where, args = _filtros_leads(q, tags, sin_etiquetas, plan_estado, outcome,
                                 qualified, has_transcript, seguimiento, desde, hasta)
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


# Columnas del CSV: (cabecera, columna SQL, formateador). El orden es el de la tabla en
# pantalla, y detras van los campos que la tabla no muestra pero que hacen falta para
# cruzar con HubSpot o Meta Ads (RUC, industria, canal, fechas del plan).
EXPORT_COLUMNAS: list[tuple[str, str, str]] = [
    ("telefono",             "lead_id",              "telefono"),
    ("nombre",               "contact_name",         "texto"),
    ("empresa",              "company_name",         "texto"),
    ("correo",               "email",                "texto"),
    ("ruc",                  "tax_id",               "texto"),
    ("industria",            "industry",             "texto"),
    ("segmento",             "segmento",             "texto"),
    ("tipo_lead",            "tipo_lead",            "texto"),
    ("calificado",           "qualified",            "si_no"),
    ("prob_conversion_pct",  "conversion_prob",      "porcentaje"),
    ("estado",               "outcome",              "texto"),
    ("etiquetas",            "outcome_tags",         "lista"),
    ("plan_estado",          "plan_estado",          "texto"),
    ("plan_nombre",          "plan_nombre",          "texto"),
    ("plan_inicia",          "plan_inicia",          "fecha_local"),
    ("plan_expira",          "plan_expira",          "fecha_local"),
    ("siguiente_paso",       "siguiente_paso",       "texto"),
    ("siguiente_paso_fecha", "siguiente_paso_fecha", "fecha"),
    ("canal",                "canal",                "texto"),
    ("mensajes",             "message_count",        "texto"),
    ("fecha_captura",        "captured_at",          "fecha_hora_local"),
]


def _csv_valor(valor, formato: str) -> str:
    """Un valor de Postgres -> el texto que va en la celda."""
    if valor is None:
        return ""
    if formato == "telefono":
        # lead_id ES el numero; la tabla lo pinta con "+" y el CSV hace lo mismo para que
        # un telefono peruano no pierda el prefijo al abrirse en una hoja de calculo.
        return "+" + str(valor)
    if formato == "si_no":
        return "Sí" if valor else "No"
    if formato == "porcentaje":
        # Entero, como se lee en pantalla: 0.72 -> 72. Una hoja de calculo lo suma y lo
        # ordena; "72%" como texto, no.
        return str(round(float(valor) * 100))
    if formato == "lista":
        # "|" y no "," ni ";": ambos son separadores de CSV en algun Excel, y una etiqueta
        # partida en dos columnas es un error que nadie ve hasta que ya importo los datos.
        return "|".join(str(v) for v in valor)
    if formato == "fecha":
        return valor.isoformat()
    if formato in ("fecha_local", "fecha_hora_local"):
        # timestamptz -> hora del negocio. En UTC, un lead de las 20:00 de Lima aparece al
        # dia siguiente, y al cruzar con Meta Ads las fechas dejan de coincidir.
        local = valor.astimezone(_tz_local())
        return local.strftime("%Y-%m-%d" if formato == "fecha_local" else "%Y-%m-%d %H:%M")
    return str(valor)


# Cuantas filas se juntan antes de mandarlas. Suficiente para no soltar un paquete por
# lead, pequeño para que la descarga empiece de inmediato y no cargue el CSV entero en RAM.
EXPORT_LOTE = 200


# OJO: esta ruta va ANTES de /api/leads/{lead_id}. FastAPI resuelve por orden de registro,
# asi que si se mueve mas abajo, "export.csv" entraria como un lead_id y devolveria 404.
@app.get("/api/leads/export.csv")
async def export_leads_csv(
    q: Optional[str] = Query(None),
    tags: Optional[str] = Query(None),
    sin_etiquetas: Optional[str] = Query(None),
    plan_estado: Optional[str] = Query(None),
    outcome: Optional[str] = Query(None),
    qualified: Optional[str] = Query(None),
    has_transcript: Optional[str] = Query(None),
    seguimiento: Optional[str] = Query(None),
    desde: Optional[str] = Query(None),
    hasta: Optional[str] = Query(None),
    sort: str = Query("recientes"),
    _=Depends(check_auth),
):
    """La tabla de leads filtrada, entera, en CSV.

    Sin limit ni offset a proposito: exportar "lo que veo" es el conjunto filtrado
    completo, no los 50 de la pagina abierta. Un CSV de 50 filas parece correcto y no lo
    es, que es la peor forma de estar mal.

    Se envia en streaming con un cursor del servidor para que el numero de leads no sea el
    limite: ni el proceso junta la tabla entera en memoria, ni el navegador espera a que
    termine para empezar a recibir.
    """
    where, args = _filtros_leads(q, tags, sin_etiquetas, plan_estado, outcome,
                                 qualified, has_transcript, seguimiento, desde, hasta)
    columnas = ", ".join(col for _, col, _f in EXPORT_COLUMNAS)
    sql = (f"SELECT {columnas} FROM leads_dataset {where} "
           f"ORDER BY {ORDER_BY.get(sort, ORDER_BY['recientes'])}")

    async def generar():
        buf = io.StringIO()
        escritor = csv.writer(buf)   # el lineterminator por defecto ya es CRLF

        def vaciar() -> bytes:
            datos = buf.getvalue().encode("utf-8")
            buf.seek(0)
            buf.truncate(0)
            return datos

        # BOM: sin el, Excel en Windows abre el CSV como ANSI y "Compañía" sale rota.
        # Los importadores de HubSpot y Meta lo toleran; Excel sin BOM, no.
        yield codecs.BOM_UTF8
        escritor.writerow([cab for cab, _c, _f in EXPORT_COLUMNAS])
        yield vaciar()

        async with db.get_pool().acquire() as con:
            async with con.transaction():   # el cursor de asyncpg exige transaccion
                pendientes = 0
                async for fila in con.cursor(sql, *args):
                    escritor.writerow([
                        _csv_valor(fila[col], fmt) for _cab, col, fmt in EXPORT_COLUMNAS
                    ])
                    pendientes += 1
                    if pendientes >= EXPORT_LOTE:
                        pendientes = 0
                        yield vaciar()
                if pendientes:
                    yield vaciar()

    nombre = f"leads-{hoy_local().isoformat()}.csv"
    return StreamingResponse(
        generar(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{nombre}"'},
    )


@app.get("/api/leads/{lead_id}")
async def get_lead(lead_id: str, _=Depends(check_auth)):
    pool = db.get_pool()
    row = await pool.fetchrow("SELECT * FROM leads_dataset WHERE lead_id = $1", lead_id)
    if not row:
        raise HTTPException(status_code=404, detail="Lead not found")
    return db.row_to_dict(row)


# Solo las etiquetas de ESTADO son hitos del embudo. El responsable y el canal describen al
# lead, no algo que le haya pasado un dia concreto, asi que no generan evento.
EVENTOS_EMBUDO = frozenset(TAG_GROUPS["estado"])


async def sincronizar_eventos(conn: asyncpg.Connection, lead_id: str,
                              tags: list[str], autor: str) -> None:
    """Refleja el cambio de etiquetas en lead_eventos: un hito fechado por cada estado.

    La fecha se pone en HOY, que es lo correcto en el uso normal: se marca «demo agendada» el
    dia que se agenda y «demo realizada» el dia que ocurre, asi que cada numero cae en su
    semana. Para el etiquetado retroactivo se corrige despues (PATCH .../eventos/{evento}).

    ON CONFLICT DO NOTHING es lo que hace que volver a guardar el mismo conjunto de etiquetas
    no reinicie las fechas: sin eso, tocar cualquier etiqueta moveria todas las demas a hoy y
    el Scoreboard de la semana pasada cambiaria solo.

    Quitar una etiqueta borra su evento, a proposito: si se marco por error, el Scoreboard
    tiene que dejar de contarlo. Un numero que no se puede corregir no se usa.
    """
    quedan = [t for t in tags if t in EVENTOS_EMBUDO]
    await conn.execute(
        "DELETE FROM lead_eventos WHERE lead_id = $1 AND NOT (evento = ANY($2::text[]))",
        lead_id, quedan,
    )
    if quedan:
        await conn.executemany(
            "INSERT INTO lead_eventos (lead_id, evento, fecha, autor) VALUES ($1, $2, $3, $4) "
            "ON CONFLICT (lead_id, evento) DO NOTHING",
            [(lead_id, e, hoy_local(), autor) for e in quedan],
        )


@app.patch("/api/leads/{lead_id}/outcome")
async def update_outcome(lead_id: str, body: OutcomeBody, usuario: str = Depends(check_auth)):
    """Reemplaza el conjunto de etiquetas del lead y recalcula su estado principal."""
    if body.tags is not None:
        crudas = body.tags
    elif body.outcome in (None, "nuevo"):  # 'nuevo' legado = sin etiquetas
        crudas = []
    else:  # formato legado de un solo valor
        crudas = [OUTCOME_LEGACY.get(body.outcome, body.outcome)]
    tags = normalize_tags(crudas)

    # Las etiquetas y sus eventos van en la misma transaccion: si divergieran, el Scoreboard
    # contaria una demo que el lead ya no tiene, o al contrario, y no habria forma de saberlo.
    pool = db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await conn.execute(
                "UPDATE leads_dataset SET outcome_tags=$1, outcome=$2, outcome_date=NOW(), "
                "outcome_source='manual', updated_at=NOW() WHERE lead_id=$3",
                tags,
                primary_outcome(tags),
                lead_id,
            )
            if result == "UPDATE 0":
                raise HTTPException(status_code=404, detail="Lead not found")
            await sincronizar_eventos(conn, lead_id, tags, usuario)
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


@app.get("/api/leads/{lead_id}/eventos")
async def listar_eventos(lead_id: str, _=Depends(check_auth)):
    """Hitos del embudo de este lead con su fecha. Es el detalle de lo que suma el Scoreboard."""
    rows = await db.get_pool().fetch(
        "SELECT evento, fecha, autor, creado_at FROM lead_eventos WHERE lead_id = $1 "
        "ORDER BY fecha, evento",
        lead_id,
    )
    return {"eventos": [db.row_to_dict(r) for r in rows]}


@app.patch("/api/leads/{lead_id}/eventos/{evento}")
async def corregir_evento(lead_id: str, evento: str, body: EventoBody,
                          usuario: str = Depends(check_auth)):
    """Corrige la FECHA de un hito ya registrado, moviendolo de semana.

    Existe porque el hito se fecha en hoy al marcar la etiqueta, y eso falla en el caso
    retroactivo: quien el viernes etiqueta las demos de toda la semana las mandaria todas al
    viernes y descuadraria el Scoreboard. Tambien sirve para la demo que se agenda hoy y se
    realiza la semana que viene.
    """
    if evento not in EVENTOS_EMBUDO:
        raise HTTPException(status_code=400, detail=f"'{evento}' no es un hito del embudo")
    result = await db.get_pool().execute(
        "UPDATE lead_eventos SET fecha = $1, autor = $2 WHERE lead_id = $3 AND evento = $4",
        _dia_iso(body.fecha), usuario, lead_id, evento,
    )
    if result == "UPDATE 0":
        raise HTTPException(status_code=404, detail="Ese lead no tiene ese hito registrado")
    return {"ok": True, "evento": evento, "fecha": body.fecha}


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


@app.post("/api/backfill", status_code=202)
async def backfill_ahora(_=Depends(check_auth)):
    """Trae de Chatwoot las conversaciones nuevas sin esperar a la pasada de las 07:00.

    Devuelve 202 y sigue en segundo plano: el barrido tarda minutos (cientos de conversaciones,
    la paginacion de mensajes y una llamada a Claude por cada una que cambio), muchisimo mas de
    lo que aguanta una peticion HTTP. El avance se consulta en /api/backfill/estado.

    Existe porque hasta ahora, si a media tarde faltaba un lead con el que se acababa de hablar,
    no habia nada que pulsar: tocaba esperar a la mañana siguiente.
    """
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise HTTPException(status_code=503, detail="Falta ANTHROPIC_API_KEY: no hay extraccion posible")
    # Reservar ANTES de crear la tarea: asi la respuesta ya sale diciendo «corriendo» y el
    # dashboard no puede sondear el estado antes de que la pasada se haya marcado.
    if not _marcar_backfill_iniciado("manual"):
        raise HTTPException(status_code=409, detail="Ya hay una actualizacion en curso")

    global _backfill_tarea

    async def _lanzar():
        try:
            await _correr_backfill("manual")
        except Exception as e:  # noqa: BLE001 — queda en _backfill_estado, que es quien lo cuenta
            print(f"[backfill/manual] fallo: {e}", flush=True)

    # Nadie espera esta tarea: su resultado vive en _backfill_estado y se consulta aparte. La
    # referencia en _backfill_tarea no es decorativa, evita que el recolector se la lleve.
    _backfill_tarea = asyncio.create_task(_lanzar())
    return backfill_estado()


@app.get("/api/backfill/estado")
async def backfill_progreso(_=Depends(check_auth)):
    """Como va la pasada lanzada a mano. El dashboard lo consulta cada pocos segundos."""
    return backfill_estado()


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


# ══════════ Reparto de leads entre vendedores ══════════
# El ranking dice a quien atender primero; el reparto dice QUIEN lo atiende. El metodo y su
# justificacion viven en app/reparto.py; aqui solo esta el cableado con la base de datos.
#
# Tope de leads abiertos por vendedor. 0 = sin tope, y es el defecto a proposito: inventar
# un numero aqui haria que el sistema dejara leads sin repartir por una cifra que nadie ha
# medido. Se sube cuando el equipo sepa cuantos puede llevar cada uno de verdad.
REPARTO_CAPACIDAD = int(os.getenv("REPARTO_CAPACIDAD", "0"))

# Quien entra en el reparto automatico. Es un SUBCONJUNTO de TAG_GROUPS["responsable"], y la
# distincion importa: llevar un lead y estar en la cola de reparto no son lo mismo. Jhon es
# administrador, no vendedor — puede quedarse con un lead puntual (por eso sigue siendo una
# etiqueta valida), pero no le toca su parte del lote.
# El defecto son los vendedores de hoy; la variable existe para cuando el equipo crezca.
# OJO: sumar a alguien nuevo pide ademas anadirlo a TAG_GROUPS aqui y en app/static/app.js,
# porque de ahi salen la etiqueta, su color y los filtros.
REPARTO_VENDEDORES = [v.strip().lower() for v in
                      os.getenv("REPARTO_VENDEDORES", "alyssa,diego").split(",") if v.strip()]

# Un lead cerrado ya no se reparte: no queda nada que trabajar en el.
OUTCOME_CERRADO = ("cliente", "perdido")


class RepartoItem(BaseModel):
    lead_id: str
    vendedor: str
    ronda: int = 1
    posicion: int = 0
    afinidad: Optional[float] = None   # solo con metodo afinidad: a(lead, vendedor) vista


METODOS = (reparto.METODO, afinidad.METODO)


class RepartoBody(BaseModel):
    # El cliente devuelve el reparto que VIO en el preview, no unos filtros para recalcular:
    # entre mirar y aplicar pueden entrar leads nuevos, y nadie debe aplicar a ciegas algo
    # distinto de lo que aprobo. El servidor revalida cada lead antes de tocarlo.
    asignaciones: list[RepartoItem]
    metodo: str = reparto.METODO
    inicio: int = 0
    capacidad: Optional[int] = None


def equipo_reparto() -> list[str]:
    """Los vendedores del reparto, en el orden de TAG_GROUPS: el reparto debe ser reproducible.

    Se cruza con TAG_GROUPS a proposito: un nombre en REPARTO_VENDEDORES que no sea una
    etiqueta valida no podria escribirse en el lead, asi que repartirle seria mentir.
    """
    return [v for v in TAG_GROUPS["responsable"] if v in REPARTO_VENDEDORES]


def _vendedores_validos(crudos: Optional[list[str]]) -> list[str]:
    """Filtra contra el equipo de reparto y conserva su orden."""
    equipo = equipo_reparto()
    if not crudos:
        return list(equipo)
    elegidos = {v.strip().lower() for v in crudos if v.strip()}
    if (ajenos := sorted(elegidos - set(equipo))):
        raise HTTPException(status_code=400,
                            detail=f"No entran en el reparto: {', '.join(ajenos)}")
    return [v for v in equipo if v in elegidos]


async def _carga_abierta(conn, vendedores: list[str]) -> dict[str, int]:
    """Leads que cada vendedor tiene AHORA sin cerrar. Es la carga real, no la historica."""
    filas = await conn.fetch(
        """
        SELECT t AS vendedor, count(*) AS abiertos
          FROM leads_dataset l, unnest(l.outcome_tags) AS t
         WHERE t = ANY($1::text[]) AND l.is_test = false
           AND l.outcome <> ALL($2::text[])
         GROUP BY t
        """,
        vendedores, list(OUTCOME_CERRADO),
    )
    abiertos = {f["vendedor"]: f["abiertos"] for f in filas}
    return {v: abiertos.get(v, 0) for v in vendedores}


async def _cursor_reparto(conn, vendedores: list[str]) -> int:
    """Quien abre este lote: el siguiente al que abrio el anterior.

    Se guarda el NOMBRE de quien abrio, no solo su indice, porque el equipo puede cambiar
    entre lotes (alguien de vacaciones sale de la lista) y un indice suelto acabaria
    apuntando a otra persona. Si quien abrio ya no esta, se empieza por el primero.
    """
    fila = await conn.fetchrow(
        "SELECT abrio FROM reparto_lotes ORDER BY creado_at DESC, id DESC LIMIT 1")
    if not fila or fila["abrio"] not in vendedores:
        return 0
    return (vendedores.index(fila["abrio"]) + 1) % len(vendedores)


async def _candidatos_reparto(conn, limite: int) -> list[dict]:
    """Los leads que toca repartir, mejor puntuado primero.

    Se excluyen los que YA tienen responsable: reasignar por lotes le quitaria a alguien un
    lead que quiza ya trabajo, y ninguna cartera seria estable. Sin conversion_prob no hay
    ranking que repartir, asi que esos tampoco entran — primero hay que puntuarlos.
    """
    filas = await conn.fetch(
        """
        SELECT lead_id, contact_name, company_name, wa_display_name, conversion_prob,
               outcome, outcome_tags, captured_at,
               -- Features del lead: sin ellas el reparto por afinidad no tiene con que
               -- emparejar y sale neutro para todo el mundo (ver app/afinidad.py).
               segmento, modulos_interes, num_rucs, volumen_comprobantes,
               objecion, solucion_actual, estilo_consulta, dominio_tecnico
          FROM leads_dataset
         WHERE is_test = false
           AND conversion_prob IS NOT NULL
           AND outcome <> ALL($1::text[])
           AND NOT (outcome_tags && $2::text[])
         ORDER BY conversion_prob DESC, lead_id
         LIMIT $3
        """,
        list(OUTCOME_CERRADO), TAG_GROUPS["responsable"], limite,
    )
    return [db.row_to_dict(f) for f in filas]


@app.get("/api/reparto/preview")
async def reparto_preview(
    limit: int = Query(20, ge=1, le=200),
    vendedores: Optional[str] = Query(None, description="csv; omitir = todo el equipo"),
    capacidad: Optional[int] = Query(None, ge=0, description="tope de leads abiertos; 0 = sin tope"),
    metodo: str = Query(reparto.METODO, description="serpiente | afinidad"),
    _=Depends(check_auth),
):
    """Calcula el reparto SIN aplicarlo. Nadie reparte leads a ciegas."""
    if metodo not in METODOS:
        raise HTTPException(status_code=400, detail=f"Método desconocido: {metodo}")
    equipo = _vendedores_validos(vendedores.split(",") if vendedores else None)

    pool = db.get_pool()
    async with pool.acquire() as conn:
        candidatos = await _candidatos_reparto(conn, limit)
        carga = await _carga_abierta(conn, equipo)
        inicio = await _cursor_reparto(conn, equipo)
        ajustes = await _ajustes(conn, equipo)
        perfiles = await _perfiles(conn, equipo) if metodo == afinidad.METODO else {}

    # Tope por vendedor: el de la peticion, si no el suyo (formulario de Vendedores), si no el
    # global. Huecos libres = tope menos lo que ya lleva abierto. Quien no tiene tope recibe
    # tantos huecos como leads hay: equivale a no limitarlo y permite un solo dict aunque
    # otro vendedor si este limitado.
    topes = _topes(equipo, ajustes, capacidad)
    huecos = ({v: (len(candidatos) if topes[v] == 0 else max(0, topes[v] - carga[v]))
               for v in equipo} if any(topes.values()) else None)
    if metodo == afinidad.METODO:
        plan = afinidad.reparto(candidatos, equipo, perfiles, capacidad=huecos, inicio=inicio)
    else:
        plan = reparto.serpiente(candidatos, equipo, capacidad=huecos, inicio=inicio)

    # Los datos del lead viajan pegados al plan para que la tabla se pinte sin un segundo
    # viaje: el algoritmo por si solo devuelve ids, que no se pueden enseñar a nadie.
    por_id = {c["lead_id"]: c for c in candidatos}
    for a in plan["asignaciones"]:
        a["lead"] = por_id.get(a["lead_id"])
    for f in plan["resumen"]:
        f["abiertos"] = carga[f["vendedor"]]
        f["tope"] = topes[f["vendedor"]]
        f["huecos"] = None if huecos is None else huecos[f["vendedor"]]

    return {
        "metodo": metodo,
        # Con afinidad, quien no tiene perfil es neutro: hay que decirlo, porque si no el
        # reparto parece que lo tuvo en cuenta y no fue asi.
        "sin_perfil": [v for v in equipo if v not in perfiles] if metodo == afinidad.METODO else [],
        "afinidad_media": plan.get("afinidad_media"),
        "beta": plan.get("beta"),
        "equipo": equipo_reparto(),   # todos los que pueden entrar, para pintar sus tarjetas
        "vendedores": equipo,         # los que participan en ESTE lote
        "abre": equipo[plan["inicio"]] if equipo else None,
        "inicio": plan["inicio"],
        "rondas": plan["rondas"],
        "capacidad": REPARTO_CAPACIDAD if capacidad is None else capacidad,
        "topes": topes,               # por vendedor; 0 = sin tope
        "candidatos": len(candidatos),
        "asignaciones": plan["asignaciones"],
        "sin_asignar": plan["sin_asignar"],
        "resumen": plan["resumen"],
        "brecha": reparto.brecha(plan["resumen"]),
    }


@app.post("/api/reparto/aplicar")
async def reparto_aplicar(body: RepartoBody, usuario: str = Depends(check_auth)):
    """Escribe la etiqueta de responsable de cada lead y deja el lote en el historial.

    Revalida lead por lead antes de tocarlo: si entre el preview y el aplicar alguien ya se
    quedo con uno o lo cerro, ese se OMITE y se dice cual. Aplicar el resto es lo correcto —
    anular el lote entero por un lead que cambio dejaria todo el trabajo sin hacer.

    No mueve outcome ni outcome_date: el responsable no es un hito del embudo, y fechar el
    reparto como si lo fuera falsearia el Scoreboard de la semana.
    """
    if not body.asignaciones:
        raise HTTPException(status_code=400, detail="No hay asignaciones que aplicar")
    if body.metodo not in METODOS:
        raise HTTPException(status_code=400, detail=f"Método desconocido: {body.metodo}")
    equipo = _vendedores_validos([a.vendedor for a in body.asignaciones])

    vistos: set[str] = set()
    for a in body.asignaciones:
        if a.lead_id in vistos:
            raise HTTPException(status_code=400, detail=f"Lead repetido en el lote: {a.lead_id}")
        vistos.add(a.lead_id)

    aplicadas: list[dict] = []
    omitidas: list[dict] = []
    pool = db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            for a in body.asignaciones:
                # FOR UPDATE: dos personas dandole a Aplicar a la vez repartirian el mismo
                # lead dos veces, y el segundo pisaria al primero sin que nadie se enterara.
                fila = await conn.fetchrow(
                    "SELECT outcome, outcome_tags, conversion_prob, is_test "
                    "FROM leads_dataset WHERE lead_id = $1 FOR UPDATE", a.lead_id)
                if fila is None:
                    omitidas.append({"lead_id": a.lead_id, "razon": "ya no existe"})
                    continue
                if fila["is_test"]:
                    omitidas.append({"lead_id": a.lead_id, "razon": "es de prueba"})
                    continue
                if fila["outcome"] in OUTCOME_CERRADO:
                    omitidas.append({"lead_id": a.lead_id, "razon": f"ya esta como {fila['outcome']}"})
                    continue
                tags = list(fila["outcome_tags"] or [])
                previo = next((t for t in tags if t in TAG_GROUPS["responsable"]), None)
                if previo:
                    omitidas.append({"lead_id": a.lead_id, "razon": f"ya es de {previo}"})
                    continue
                await conn.execute(
                    "UPDATE leads_dataset SET outcome_tags=$1, updated_at=NOW() WHERE lead_id=$2",
                    normalize_tags(tags + [a.vendedor]), a.lead_id)
                aplicadas.append({"lead_id": a.lead_id, "vendedor": a.vendedor,
                                  "ronda": a.ronda, "posicion": a.posicion,
                                  "score": fila["conversion_prob"],
                                  "afinidad": a.afinidad})

            # El lote se guarda aunque no se haya aplicado nada: que un reparto saliera
            # entero en falso tambien es informacion, y el cursor tiene que avanzar igual
            # para que la rotacion no se quede clavada en la misma persona.
            abre = equipo[body.inicio % len(equipo)] if equipo else None
            lote = await conn.fetchval(
                """
                INSERT INTO reparto_lotes
                       (metodo, autor, inicio, abrio, vendedores, capacidad, leads, sin_asignar)
                VALUES ($1, $2, $3, $4, $5::text[], $6, $7, $8) RETURNING id
                """,
                body.metodo, usuario, body.inicio % max(1, len(equipo)), abre, equipo,
                body.capacidad, len(aplicadas), len(omitidas),
            )
            if aplicadas:
                await conn.executemany(
                    "INSERT INTO reparto_asignaciones "
                    "(lote_id, lead_id, vendedor, ronda, posicion, score, afinidad, brazo) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
                    [(lote, a["lead_id"], a["vendedor"], a["ronda"], a["posicion"], a["score"],
                      a["afinidad"], body.metodo)
                     for a in aplicadas],
                )

    print(f"[reparto] lote {lote} ({body.metodo}) por {usuario}: "
          f"{len(aplicadas)} asignados, {len(omitidas)} omitidos", flush=True)
    return {"ok": True, "lote": lote, "aplicadas": aplicadas, "omitidas": omitidas,
            "resumen": reparto.resumen(aplicadas, equipo)}


@app.get("/api/reparto/historial")
async def reparto_historial(limit: int = Query(20, ge=1, le=100), _=Depends(check_auth)):
    """Ultimos lotes repartidos. Es lo que permite auditar quien recibio que, y cuando."""
    filas = await db.get_pool().fetch(
        """
        SELECT l.*,
               (SELECT count(*) FROM reparto_asignaciones a WHERE a.lote_id = l.id) AS asignados
          FROM reparto_lotes l
         ORDER BY l.creado_at DESC, l.id DESC
         LIMIT $1
        """, limit)
    return {"items": [db.row_to_dict(f) for f in filas]}


# ══════════ Perfil de vendedor (metodo 2: afinidad) ══════════
# Las dimensiones, la validacion y el significado de cada valor viven en app/afinidad.py;
# aqui solo esta el cableado con la base de datos y quien puede tocar que.

class PerfilBody(BaseModel):
    # El formulario manda el perfil ENTERO en la escala 0-10 (reemplazo, no incremento), el
    # tope de leads abiertos (None = usa el global, 0 = sin tope) y notas libres.
    perfil: dict[str, dict[str, Optional[float]]]
    capacidad: Optional[int] = None
    notas: Optional[str] = None


def _puede_editar_perfiles(usuario: str) -> bool:
    """Quien esta EN el reparto no edita perfiles, ni el suyo ni el de nadie.

    Un vendedor que pudiera subirse su propia afinidad se estaria repartiendo los mejores
    leads a si mismo. Editan el administrador, el token de API y quien no entra en la cola
    (el administrador comercial). Se decide por pertenencia al reparto y no por una lista
    aparte, para que dar de alta a un vendedor le quite el permiso solo.
    """
    return usuario in ("api", ADMIN_USER) or usuario not in equipo_reparto()


async def _perfiles(conn, vendedores: list[str]) -> dict[str, dict[str, dict[str, float]]]:
    """Perfil guardado de cada vendedor, en 0-1. Quien no tiene filas no aparece."""
    filas = await conn.fetch(
        "SELECT vendedor, dimension, clave, afinidad FROM vendedor_perfil "
        "WHERE vendedor = ANY($1::text[]) AND origen = 'manual'",
        vendedores,
    )
    perfiles: dict[str, dict[str, dict[str, float]]] = {}
    for f in filas:
        perfiles.setdefault(f["vendedor"], {}).setdefault(f["dimension"], {})[f["clave"]] = f["afinidad"]
    return perfiles


async def _ajustes(conn, vendedores: list[str]) -> dict[str, dict]:
    filas = await conn.fetch(
        "SELECT vendedor, capacidad, notas, actualizado_at, actualizado_por "
        "FROM vendedor_ajustes WHERE vendedor = ANY($1::text[])",
        vendedores,
    )
    return {f["vendedor"]: db.row_to_dict(f) for f in filas}


def _topes(equipo: list[str], ajustes: dict[str, dict], forzado: Optional[int]) -> dict[str, int]:
    """Tope de leads abiertos por vendedor: el de la peticion, si no el suyo, si no el global.

    0 significa sin tope. La distincion importa: NULL en vendedor_ajustes es «no se dijo
    nada» y cae al global; 0 es «este no tiene tope aunque el global lo tenga».
    """
    if forzado is not None:
        return {v: forzado for v in equipo}
    salida = {}
    for v in equipo:
        propio = (ajustes.get(v) or {}).get("capacidad")
        salida[v] = REPARTO_CAPACIDAD if propio is None else propio
    return salida


@app.get("/api/vendedores")
async def listar_vendedores(usuario: str = Depends(check_auth)):
    """Los vendedores del reparto con su perfil, su tope y su carga. Lo que pinta el formulario."""
    equipo = equipo_reparto()
    pool = db.get_pool()
    async with pool.acquire() as conn:
        perfiles = await _perfiles(conn, equipo)
        ajustes = await _ajustes(conn, equipo)
        carga = await _carga_abierta(conn, equipo)
    items = []
    for v in equipo:
        aj = ajustes.get(v) or {}
        items.append({
            "vendedor": v,
            "abiertos": carga[v],
            "capacidad": aj.get("capacidad"),          # None = usa el global
            "notas": aj.get("notas"),
            "completado": v in perfiles,
            "actualizado_at": aj.get("actualizado_at"),
            "actualizado_por": aj.get("actualizado_por"),
            "perfil": afinidad.a_escala(perfiles.get(v) or {}),
        })
    return {
        "items": items,
        "dimensiones": afinidad.dimensiones_json(),
        "escala": afinidad.ESCALA,
        "neutro": int(round(afinidad.NEUTRO * afinidad.ESCALA)),
        "capacidad_global": REPARTO_CAPACIDAD,
        "puede_editar": _puede_editar_perfiles(usuario),
    }


@app.put("/api/vendedores/{vendedor}/perfil")
async def guardar_perfil(vendedor: str, body: PerfilBody, usuario: str = Depends(check_auth)):
    """Reemplaza el perfil manual del vendedor y sus ajustes. Deja quien y cuando."""
    if not _puede_editar_perfiles(usuario):
        raise HTTPException(status_code=403,
                            detail="Quien está en el reparto no edita perfiles")
    vendedor = vendedor.strip().lower()
    if vendedor not in equipo_reparto():
        raise HTTPException(status_code=404, detail=f"{vendedor} no está en el reparto")
    try:
        perfil = afinidad.normalizar(body.perfil)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if body.capacidad is not None and body.capacidad < 0:
        raise HTTPException(status_code=400, detail="El tope no puede ser negativo")
    notas = (body.notas or "").strip() or None

    pool = db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Reemplazo completo de lo MANUAL. Las filas de otro origen (las que algun dia
            # salgan de las conversaciones) no se tocan: el administrador no las escribio.
            await conn.execute(
                "DELETE FROM vendedor_perfil WHERE vendedor = $1 AND origen = 'manual'", vendedor)
            await conn.executemany(
                "INSERT INTO vendedor_perfil (vendedor, dimension, clave, afinidad, origen, actualizado_por) "
                "VALUES ($1, $2, $3, $4, 'manual', $5)",
                [(v, d, c, a, usuario) for v, d, c, a in afinidad.filas(vendedor, perfil)],
            )
            await conn.execute(
                """
                INSERT INTO vendedor_ajustes (vendedor, capacidad, notas, actualizado_por)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (vendedor) DO UPDATE
                   SET capacidad = EXCLUDED.capacidad, notas = EXCLUDED.notas,
                       actualizado_at = now(), actualizado_por = EXCLUDED.actualizado_por
                """,
                vendedor, body.capacidad, notas, usuario,
            )
    print(f"[perfil] {vendedor} guardado por {usuario} "
          f"(tope={'global' if body.capacidad is None else body.capacidad})", flush=True)
    return {"ok": True, "vendedor": vendedor, "perfil": afinidad.a_escala(perfil),
            "capacidad": body.capacidad}
