"""Metodo 2 de reparto: afinidad vendedor-lead.

Dos mitades. La del VENDEDOR es su perfil: que dimensiones tiene, que pregunta responde cada
una y como se valida lo que llega del formulario. La del LEAD sale de lo que el extractor ya
saca (segmento, modulos, RUCs, objecion, solucion actual) traducido a las mismas claves. Con
las dos se calcula afinidad(lead, vendedor) y se reparte.

El reparto HEREDA la paridad de la serpiente y solo decide quien va con quien: los cupos por
vendedor se cuentan igual que en app/reparto.py (por rondas, respetando topes), y sobre esos
cupos un problema de asignacion maximiza el valor esperado. Con perfiles neutros el resultado
es exactamente la serpiente; a medida que los perfiles dicen algo, los leads se recolocan
donde la afinidad lo justifica. El diseño y la literatura estan en
docs/reparto-metodo-2-afinidad.md (§6 dimensiones, §7 algoritmo).

Modulo PURO, como app/reparto.py: sin base de datos ni red. `python -m app.afinidad` imprime
el cuestionario y una comprobacion de ida y vuelta.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional, Sequence

from app import reparto as _serp

METODO = "afinidad"

# El formulario pregunta de 0 a 10 porque a una persona le resulta natural; se guarda de 0 a 1.
ESCALA = 10
# Sin respuesta, la afinidad es NEUTRA: ni ayuda ni estorba. Es lo que hace que un perfil sin
# rellenar deje al metodo 2 comportandose como un reparto parejo, en vez de sesgarlo.
NEUTRO = 0.5
# Lift relativo supuesto cuando un vendedor esta 1.0 de afinidad por encima de la media del
# lead. Es un PRIOR, no una medida: la comparacion entre metodos es lo que lo estimara.
BETA = 0.25

# Las dimensiones del perfil. Cada una es UNA pregunta al vendedor, y sus claves son las
# respuestas posibles, que coinciden con los valores que el extractor ya saca del lead
# (segmento, modulos_interes, solucion_actual, objecion) o que se derivan de el (tamano por
# num_rucs). `peso` es el prior de la literatura: el conocimiento del cliente y del producto es
# el mayor predictor (Verbeke 2011); `nivel` dice cada cuanto se activa sobre los candidatos
# reales: A = casi siempre (segmento 95%, modulo 79%, estilo 77%, tamano 54%), B = pocas veces
# pero cuenta cuando aparece (tecnico y objecion 13%, migracion 4%).
DIMENSIONES: list[dict[str, Any]] = [
    {
        "id": "segmento", "nivel": "A", "peso": 3,
        "pregunta": "¿Con qué tipo de cliente cierra mejor?",
        "ayuda": "Es la dimensión que más pesa: conocer al cliente.",
        "claves": [("estudio", "Estudio contable"),
                   ("independiente", "Contador independiente"),
                   ("empresa", "Empresa con contabilidad propia")],
    },
    {
        "id": "modulo", "nivel": "A", "peso": 3,
        "pregunta": "¿Qué módulo domina más?",
        "ayuda": "Conocimiento del producto. Se cruza con los módulos que pidió el lead.",
        "claves": [("procesa", "Procesa"), ("comunica", "Comunica"), ("valida", "Valida")],
    },
    {
        "id": "tamano", "nivel": "A", "peso": 2,
        "pregunta": "¿Pocas cuentas grandes o volumen de pequeñas?",
        "ayuda": "Por el número de RUCs que maneja el lead.",
        "claves": [("pequena", "Pequeña (menos de 10 RUCs)"),
                   ("mediana", "Mediana (10 a 49)"),
                   ("grande", "Grande (50 o más)")],
    },
    {
        "id": "estilo", "nivel": "A", "peso": 2,
        "pregunta": "¿Con qué tipo de consulta se maneja mejor?",
        "ayuda": "Según lo que el lead pregunta por iniciativa propia.",
        "claves": [("directo", "Directo: pregunta precio, demo, si se integra con X"),
                   ("explorador", "Explorador: pide que le cuenten, sin concretar")],
    },
    {
        "id": "tecnico", "nivel": "B", "peso": 2,
        "pregunta": "¿Cómo se maneja con clientes muy técnicos?",
        "ayuda": "Los que hablan de SIRE, PLE, crédito fiscal o integraciones. Uno de cada ocho.",
        "claves": [("alto", "Cliente con vocabulario técnico")],
    },
    {
        "id": "objecion", "nivel": "B", "peso": 1,
        "pregunta": "Cuando el freno es el precio, ¿cómo cierra?",
        "ayuda": "La única objeción con volumen suficiente para contar.",
        "claves": [("precio", "Objeción de precio")],
    },
    {
        "id": "migracion", "nivel": "B", "peso": 1,
        "pregunta": "¿Qué sistemas conoce para migrar desde ellos?",
        "ayuda": "Pocos leads vienen de estos sistemas; casi nunca decidirá.",
        "claves": [("concar", "Concar"), ("starsoft", "Starsoft"),
                   ("contasis", "Contasis"), ("odoo", "Odoo")],
    },
]

CLAVES: dict[str, list[str]] = {d["id"]: [c for c, _ in d["claves"]] for d in DIMENSIONES}
PESOS: dict[str, int] = {d["id"]: d["peso"] for d in DIMENSIONES}
ETIQUETAS: dict[str, str] = {c: l for d in DIMENSIONES for c, l in d["claves"]}


# ── Lado del vendedor: el perfil ─────────────────────────────────────────────────────────────

def dimensiones_json() -> list[dict[str, Any]]:
    """DIMENSIONES tal como las consume el formulario: listas en vez de tuplas."""
    return [dict(d, claves=[[c, l] for c, l in d["claves"]]) for d in DIMENSIONES]


def perfil_neutro() -> dict[str, dict[str, float]]:
    return {dim: {clave: NEUTRO for clave in claves} for dim, claves in CLAVES.items()}


def normalizar(crudo: dict[str, dict[str, Any]]) -> dict[str, dict[str, float]]:
    """Valida lo que manda el formulario (0-10) y lo devuelve en 0-1, completo.

    Rechaza dimensiones o claves que no existen y valores fuera de escala: un perfil con una
    clave inventada no rompe nada hoy, pero el dia que el emparejamiento la lea daria una
    afinidad de la nada. Las claves que falten quedan en NEUTRO.
    """
    if not isinstance(crudo, dict):
        raise ValueError("el perfil debe ser un objeto {dimension: {clave: valor}}")
    if (ajenas := sorted(set(crudo) - set(CLAVES))):
        raise ValueError(f"dimensiones desconocidas: {', '.join(ajenas)}")

    perfil = perfil_neutro()
    for dim, valores in crudo.items():
        if not isinstance(valores, dict):
            raise ValueError(f"{dim}: se esperaba un objeto {{clave: valor}}")
        if (ajenas := sorted(set(valores) - set(CLAVES[dim]))):
            raise ValueError(f"{dim}: claves desconocidas: {', '.join(ajenas)}")
        for clave, v in valores.items():
            if v is None:
                continue
            try:
                n = float(v)
            except (TypeError, ValueError):
                raise ValueError(f"{dim}.{clave}: '{v}' no es un número") from None
            if not 0 <= n <= ESCALA:
                raise ValueError(f"{dim}.{clave}: {v} está fuera de 0–{ESCALA}")
            perfil[dim][clave] = n / ESCALA
    return perfil


def a_escala(perfil: dict[str, dict[str, float]]) -> dict[str, dict[str, int]]:
    """De 0-1 a la escala del formulario (0-10, enteros). Rellena con NEUTRO lo que falte."""
    base = perfil_neutro()
    for dim, valores in (perfil or {}).items():
        if dim in base:
            for clave, v in (valores or {}).items():
                if clave in base[dim] and v is not None:
                    base[dim][clave] = float(v)
    return {dim: {clave: int(round(v * ESCALA)) for clave, v in valores.items()}
            for dim, valores in base.items()}


def filas(vendedor: str, perfil: dict[str, dict[str, float]]) -> list[tuple[str, str, str, float]]:
    """El perfil como filas (vendedor, dimension, clave, afinidad) para guardarlo."""
    return [(vendedor, dim, clave, float(v))
            for dim, valores in perfil.items() for clave, v in valores.items()]


# ── Lado del lead: sus claves ────────────────────────────────────────────────────────────────

def tamano_del_lead(num_rucs: Optional[int], volumen: Optional[int]) -> Optional[str]:
    """Pequena / mediana / grande. Por RUCs; si no los dio, por comprobantes al mes.

    Los cortes salen de la base real (mediana 20 RUCs, 500 comprobantes/mes): son los que
    dejan tres grupos con gente en cada uno, no una verdad del mercado.
    """
    if num_rucs is not None:
        return "pequena" if num_rucs < 10 else "mediana" if num_rucs < 50 else "grande"
    if volumen is not None:
        return "pequena" if volumen < 200 else "mediana" if volumen < 2000 else "grande"
    return None


def claves_del_lead(lead: dict) -> dict[str, list[str]]:
    """Que clave tiene el lead en cada dimension, segun lo que el extractor saco.

    Lista vacia = no se sabe, y entonces esa dimension es neutra para todos los vendedores.
    Solo `modulo` puede traer varias: un lead pide Procesa y Comunica a la vez.
    """
    seg = lead.get("segmento")
    sol = lead.get("solucion_actual")
    est = lead.get("estilo_consulta")
    tam = tamano_del_lead(lead.get("num_rucs"), lead.get("volumen_comprobantes"))
    return {
        "segmento": [seg] if seg in CLAVES["segmento"] else [],
        "modulo": [m for m in (lead.get("modulos_interes") or []) if m in CLAVES["modulo"]],
        "tamano": [tam] if tam else [],
        "estilo": [est] if est in CLAVES["estilo"] else [],
        # Solo el positivo: «bajo» se lo llevaria cualquiera que escriba dos palabras y no
        # distingue a nadie, asi que el extractor ya no lo produce (ver ESTILO_PROPS).
        "tecnico": ["alto"] if lead.get("dominio_tecnico") == "alto" else [],
        "objecion": ["precio"] if lead.get("objecion") == "precio" else [],
        "migracion": [sol] if sol in CLAVES["migracion"] else [],
    }


# ── El emparejamiento ────────────────────────────────────────────────────────────────────────

def afinidad(lead: dict, perfil: dict[str, dict[str, float]]) -> tuple[float, list[dict]]:
    """a(lead, vendedor) en [0, 1] y el detalle por dimension que explica el numero.

    Media ponderada por PESOS. Donde el lead no tiene dato, NEUTRO: asi un lead sin nada
    extraido vale 0.5 con todo el mundo y no se va siempre al mismo vendedor. En `modulo`,
    con varias claves, se promedia lo que el vendedor tiene en cada una.
    """
    claves = claves_del_lead(lead)
    total = peso_total = 0.0
    detalle: list[dict] = []
    for dim, peso in PESOS.items():
        suyas = claves.get(dim) or []
        if suyas:
            valores = [(perfil.get(dim) or {}).get(c, NEUTRO) for c in suyas]
            m = sum(valores) / len(valores)
            detalle.append({"dimension": dim, "claves": suyas, "valor": round(m, 4)})
        else:
            m = NEUTRO
        total += peso * m
        peso_total += peso
    return (total / peso_total if peso_total else NEUTRO), detalle


def explicar(detalle: Sequence[dict]) -> str:
    """El «por que» de una asignacion, legible: «estudio 9 · procesa/comunica 7»."""
    if not detalle:
        return "sin datos del lead"
    return " · ".join(f"{'/'.join(d['claves'])} {int(round(d['valor'] * ESCALA))}" for d in detalle)


def valor(prob: Optional[float], a: float, a_media: float, beta: float = BETA) -> float:
    """Lo que vale asignar este lead a este vendedor: su probabilidad, corregida por afinidad.

    Se resta la media del lead entre los vendedores disponibles para que la afinidad solo
    REORDENE vendedores: la suma de valor de un lead no depende de ella, solo quien lo lleva.
    """
    return (prob or 0.0) * (1.0 + beta * (a - a_media))


def _cupos(n: int, vendedores: Sequence[str], libres: dict[str, int]) -> dict[str, int]:
    """Cuantos leads le tocan a cada vendedor: por rondas, respetando huecos.

    Es la misma cuenta que hace la serpiente. Por eso el metodo 2 hereda su paridad y solo
    decide QUIEN va con quien; y quien no tiene huecos, no recibe. No muta `libres`.
    """
    libres = dict(libres)
    cupos = {v: 0 for v in vendedores}
    dados = 0
    while dados < n:
        avanzo = False
        for v in vendedores:
            if dados >= n:
                break
            if libres.get(v, 0) <= 0:
                continue
            cupos[v] += 1
            libres[v] -= 1
            dados += 1
            avanzo = True
        if not avanzo:
            break
    return cupos


def _casillas(vendedores: Sequence[str], cupos: dict[str, int], inicio: int) -> list[str]:
    """Las casillas en orden de serpiente: [A, B, B, A, A, B, ...] hasta agotar los cupos.

    Sirve de desempate: con afinidades iguales, el lead i cae en la casilla i, que es
    exactamente la serpiente. Solo cuando la afinidad lo justifica se sale de ahi.
    """
    quedan = dict(cupos)
    casillas: list[str] = []
    ronda = 0
    while sum(quedan.values()) > 0:
        for v in _serp._orden_de_ronda(vendedores, ronda, inicio):
            if quedan.get(v, 0) > 0:
                casillas.append(v)
                quedan[v] -= 1
        ronda += 1
    return casillas


def _hungaro(coste: list[list[float]]) -> list[int]:
    """Asignacion de coste minimo (Kuhn-Munkres, O(n^3)) para una matriz cuadrada.

    Devuelve, para cada fila, la columna asignada. Con 200 leads son 8 millones de pasos en
    Python puro: menos de un segundo, y no hace falta ninguna dependencia.
    """
    n = len(coste)
    if n == 0:
        return []
    m = len(coste[0])
    INF = float("inf")
    u = [0.0] * (n + 1)
    v = [0.0] * (m + 1)
    p = [0] * (m + 1)
    way = [0] * (m + 1)
    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minv = [INF] * (m + 1)
        usado = [False] * (m + 1)
        while True:
            usado[j0] = True
            i0 = p[j0]
            delta = INF
            j1 = 0
            for j in range(1, m + 1):
                if not usado[j]:
                    cur = coste[i0 - 1][j - 1] - u[i0] - v[j]
                    if cur < minv[j]:
                        minv[j] = cur
                        way[j] = j0
                    if minv[j] < delta:
                        delta = minv[j]
                        j1 = j
            for j in range(m + 1):
                if usado[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while True:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
            if j0 == 0:
                break
    salida = [0] * n
    for j in range(1, m + 1):
        if p[j]:
            salida[p[j] - 1] = j - 1
    return salida


def reparto(leads: Iterable[dict], vendedores: Sequence[str],
            perfiles: dict[str, dict[str, dict[str, float]]],
            capacidad: Optional[dict[str, int]] = None,
            inicio: int = 0, beta: float = BETA) -> dict:
    """Reparte `leads` entre `vendedores` maximizando valor con carteras parejas.

    perfiles   -- {vendedor: perfil en 0-1}; quien no tiene, neutro.
    capacidad  -- huecos libres por vendedor, como en la serpiente. None = sin tope.
    inicio     -- cursor de rotacion; solo afecta al desempate (ver _casillas).

    Devuelve lo mismo que reparto.serpiente, mas `afinidad` y `porque` por asignacion,
    `afinidad_media` por vendedor y `cupos`.
    """
    vendedores = list(vendedores)
    orden = sorted(leads, key=lambda l: (-(l.get("conversion_prob") or 0.0), str(l.get("lead_id"))))
    if not vendedores:
        return {"asignaciones": [], "sin_asignar": orden, "resumen": [], "inicio": 0,
                "siguiente_inicio": 0, "rondas": 0, "cupos": {}, "beta": beta}
    inicio %= len(vendedores)

    libres = dict(capacidad) if capacidad is not None else {v: len(orden) for v in vendedores}
    cupos = _cupos(len(orden), vendedores, libres)
    # Holgura de UNA casilla para quien no fue cuello de botella. Sin ella, cuando n no es
    # multiplo del numero de vendedores el lead sobrante iba a quien abria el turno aunque la
    # afinidad dijera otra cosa (con un solo lead, siempre al primero). Con ella lo decide
    # la afinidad, y las carteras siguen difiriendo como mucho en 1: con 20 leads y 2
    # vendedores no hay holgura (10 y 10). Quien esta al tope se queda con sus cupos justos.
    # Suelto = le quedan huecos, o tiene tantos como leads hay (sin tope de verdad): con un
    # solo lead, quien lo recibe por turno agota sus «huecos» y sin esta clausula dejaria
    # de contar como suelto, y la afinidad no podria moverlo.
    n = len(orden)
    sueltos = [v for v in vendedores if libres.get(v, 0) - cupos[v] > 0 or libres.get(v, 0) >= n]
    tope_suelto = max((cupos[v] for v in sueltos), default=0)
    casillas = _casillas(vendedores,
                         {v: (tope_suelto if v in sueltos else cupos[v]) for v in vendedores},
                         inicio)
    # El ranking decide quien espera, igual que en la serpiente: los de menor probabilidad.
    caben = sum(cupos.values())
    elegidos, esperan = orden[:caben], orden[caben:]

    perfil_de = {v: (perfiles.get(v) or {}) for v in vendedores}
    afin = [{v: afinidad(l, perfil_de[v]) for v in vendedores} for l in elegidos]
    coste: list[list[float]] = []
    for i, l in enumerate(elegidos):
        prob = l.get("conversion_prob") or 0.0
        media = sum(a for a, _ in afin[i].values()) / len(vendedores)
        fila = []
        for j, v in enumerate(casillas):
            # El termino minusculo rompe empates hacia la casilla de la serpiente. No puede
            # darle la vuelta a una diferencia real de valor: es 1e-9 contra ~1e-3.
            fila.append(-valor(prob, afin[i][v][0], media, beta) + 1e-9 * abs(i - j))
        coste.append(fila)

    asignacion = _hungaro(coste)
    asignaciones = []
    for i, l in enumerate(elegidos):
        v = casillas[asignacion[i]]
        a, detalle = afin[i][v]
        asignaciones.append({
            "lead_id": l.get("lead_id"), "vendedor": v,
            "ronda": 0, "posicion": i + 1, "score": l.get("conversion_prob"),
            "afinidad": round(a, 4), "porque": explicar(detalle), "detalle": detalle,
        })

    resumen = _serp.resumen(asignaciones, vendedores)
    for f in resumen:
        suyas = [x["afinidad"] for x in asignaciones if x["vendedor"] == f["vendedor"]]
        f["afinidad_media"] = round(sum(suyas) / len(suyas), 4) if suyas else None
    return {
        "asignaciones": asignaciones, "sin_asignar": esperan, "resumen": resumen,
        "inicio": inicio, "siguiente_inicio": (inicio + 1) % len(vendedores),
        "rondas": 0, "cupos": cupos, "beta": beta,
        "afinidad_media": round(sum(x["afinidad"] for x in asignaciones) / len(asignaciones), 4)
        if asignaciones else None,
    }


if __name__ == "__main__":
    print("Cuestionario del vendedor (0 = le cuesta · 5 = neutro · 10 = su fuerte)\n")
    for i, d in enumerate(DIMENSIONES, 1):
        print(f"{i}. {d['pregunta']}   [nivel {d['nivel']}, peso {d['peso']}]")
        for clave, etiqueta in d["claves"]:
            print(f"     · {etiqueta}  ({d['id']}.{clave})")

    ejemplo = {"segmento": {"estudio": 9, "independiente": 4}, "modulo": {"procesa": 8}}
    normal = normalizar(ejemplo)
    print("\nperfil de ejemplo ->", a_escala(normal)["segmento"], a_escala(normal)["modulo"])
    lead = {"lead_id": "x", "conversion_prob": 0.8, "segmento": "estudio",
            "modulos_interes": ["procesa", "comunica"], "num_rucs": 25,
            "estilo_consulta": "directo", "dominio_tecnico": "alto"}
    a, det = afinidad(lead, normal)
    print(f"lead estudio+procesa/comunica+25 RUCs -> afinidad {a:.3f}: {explicar(det)}")
    for malo in ({"segmento": {"estudio": 11}}, {"otra": {}}, {"segmento": {"x": 5}}):
        try:
            normalizar(malo)
        except ValueError as e:
            print("rechaza:", e)
