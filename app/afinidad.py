"""Metodo 2 de reparto: afinidad vendedor-lead.

Por ahora aqui vive solo la mitad del VENDEDOR: que dimensiones tiene un perfil, que pregunta
responde cada una y como se valida lo que llega del formulario. El emparejamiento en si
(afinidad(lead, vendedor), valor de asignar, reparto) llega cuando haya perfiles rellenos.
El diseño completo, con la literatura detras de cada dimension, esta en
docs/reparto-metodo-2-afinidad.md (§6 dimensiones, §7 algoritmo).

Es un modulo PURO, como app/reparto.py: sin base de datos ni red, para poder comprobarlo a
mano con `python -m app.afinidad`.
"""

from __future__ import annotations

from typing import Any

METODO = "afinidad"

# El formulario pregunta de 0 a 10 porque a una persona le resulta natural; se guarda de 0 a 1.
ESCALA = 10
# Sin respuesta, la afinidad es NEUTRA: ni ayuda ni estorba. Es lo que hace que un perfil sin
# rellenar deje al metodo 2 comportandose como un reparto parejo, en vez de sesgarlo.
NEUTRO = 0.5

# Las dimensiones del perfil. Cada una es UNA pregunta al vendedor, y sus claves son las
# respuestas posibles, que coinciden con los valores que el extractor ya saca del lead
# (segmento, modulos_interes, solucion_actual, objecion) o que se derivan de el (tamano por
# num_rucs). `peso` es el prior de la literatura: el conocimiento del cliente y del producto es
# el mayor predictor (Verbeke 2011); `nivel` A = se puede emparejar desde el primer dia con
# datos que ya existen, B = falta extraer el dato del lado del lead.
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
        "id": "estilo", "nivel": "B", "peso": 2,
        "pregunta": "¿Qué tipo de conversación le sale mejor?",
        "ayuda": "Se usará cuando el extractor saque el estilo del lead; hoy no lo hace.",
        "claves": [("tarea", "Directo: va al precio, la integración, los plazos"),
                   ("relacion", "Conversador: cuenta su historia y su problema")],
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


if __name__ == "__main__":
    # Comprobacion a mano: el cuestionario y un perfil de ejemplo de ida y vuelta.
    print("Cuestionario del vendedor (0 = le cuesta · 5 = neutro · 10 = su fuerte)\n")
    for i, d in enumerate(DIMENSIONES, 1):
        print(f"{i}. {d['pregunta']}   [nivel {d['nivel']}, peso {d['peso']}]")
        for clave, etiqueta in d["claves"]:
            print(f"     · {etiqueta}  ({d['id']}.{clave})")
    ejemplo = {"segmento": {"estudio": 9, "independiente": 4}, "modulo": {"procesa": 8}}
    normal = normalizar(ejemplo)
    print("\nejemplo ->", {k: v for k, v in normal.items() if k in ("segmento", "modulo")})
    print("ida y vuelta ->", a_escala(normal)["segmento"], a_escala(normal)["modulo"])
    for malo in ({"segmento": {"estudio": 11}}, {"otra": {}}, {"segmento": {"x": 5}}):
        try:
            normalizar(malo)
        except ValueError as e:
            print("rechaza:", e)
