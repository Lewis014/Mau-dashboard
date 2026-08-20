# ¿Se pueden extraer las etiquetas del chatbot vía API?

**Sí se puede.** Y con un margen amplio: el dashboard ya está llamando al endpoint que las
devuelve, y las está descartando sin mirarlas.

Este documento cierra el item. No hace falta implementar nada para cerrarlo; lo que queda
pendiente es una sola comprobación contra la instancia real, descrita al final.

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

El resultado decide el final de este documento, que será una de estas dos frases:

- **Hay vocabulario útil** → se escribe aquí la tabla de equivalencias Chatwoot → `VALID_TAGS`
  y esto pasa a ser un item de implementación. Estimación: media jornada, porque el 90% del
  camino (autenticación, paginado, cruce por teléfono) ya está construido.
- **No hay etiquetas, o son ruido** → queda escrito que la API sí lo permite pero que **no hay
  dato que traer**. La alternativa es la que ya está en marcha: el etiquetado propio del
  dashboard, que además tiene un vocabulario cerrado y auditable que Chatwoot no ofrece.

> **Resultado del inventario:** _(pendiente de ejecutar contra la instancia real)_

---

## 4. Nota al margen: ¿y si «etiquetas del chatbot» eran otras?

Si lo que se pedía eran las etiquetas de la **app de WhatsApp Business** (las de Meta, no las
de Chatwoot), ese es un camino distinto y habría que verificarlo aparte.

No hace falta: todo lo que pasa por el bot pasa por Chatwoot, y ahí las etiquetas sí son
accesibles por API con las credenciales que este repositorio ya tiene.
