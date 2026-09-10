"""Reparto de leads entre vendedores segun el ranking de probabilidad de conversion.

Este modulo es PURO: no toca la base de datos ni la red. Entra una lista de leads ya
puntuados y sale quien se lleva cada uno. Asi se puede correr a mano para comprobar un
reparto antes de aplicarlo (`python -m app.reparto`), que es la unica forma de verificarlo
sin tests ni Postgres local.

METODO 1 — SERPIENTE (snake draft)
----------------------------------
Se ordenan los leads por probabilidad de conversion, de mayor a menor, y se reparten por
rondas invirtiendo el orden en cada una:

    ronda 1 ->  Alyssa  Diego   Jhon      (#1  #2  #3)
    ronda 2 <-  Jhon    Diego   Alyssa    (#4  #5  #6)
    ronda 3 ->  Alyssa  Diego   Jhon      (#7  #8  #9)

Por que invertir: el round-robin simple (siempre 1-2-3) le da a quien va primero el mejor
lead de CADA ronda, y esa ventaja se acumula lote tras lote. Invirtiendo, quien pierde el
primer puesto de una ronda gana el primero de la siguiente. Sobre un top 20 con una curva
de scores realista la brecha de valor esperado entre el primero y el ultimo vendedor baja
de 0.67 a 0.35 conversiones — la mitad.

Ademas el asiento inicial ROTA entre lotes (`inicio`): la ventaja residual de abrir la
ronda 1 no se queda pegada siempre a la misma persona.

Limite conocido: la serpiente reparte partes IGUALES. No mira cuantos leads abiertos
arrastra ya cada vendedor, asi que dos personas con carga muy distinta reciben lo mismo.
`capacidad` solo corta por arriba (a quien no le quedan huecos se le deja de repartir y
los leads sobrantes se quedan sin asignar, que es preferible a meterlos en una bandeja
saturada donde mueren). Equilibrar por carga acumulada es otro metodo, no este.
"""

from __future__ import annotations

from typing import Iterable, Optional, Sequence

# Nombre con el que se guarda el metodo en el historial. Cuando haya mas de uno, esta
# constante es la que distingue los lotes viejos de los nuevos en reparto_lotes.metodo.
METODO = "serpiente"


def _orden_de_ronda(vendedores: Sequence[str], ronda: int, inicio: int) -> list[str]:
    """El orden en que reparte una ronda: rotado por `inicio` e invertido si es impar."""
    n = len(vendedores)
    rotado = [vendedores[(inicio + i) % n] for i in range(n)]
    return rotado if ronda % 2 == 0 else rotado[::-1]


def serpiente(leads: Iterable[dict], vendedores: Sequence[str],
              capacidad: Optional[dict[str, int]] = None,
              inicio: int = 0) -> dict:
    """Reparte `leads` entre `vendedores` en serpiente. No muta lo que recibe.

    leads      -- dicts con al menos lead_id y conversion_prob. Se ordenan aqui dentro:
                  no se confia en que lleguen ordenados.
    vendedores -- en orden fijo (el de TAG_GROUPS['responsable']), para que el reparto sea
                  reproducible. Quien este de vacaciones se excluye antes de llamar.
    capacidad  -- huecos LIBRES por vendedor. None = sin tope. Un vendedor con 0 no recibe.
    inicio     -- indice del vendedor que abre la ronda 1 (cursor de rotacion entre lotes).

    Devuelve {asignaciones, sin_asignar, resumen, inicio, siguiente_inicio, rondas}.
    """
    vendedores = list(vendedores)
    if not vendedores:
        return {"asignaciones": [], "sin_asignar": list(leads), "resumen": [],
                "inicio": 0, "siguiente_inicio": 0, "rondas": 0}

    inicio %= len(vendedores)
    # Mayor probabilidad primero. El lead_id desempata para que dos scores iguales no se
    # repartan distinto en el preview y en el aplicar (el orden de Postgres no es estable).
    orden = sorted(leads, key=lambda l: (-(l.get("conversion_prob") or 0.0),
                                         str(l.get("lead_id"))))
    libres = dict(capacidad) if capacidad is not None else None

    asignaciones: list[dict] = []
    i = ronda = 0
    while i < len(orden):
        repartio = False
        for vendedor in _orden_de_ronda(vendedores, ronda, inicio):
            if i >= len(orden):
                break
            if libres is not None and libres.get(vendedor, 0) <= 0:
                continue
            lead = orden[i]
            asignaciones.append({
                "lead_id": lead.get("lead_id"),
                "vendedor": vendedor,
                "ronda": ronda + 1,
                "posicion": i + 1,                      # puesto en el ranking del lote
                "score": lead.get("conversion_prob"),
            })
            if libres is not None:
                libres[vendedor] -= 1
            i += 1
            repartio = True
        # Si una ronda entera no coloco a nadie es que ya nadie tiene hueco: parar aqui
        # evita el bucle infinito y deja el resto explicitamente sin asignar.
        if not repartio:
            break
        ronda += 1

    return {
        "asignaciones": asignaciones,
        "sin_asignar": orden[i:],
        "resumen": resumen(asignaciones, vendedores),
        "inicio": inicio,
        # Rotar UNA silla por lote: quien abrio hoy no abre mañana.
        "siguiente_inicio": (inicio + 1) % len(vendedores),
        "rondas": ronda,
    }


def resumen(asignaciones: Sequence[dict], vendedores: Sequence[str]) -> list[dict]:
    """Cartera resultante por vendedor: cuantos leads, cuanto valor y de que calidad.

    `valor` es la suma de probabilidades = conversiones esperadas de esa cartera. Es el
    numero que hay que mirar para juzgar si el reparto fue justo, no la cantidad de leads:
    seis leads buenos valen mas que siete malos.
    """
    salida = []
    for vendedor in vendedores:
        suyos = [a for a in asignaciones if a["vendedor"] == vendedor]
        valor = sum(a["score"] or 0.0 for a in suyos)
        salida.append({
            "vendedor": vendedor,
            "leads": len(suyos),
            "valor": round(valor, 4),
            "media": round(valor / len(suyos), 4) if suyos else None,
            "mejor": min((a["posicion"] for a in suyos), default=None),
        })
    return salida


def brecha(resumen_: Sequence[dict]) -> float:
    """Diferencia de valor esperado entre la mejor y la peor cartera del lote.

    Es la medida de justicia del reparto: cuanto mas cerca de 0, mas parejo. Se compara
    contra la del round-robin simple para saber si la serpiente esta aportando algo.
    """
    valores = [r["valor"] for r in resumen_ if r["leads"]]
    return round(max(valores) - min(valores), 4) if valores else 0.0


def _round_robin(leads: Sequence[dict], vendedores: Sequence[str]) -> list[dict]:
    """Round-robin simple (1-2-3, 1-2-3...). Solo existe para comparar contra la serpiente."""
    orden = sorted(leads, key=lambda l: (-(l.get("conversion_prob") or 0.0),
                                         str(l.get("lead_id"))))
    return [{"lead_id": l.get("lead_id"), "vendedor": vendedores[i % len(vendedores)],
             "ronda": i // len(vendedores) + 1, "posicion": i + 1,
             "score": l.get("conversion_prob")}
            for i, l in enumerate(orden)]


if __name__ == "__main__":
    # Comprobacion a mano del metodo, sin Postgres: una curva de scores plausible para un
    # top 20 y los tres vendedores reales. Correr con:  python -m app.reparto
    curva = [0.86, 0.81, 0.78, 0.74, 0.71, 0.68, 0.64, 0.61, 0.58, 0.55,
             0.52, 0.49, 0.46, 0.43, 0.41, 0.38, 0.36, 0.34, 0.32, 0.30]
    demo = [{"lead_id": f"lead-{i:02d}", "conversion_prob": p} for i, p in enumerate(curva, 1)]
    equipo = ["alyssa", "diego", "jhon"]

    r = serpiente(demo, equipo)
    ancho = max(len(v) for v in equipo)
    print(f"Top {len(demo)} entre {len(equipo)} vendedores — total esperado: "
          f"{sum(curva):.2f} conversiones\n")
    print("Reparto por rondas (# = puesto en el ranking):")
    for ronda in range(1, r["rondas"] + 1):
        fila = {a["vendedor"]: a["posicion"] for a in r["asignaciones"] if a["ronda"] == ronda}
        flecha = "->" if ronda % 2 else "<-"
        print(f"  ronda {ronda} {flecha} " +
              "  ".join(f"{v}:#{fila[v]:<2}" if v in fila else f"{v}:--" for v in equipo))

    print("\nCarteras:")
    for f in r["resumen"]:
        print(f"  {f['vendedor']:<{ancho}}  {f['leads']} leads   valor {f['valor']:.2f}   "
              f"media {f['media']:.3f}   mejor #{f['mejor']}")
    print(f"\n  brecha serpiente     : {brecha(r['resumen']):.2f}")
    print(f"  brecha round-robin   : {brecha(resumen(_round_robin(demo, equipo), equipo)):.2f}")
    if r["sin_asignar"]:
        print(f"\n  sin asignar: {len(r['sin_asignar'])}")
