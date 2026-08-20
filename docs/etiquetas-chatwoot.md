# ¿Se pueden extraer las etiquetas del chatbot vía API?

**Sí se puede, y el dato sirve.** Por un margen amplio: el dashboard ya está llamando al
endpoint que las devuelve, y las está descartando sin mirarlas.

Inventariado contra la instancia real el 20 de agosto de 2026: 12 etiquetas, 86 aplicaciones,
de las que **70 son importables**. Las 16 restantes se quedan fuera a propósito porque mau-web
ya sabe eso mejor (§5).

Con esto el item queda **cerrado**. Lo que sigue es un item nuevo de implementación, con dos
preguntas que hay que responder antes de empezar (§6).

---

## 1. La prueba

`cw_list_conversations()` en [`app/backfill.py:139-152`](../app/backfill.py) llama a:

```
GET /api/v1/accounts/{account_id}/conversations
```

Según la [documentación oficial de Chatwoot](https://developers.chatwoot.com/api-reference/conversations/conversations-list),
cada conversación del `payload` de ese endpoint incluye un campo **`labels`, un array de
strings**. El código actual solo lee `inbox_id` y `meta.sender.phone_number`
([`backfill.py:378-384`](../app/backfill.py)) y tira el resto del objeto, etiquetas incluidas.

Endpoints disponibles, todos con la cabecera `api_access_token` que `_cw_get()` ya envía
([`backfill.py:130-136`](../app/backfill.py)):

| Qué | Método y ruta |
|---|---|
| Etiquetas dentro del listado de conversaciones | `GET /api/v1/accounts/{account_id}/conversations` → `payload[].labels` |
| Etiquetas de una conversación concreta | `GET /api/v1/accounts/{account_id}/conversations/{conversation_id}/labels` → `{"payload": ["...", "..."]}` |
| Catálogo de etiquetas de la cuenta | `GET /api/v1/accounts/{account_id}/labels` |
| Escribir etiquetas en una conversación | `POST /api/v1/accounts/{account_id}/conversations/{conversation_id}/labels` con `{"labels": [...]}` |

**No hace falta infraestructura nueva.** Mismo host, mismo token, misma red de Docker, misma
función. Traer las etiquetas al dashboard es leer una clave más de un JSON que ya llega.

---

## 2. Los cuatro problemas que hay que resolver antes

El «sí» es la parte fácil. Lo que decide si esto funciona o se convierte en un desastre
silencioso es lo siguiente.

### 2.1 Las etiquetas son de la conversación; el lead es un teléfono

En Chatwoot la etiqueta cuelga de una conversación. En el dashboard la unidad es el lead,
identificado por su teléfono, y un mismo teléfono puede tener varias conversaciones — de
hecho `by_phone` ya es un `dict[str, list[int]]` ([`backfill.py:377-384`](../app/backfill.py)).

Hay que **unir** las etiquetas de todas las conversaciones del lead. Quedarse con las de una
sola daría un resultado que cambia según el orden en que responda la API.

### 2.2 Vocabulario abierto contra vocabulario cerrado

En Chatwoot cualquier agente crea una etiqueta escribiéndola. En el dashboard las etiquetas
son una lista cerrada (`VALID_TAGS`) y `normalize_tags()`
([`app/main.py`](../app/main.py)) responde **400 ante cualquier valor que no esté en ella**.

Eso obliga a un diccionario explícito de equivalencias Chatwoot → dashboard. Y obliga a
decidir qué pasa con lo que no está en el diccionario: la respuesta correcta es
**reportarlo**, nunca inventar una equivalencia ni dejar que reviente la importación.

### 2.3 Ya hay dos manos escribiendo en `outcome_tags`

Hoy escriben ahí:

1. El vendedor, desde el dashboard.
2. `sync_planes`, que manda **solo** sobre `free_trial` / `cliente` / `perdido` (`PLAN_TAGS`)
   y deja el resto intacto a propósito ([`app/sync_planes.py:90-94`](../app/sync_planes.py)).

Una tercera mano sin carril declarado deshará el trabajo del vendedor en silencio, que es la
peor forma de fallar: nadie presenta una queja, simplemente dejan de fiarse del tablero.

**Recomendación: la primera versión debe ser de solo lectura.** La etiqueta de Chatwoot se
muestra en su propia columna, sin tocar `outcome_tags`. Fusionarlas se decide *después* de
ver el vocabulario real, no antes.

### 2.4 Escribir de vuelta es posible, pero destructivo

El `POST` de etiquetas **sobrescribe el conjunto entero** de la conversación; no añade. Si el
dashboard empujara sus etiquetas a Chatwoot sin leer antes lo que hay, borraría las que puso
un agente a mano.

---

## 3. Lo único que falta para cerrar del todo

Que la API lo permita no significa que el dato sirva. Eso depende de si el equipo usa las
etiquetas de verdad en Chatwoot, y no se puede saber leyendo código.

Se responde con un comando, sin escribir nada nuevo — reutiliza la función que ya existe:

```bash
docker compose exec dashboard python -c "
from collections import Counter
from app.backfill import cw_list_conversations, _cw_get
print('Catálogo de la cuenta:', [l.get('title') for l in (_cw_get('/labels').get('payload') or [])])
c = Counter(l for cv in cw_list_conversations() for l in (cv.get('labels') or []))
print(f'{len(c)} etiquetas en uso, sobre {sum(c.values())} aplicaciones')
[print(f'{n:>5}  {l}') for l, n in c.most_common()]
"
```

### Resultado (ejecutado el 20 de agosto de 2026)

**Hay vocabulario útil.** 12 etiquetas en el catálogo de la cuenta, las 12 en uso, con 86
aplicaciones repartidas así:

| Etiqueta en Chatwoot | Usos |
|---|---:|
| `lead-interesado` | 37 |
| `llamada-aly` | 19 |
| `llamsincon` | 9 |
| `demo` | 5 |
| `prueba` | 5 |
| `llamada` | 3 |
| `cliente` | 3 |
| `no-desea` | 1 |
| `no-aplica` | 1 |
| `devolver-llamada` | 1 |
| `numero-apagado` | 1 |
| `free-trial` | 1 |

Para dimensionarlo: el dataset tiene 528 leads, así que esto cubre como mucho un 15% de la
base. **Es un aporte, no un sustituto del etiquetado propio.**

---

## 4. Tabla de equivalencias

Nueve de las doce traducen sin ambigüedad:

| Chatwoot | → dashboard | Nota |
|---|---|---|
| `lead-interesado` | `lead_interesado` | directa |
| `llamada` | `llamada` | directa |
| `llamada-aly` | `llamada` **+** `alyssa` | dos etiquetas de grupos distintos, ver abajo |
| `llamsincon` | `llamada_no_responde` | «llamada sin contestar» |
| `numero-apagado` | `llamada_no_responde` | |
| `devolver-llamada` | `insistir` | |
| `no-desea` | `perdido` | pero ver el carril del §5 |
| `cliente` | `cliente` | pero ver el carril del §5 |
| `free-trial` | `free_trial` | pero ver el carril del §5 |

**`llamada-aly` es el hallazgo que más vale.** Con 19 usos es la segunda más aplicada, y
codifica algo que el dashboard no puede sacar de ninguna otra fuente: **quién atendió al
lead**. Traducirla asigna responsable a 19 leads de golpe, y el responsable es justo lo que
decide a quién se dirige una alerta automática.

### Las tres que NO se pueden traducir solas

1. **`demo` (5 usos)** — el dashboard distingue `demo_agendada` de `demo_realizada`, y
   Chatwoot no. Agendada y realizada no son lo mismo para el embudo: una es una promesa y la
   otra un hecho. **Hace falta decidir a cuál corresponde**, o mirar esas 5 conversaciones.

2. **`prueba` (5 usos)** — ambigua y **la más peligrosa**. Puede significar «empezó la prueba
   gratis» o «esta conversación es una prueba interna». Que el catálogo tenga además
   `free-trial` por separado hace sospechar que no son sinónimos. Si significa lo segundo y
   se importa como `free_trial`, se mete ruido en la columna que alimenta el modelo de la
   tesis. **Hasta aclararlo, esta etiqueta no se importa.**

3. **`no-aplica` (1 uso)** — no hay equivalente. Lo más cercano es `perdido`, pero «no aplica»
   suena a que nunca fue un lead válido, que es otra cosa. Con un solo uso, no merece decidir
   nada: se reporta y se etiqueta a mano.

---

## 5. El carril: qué puede escribir Chatwoot y qué no

Aquí se concreta el problema del §2.3 con los datos ya sobre la mesa.

`cliente`, `free-trial` y `no-desea` caen sobre `cliente` / `free_trial` / `perdido`, que son
exactamente las tres etiquetas que **`sync_planes` reescribe cada mañana** desde mau-web
(`PLAN_TAGS`, [`app/sync_planes.py:90-94`](../app/sync_planes.py)). Importarlas produciría
uno de estos dos desenlaces, los dos malos:

- En los 30 leads que cruzaron con una cuenta, el sync las borraría a la mañana siguiente.
- En los 498 que no cruzaron, sobrevivirían — dando un criterio distinto según el lead, que
  es peor que no tener criterio.

**Regla, entonces:**

> Chatwoot manda sobre las etiquetas de **flujo de venta** (`lead_interesado`, `llamada`,
> `llamada_no_responde`, `insistir`, `demo_*`) y sobre el **responsable**.
> mau-web sigue mandando sobre `cliente` / `free_trial` / `perdido`, porque ahí es un hecho
> del sistema de suscripciones y no la opinión de un agente.

Con ese recorte, lo importable son **70 de las 86 aplicaciones** — y las 16 que se quedan
fuera son justo las que mau-web ya sabe mejor.

---

## 6. Veredicto

**Se puede, y el dato sirve.** Esto pasa de pregunta a item de implementación:

1. Leer `labels` en `cw_list_conversations()` (ya llega en el JSON) y unirlas por teléfono.
2. Traducir con la tabla del §4, saltando `prueba` y `no-aplica`, y **reportando** cualquier
   etiqueta nueva que aparezca en vez de inventarle un equivalente.
3. Escribir solo en el carril del §5, y **nunca** sobre `PLAN_TAGS`.
4. Primera versión en modo informe (`--dry-run`): cuántos leads recibirían qué, antes de
   escribir nada.

Estimación: media jornada. El 90% del camino —autenticación, paginado, cruce por teléfono—
ya está construido para el backfill.

**Antes de empezar hacen falta dos respuestas:** qué significa `demo` (¿agendada o realizada?)
y qué significa `prueba` (¿prueba gratis o conversación de prueba?).

---

## 7. Nota al margen: ¿y si «etiquetas del chatbot» eran otras?

Si lo que se pedía eran las etiquetas de la **app de WhatsApp Business** (las de Meta, no las
de Chatwoot), ese es un camino distinto y habría que verificarlo aparte.

No hace falta: todo lo que pasa por el bot pasa por Chatwoot, y ahí las etiquetas sí son
accesibles por API con las credenciales que este repositorio ya tiene.
