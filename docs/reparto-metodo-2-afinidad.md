# Método 2 de reparto: afinidad vendedor–lead. Qué dice la literatura y qué se puede emparejar hoy

**Veredicto corto.** La literatura respalda con claridad *qué atributos del vendedor* importan
—conocimiento del cliente y del producto, adaptabilidad— y desaconseja otros: similitud
demográfica, tests de personalidad, orientación al cliente como criterio. Lo que **no** respalda
todavía es que *emparejar* por esos atributos venza a un reparto parejo: en ventas, esa evidencia
viene de proveedores y patentes, no de estudios revisados por pares. Para Contatech eso se
traduce en tres dimensiones de afinidad con datos que ya existen, dos más que hay que extraer
primero, y el método 2 **diseñado como un experimento contra el método 1**, no como su reemplazo.

Y una restricción que manda sobre todo lo demás: con 41 leads cerrados etiquetados y **cero
ventas atribuidas a un vendedor**, hoy no hay forma de estimar afinidades con datos. Arrancan
como criterio experto, y es el propio reparto el que genera el dato que después las corrige.

Revisado el 10 de septiembre de 2026 contra la base de producción (552 leads reales, 327 con
score). Método 1 (serpiente) en [`app/reparto.py`](../app/reparto.py).

---

## 1. Qué predice el desempeño de un vendedor (y qué no)

Tres meta-análisis fijan el orden de importancia. Son estudios sobre el vendedor *individual*,
no sobre la pareja vendedor-cliente, pero dicen qué atributos vale la pena perfilar.

| Predictor | Efecto | Fuente |
|---|---|---|
| Conocimiento relacionado con la venta (cliente + producto) | **β = .28**, el mayor | Verbeke, Dietz & Verwaal 2011 |
| Grado de adaptabilidad (venta adaptativa) | **β = .27** | Verbeke et al. 2011; Franke & Park 2006 |
| Ambigüedad de rol | β = −.25 | Verbeke et al. 2011 |
| Aptitud cognitiva | β = .23 (ratings); **r = .04 en ventas objetivas** | Verbeke 2011; Vinchur et al. 1998 |
| Logro (faceta de responsabilidad, Big Five) | r = .41 en ventas objetivas | Vinchur et al. 1998 |
| Potencia (faceta de extraversión) | r = .26 en ventas objetivas | Vinchur et al. 1998 |
| Orientación al cliente | mejora **solo** el desempeño autoevaluado | Franke & Park 2006 (155 muestras, >31 000 vendedores) |

Lo que se lleva de aquí:

- **El conocimiento es el atributo número uno**, y es justamente el que se puede *emparejar*:
  quien conoce mejor a los estudios contables debería llevar estudios contables. Verbeke lo
  desglosa en conocimiento del cliente y del producto/tecnología, y la literatura B2B posterior
  mantiene esa distinción (marco de habilidades del vendedor B2B, JBBM 2021).
- **La adaptabilidad no se empareja, se tiene.** Un vendedor adaptativo rinde con cualquier
  lead; es un atributo de contratación y formación, no de enrutamiento.
- **La orientación al cliente no sirve como criterio.** Si mejora solo lo que el vendedor dice
  de sí mismo, perfilar «quién es más orientado al cliente» produce un ranking que no predice
  ventas.
- **La personalidad predice al vendedor, no a la pareja.** Que «logro» prediga ventas
  objetivas (r = .41) es una razón para contratar por logro, no para enrutar por logro: no hay
  evidencia de que un lead concreto convierta mejor con un vendedor más extravertido. Los tests
  tipo DISC, populares en ventas, no tienen respaldo como clave de emparejamiento.

## 2. Qué se sabe sobre emparejar vendedor y cliente

Aquí la evidencia es más vieja y más fina, pero apunta en una misma dirección.

**La similitud importa, pero la interna, no la visible.** Lichtenthal & Tellefsen (2001)
proponen la teoría de similitud comprador-vendedor para B2B y separan dos tipos: *observable*
(edad, género, origen) e *interna* (cómo piensan, valores, forma de decidir). Priorizan la
segunda. Crosby, Evans & Cowles (1990) encuentran que la similitud y la **pericia** del vendedor
son lo que convierte una oportunidad en venta, mientras que la calidad de la relación (confianza,
satisfacción) es lo que genera oportunidades *futuras*. El estudio clásico en retail (Churchill,
Collins & Strang 1975) da resultados débiles para la similitud demográfica.

Traducción: emparejar por «se parecen» no tiene base; emparejar por «este vendedor entiende cómo
piensa este tipo de cliente» sí la tiene, y es el mismo hallazgo que el conocimiento del cliente
del §1 visto desde el otro lado de la mesa.

**El estilo de comunicación es un atributo real de la díada.** Williams & Spiro (1985)
clasifican vendedores y clientes en orientados a la *tarea*, a la *interacción* o a *sí mismos*,
y encuentran que la combinación de estilos determina el resultado de la venta. Es el único
rasgo de «afinidad» que además se puede **leer en una transcripción**: un lead que abre con «¿cuánto
cuesta y cómo se integra con Concar?» y uno que abre contando su historia con el contador anterior
están pidiendo vendedores distintos.

## 3. Enrutamiento por habilidades: lo que trae la ingeniería de call centers

Los call centers resolvieron hace veinte años un problema estructuralmente idéntico: llamadas
con distintos requisitos, agentes con distintas habilidades, y una cola. Lo que aportan:

- **Con pocas habilidades por agente se obtiene casi todo el beneficio.** Wallace & Whitt (2005)
  y los estudios de simulación posteriores encuentran que dos habilidades por agente rinden casi
  como la flexibilidad total. Para Contatech significa que **no hace falta perfilar a Alyssa y a
  Diego en diez dimensiones**: dos o tres bien elegidas capturan casi todo. (Resultado tomado de
  los resúmenes; el abstract original no se pudo verificar directamente.)
- **Las habilidades se pueden calificar a partir de transcripciones.** IBM patentó en 2022
  (US 11 227 250 B2, Jones et al.) un procedimiento que toma transcripciones de chat con tripletas
  etiquetadas, construye *vectores de éxito multidimensionales* por representante, los agrega,
  los filtra por prioridad de negocio y los normaliza para obtener una calificación; la solicitud
  hermana (16/452 889) usa esas calificaciones para emparejar dinámicamente cliente y
  representante. Es exactamente lo que Chatwoot permitiría aquí: calificar a cada vendedor **por
  dimensión** a partir de sus propios turnos en las conversaciones cerradas. Hoy no se puede, por
  falta de atribución (§5); es el destino del dato que el método 1 empieza a generar.
- **El emparejamiento conductual existe como producto, no como evidencia.** Afiniti lo patentó
  (US 10 834 263, 11 218 597) y hay casos de proveedor con mejoras del orden del 29 % en un test
  A/B de 7 500 llamadas. Son cifras de quien vende el producto, sin revisión por pares; valen
  como indicio de que el efecto puede ser grande, no como prueba.

## 4. El tiempo de respuesta manda, y condiciona el diseño

Oldroyd, McElheran & Elkington (HBR, 2011) auditaron 2 241 empresas y, en un estudio aparte con
1,25 millones de leads de 29 empresas B2C y 13 B2B, encontraron que las que contactaban dentro de
la primera hora tenían **casi 7 veces** más probabilidad de calificar el lead (conversación
sustantiva con quien decide) que las que lo hacían una hora después, y **más de 60 veces** más que
las que esperaban un día. Y entre las causas de la lentitud citan, literalmente, «reglas para
distribuir los leads entre agentes basadas en geografía y *equidad*».

Consecuencias directas:

1. **Ninguna afinidad compensa una hora de retraso.** El método 2 no puede ser otro reparto por
   lotes: tiene que decidir **lead por lead, al llegar**. El lote en serpiente queda para el
   atraso acumulado (hoy 262 candidatos); lo que entra nuevo se asigna al momento.
2. **El SLA de recontacto sigue siendo la palanca mayor**, por delante de cualquier método de
   reparto. Está pendiente desde el método 1 y no cambia con este.

## 5. Qué hay en `leads_dataset` hoy (cobertura real)

El extractor de [`app/backfill.py`](../app/backfill.py) ya produce, por lead, la mayoría de las
features del lado del cliente. Medido el 10/09/2026 sobre 552 leads reales:

| Feature del lead | Cobertura | Distribución | ¿Sirve para emparejar? |
|---|---|---|---|
| `segmento` | 56 % | estudio 153 · independiente 151 · empresa **9** | **Sí, y es binaria en la práctica** |
| `modulos_interes` | 50 % | procesa 209 · comunica 165 · valida 152 (solapados) | **Sí**: conocimiento de producto |
| `num_rucs` / `volumen_comprobantes` | 30 % / 23 % | mediana 20 RUCs · 500 comprobantes/mes | Sí, como tamaño de cuenta; cobertura baja |
| `objecion` | 49 % | ninguna 201 · **precio 44** · pensarlo 20 · integración 5 · personal 2 | Solo `precio` tiene volumen |
| `solucion_actual` | 37 % | manual 133 · otro 30 · ninguno 24 · concar 9 · starsoft 5 · contasis 4 · odoo 3 | Migración desde competidor: ~21 leads en total |
| `urgencia` | 56 % | media 223 · baja 76 · alta 11 | No: es **prioridad** (SLA), no afinidad |
| `industry` | 34 % | «estudio contable», «Contabilidad», «accounting»… | **No discrimina**: todo el mercado es contable |
| `canal` | 100 % | whatsapp 552 | Constante |
| `tipo_lead`, `ticket_estimado` | 32 % / 15 % | juicios libres del LLM | No: heterogéneos y son opinión |

Y el dato que lo condiciona todo, cerrados con vendedor atribuido:

```
alyssa  estudio        ganados 0  perdidos 2
alyssa  independiente  ganados 0  perdidos 1
diego   estudio        ganados 0  perdidos 1
total cerrados etiquetados: 41   (37 sin etiqueta de responsable)
```

**No existe hoy ni una venta atribuida a un vendedor.** Cualquier afinidad «aprendida» sería
ruido; la estimación por datos es imposible hasta que el reparto (método 1 en producción desde el
10/09) acumule cierres con responsable. Esto no es un problema del método 2: es la razón por la
que el método 1 guarda historial.

## 6. Las dimensiones de afinidad, por niveles

**Nivel A — emparejar desde el primer día.** Evidencia fuerte (conocimiento, §1) y datos ya
extraídos:

| Dimensión | Claves | Lado del vendedor | Base |
|---|---|---|---|
| `segmento` | estudio · independiente (· empresa) | «con quién trabaja mejor» | Conocimiento del cliente (Verbeke); similitud interna (Lichtenthal) |
| `modulo` | procesa · comunica · valida | dominio de cada módulo | Conocimiento de producto (Verbeke) |
| `tamano` | pequeña (< 10 RUCs) · mediana · grande (≥ 50) | cuentas grandes vs. volumen de pequeñas | Especialización por tamaño de cuenta (Zoltners, Sinha & Lorimer) |

**Nivel B — extraer primero, emparejar después.** Evidencia buena, pero el dato del lado del
lead falta o es escaso:

| Dimensión | Qué falta | Base |
|---|---|---|
| `estilo` (tarea · relación) | Añadir un enum al extractor del backfill y re-extraer. El lado del vendedor sale de sus propios turnos en Chatwoot (§3, IBM) | Williams & Spiro 1985 |
| `objecion` (precio) | Solo `precio` tiene volumen (44). Un único atributo: «cierra bien con objeción de precio» | Enrutamiento por habilidades |
| `migracion` (concar · starsoft · contasis · odoo) | ~21 leads. Se define pero casi nunca se activa | Conocimiento de producto |

**Nivel C — no usar como clave de emparejamiento**, aunque parezcan naturales:

- Similitud demográfica u observable (edad, género, ciudad): efecto débil (Churchill 1975), la
  teoría B2B la subordina a la interna (Lichtenthal 2001).
- Orientación al cliente como rasgo: solo mueve la autoevaluación (Franke & Park 2006).
- Perfiles de personalidad (DISC, Big Five): predicen al vendedor, no a la pareja (Vinchur 1998).
  Valen para contratar y formar; como clave de enrutamiento no tienen respaldo.
- `urgencia`: es prioridad de atención, va al SLA, no a la afinidad. `industry` y `canal` no
  discriminan en este mercado.

## 7. El algoritmo

Todo se apoya en `conversion_prob` como ancla: la afinidad **reordena vendedores**, nunca cambia
la prioridad del lead.

**Afinidad.** Para cada dimensión *k* del nivel A (y B cuando exista), el emparejamiento
`m_k(l, v) ∈ [0, 1]` es la afinidad declarada del vendedor *v* para la clave que tiene el lead
*l* en esa dimensión. Si el lead **no tiene** la feature —que es el caso del 44–77 % según la
dimensión— `m_k = 0,5`: neutral. Esto es lo que evita que un lead sin datos se vaya siempre al
mismo vendedor.

```
a(l, v) = Σ_k w_k · m_k(l, v) / Σ_k w_k
```

Pesos iniciales desde la literatura: `segmento` y `modulo` (conocimiento) los más altos; `tamano`
intermedio; nivel B por debajo. Son priores, no verdades: el experimento del §8 es lo que los
corrige.

**Valor de asignar.** `valor(l, v) = p(l) · (1 + β · (a(l, v) − ā(l)))`, con `ā(l)` la
afinidad media del lead entre los vendedores disponibles y `β` el *lift* relativo supuesto a
afinidad plena (arrancar en 0,25). Restar la media hace que la suma de valor de un lead no
dependa de la afinidad —solo *quién* lo lleva— y `β` es precisamente la cantidad que el
experimento estima.

**Reparto por lotes (el atraso).** Maximizar `Σ valor(l, v)` con tope de capacidad y equilibrio
de cantidades `|n_v − n_w| ≤ 1`. Es el problema de asignación (Kuhn 1955, algoritmo húngaro) con
cada vendedor replicado ⌈n/k⌉ veces. Con **dos vendedores** tiene solución cerrada: ordenar los
leads por `Δ(l) = valor(l, A) − valor(l, B)` y darle a A la mitad con mayor Δ. No hace falta
ninguna librería.

**Lead por lead (lo que entra).** `argmax_v [ valor(l, v) − γ · (carga_v − carga_media) ]`
entre quienes tienen hueco. El término `γ` es el equilibrio por carga acumulada que el método 1
no tiene, y con él el reparto en vivo converge a carteras parejas aunque no haya lotes.

## 8. Cómo se evalúa, y por qué el método 2 es un experimento

Nada de lo anterior está demostrado en este mercado. El diseño honesto —y el defendible ante
jurado— es medirlo:

- **Aleatorizar por lead** entre dos brazos: *serpiente/rotación* (método 1, el control) y
  *afinidad* (método 2). El brazo se guarda por asignación. Con exploración del 100 % en el
  control no hace falta ε-greedy aparte: el control **es** la exploración.
- **Comparar a igual score.** Como `conversion_prob` correlaciona con el resultado por
  construcción, quien reciba leads mejor puntuados mostrará mejor tasa sin importar la afinidad.
  La comparación va estratificada por deciles de `conversion_prob`, o como *lift* sobre lo
  predicho. Es la misma lógica del *uplift modeling*: lo que importa es el efecto *incremental*
  de la asignación, no la tasa bruta (VALOR, 2026, lo formula para B2B: identificar cuentas
  «persuadibles»).
- **Resultado:** la etiqueta de la tesis, `cliente`/`free_trial` frente a `perdido`; los estados en
  curso son censura y se excluyen, como en el modelo.
- **Tamaño de muestra, sin adornos.** A ~7 leads/día, detectar que la afinidad sube la conversión
  de 20 % a 30 % (lift relativo del 50 %, α = 0,05, potencia 0,8) pide unos **300 leads por
  brazo: ~3 meses**. Detectar 10 % → 13 % pide ~1 800 por brazo: inviable. La tesis puede
  presentar el **diseño** y un **piloto con intervalo de confianza** (bootstrap, como el AUC), no
  un veredicto. Decirlo así es más defendible que un efecto sin potencia.

## 9. Qué hay que construir

Lado del vendedor, nuevo:

```sql
-- Perfil de afinidad, una fila por (vendedor, dimension, clave). `origen` distingue lo que puso
-- el administrador de lo que se calculo despues a partir de cierres atribuidos (§3, IBM).
CREATE TABLE vendedor_perfil (
  vendedor        text NOT NULL,
  dimension       text NOT NULL,   -- segmento | modulo | tamano | estilo | objecion | migracion
  clave           text NOT NULL,   -- estudio | procesa | grande | tarea | precio | concar ...
  afinidad        real NOT NULL CHECK (afinidad BETWEEN 0 AND 1),
  origen          text NOT NULL DEFAULT 'manual',   -- manual | observado
  actualizado_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (vendedor, dimension, clave)
);
-- Lo que hace evaluable el experimento: la afinidad y el brazo EN EL MOMENTO del reparto.
ALTER TABLE reparto_asignaciones ADD COLUMN afinidad real;
ALTER TABLE reparto_asignaciones ADD COLUMN brazo text;   -- serpiente | afinidad
```

Lado del lead: un enum `estilo_comunicacion` (`tarea` · `relacion` · null) en el esquema del
extractor de `backfill.py`, y una re-extracción (552 llamadas al modelo). Hasta entonces el nivel
B queda en `m_k = 0,5` y no estorba.

Pantalla: un formulario de perfil por vendedor (seis controles deslizantes, no más, por §3) y,
en la vista de Reparto, un tercer botón junto a *Top 10/20/50*: **Afinidad** contra
**Serpiente**, con la brecha de carteras y la afinidad media del lote a la vista.

Prerrequisitos que ya están: `conversion_prob`, las features del lead, la carga abierta por
vendedor, el historial de lotes y `REPARTO_VENDEDORES` para saber quién entra.

## 10. Referencias

Verificadas en el texto original: Verbeke et al. (resumen), Franke & Park (resumen), Vinchur et
al. (resumen), Oldroyd et al. (texto completo), IBM US 11 227 250 (portada y resumen). El resto,
desde resúmenes y fichas bibliográficas.

- Verbeke, W., Dietz, B. & Verwaal, E. (2011). *Drivers of sales performance: a contemporary
  meta-analysis. Have salespeople become knowledge brokers?* JAMS 39(3), 407–428.
  <https://link.springer.com/article/10.1007/s11747-010-0211-8>
- Franke, G. R. & Park, J.-E. (2006). *Salesperson adaptive selling behavior and customer
  orientation: a meta-analysis.* JMR 43(4), 693–702. <https://journals.sagepub.com/doi/10.1509/jmkr.43.4.693>
- Vinchur, A. J., Schippmann, J. S., Switzer, F. S. & Roth, P. L. (1998). *A meta-analytic review
  of predictors of job performance for salespeople.* J. Applied Psychology 83(4), 586–597.
  <https://www.semanticscholar.org/paper/eb63b764e95e85e3a23cff5fe72d895f5d75c9df>
- Lichtenthal, J. D. & Tellefsen, T. (2001). *Toward a theory of business buyer-seller
  similarity.* JPSSM 21(1), 1–14. <https://www.tandfonline.com/doi/abs/10.1080/08853134.2001.10754251>
- Crosby, L. A., Evans, K. R. & Cowles, D. (1990). *Relationship quality in services selling: an
  interpersonal influence perspective.* J. Marketing 54(3), 68–81.
  <https://journals.sagepub.com/doi/abs/10.1177/002224299005400306>
- Churchill, G. A., Collins, R. H. & Strang, W. A. (1975). *Should retail salespersons be similar
  to their customers?* J. Retailing 51(3).
- Williams, K. C. & Spiro, R. L. (1985). *Communication style in the salesperson-customer dyad.*
  JMR 22(4), 434–442. <https://journals.sagepub.com/doi/abs/10.1177/002224378502200408>
- Wallace, R. B. & Whitt, W. (2005). *A staffing algorithm for call centers with skill-based
  routing.* MSOM 7(4), 276–294. Estudio de simulación relacionado:
  <https://www.researchgate.net/publication/4111952>
- Jones, S. W. et al. / IBM (2022). *Rating customer representatives based on past chat
  transcripts.* US 11 227 250 B2. Solicitud hermana 16/452 889, *Matching a customer and customer
  representative dynamically based on a customer representative's past performance*.
- Afiniti. *Techniques for behavioral pairing in a contact center system.* US 10 834 263 B2;
  US 11 218 597 B2. Caso de proveedor: <https://behavioralsignals.com/ai-mediated-conversations-or-behavioral-profile-pairing-how-that-benefits-your-business/>
- Oldroyd, J. B., McElheran, K. & Elkington, D. (2011). *The short life of online sales leads.*
  Harvard Business Review, marzo. <https://hbr.org/2011/03/the-short-life-of-online-sales-leads>
- Zoltners, A. A., Sinha, P. & Lorimer, S. E. (2004). *Sales Force Design for Strategic
  Advantage.* Palgrave. <https://link.springer.com/book/10.1057/9780230514928> — y (2006) *Match
  your sales force structure to your business cycle*, HBR.
- *B to B sellers' skill level in sales performance – frameworks and findings* (2021). J. of
  Business-to-Business Marketing. <https://www.tandfonline.com/doi/full/10.1080/1051712X.2021.1974169>
- *VALOR: Value-aware revenue uplift modeling with treatment-gated representation for B2B sales*
  (2026). arXiv:2604.02472. <https://arxiv.org/abs/2604.02472>
- Kuhn, H. W. (1955). *The Hungarian method for the assignment problem.* Naval Research
  Logistics Quarterly 2, 83–97.
