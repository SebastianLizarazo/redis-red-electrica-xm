# GUION DE PRESENTACION — Demo en vivo

> Documento de apoyo para los 5 integrantes del equipo
> `redis-red-electrica-xm` durante la presentación del taller el
> martes 22-sept-2026.
>
> **Sin diapositivas**: la materia no las permite. Toda la presentación
> se cubre en demo en vivo más este guion. El sistema funcionando en
> pantalla es el material principal.

## Cabecera — Integrantes y secciones del guion

| Persona | Rol en el equipo | Seccion(es) del guion que presenta |
|---------|------------------|------------------------------------|
| **Sebastian** | Lider tecnico · Integracion · Documentacion | Paso 1 (publisher en marcha) + Paso 4 (procesamiento) + Paso 8 (estado actual) + Paso 9 (historico reciente) + Cierre |
| **Edwar** | Publisher (XM + simulador + selector) | Paso 2 (generar evento nuevo) + Paso 6 (condicion de alerta) + Mantenimiento de publishers |
| **Alejandro** | Subscriber + API + alertas | Paso 3 (evento en Redis) + soporte tecnico en Paso 4 y Paso 6 |
| **David** | Dashboard frontend | Paso 5 (dashboard actualizandose) + Paso 7 (cambio visual del dashboard) |
| **Jonathan** | Infraestructura + Testing + Docker | Plan B (`cloudflared`) si falla la demo local |

> Todos deben poder responder cualquier pregunta tecnica del profesor o
> del jurado, no solo las de su seccion. Esta tabla es solo la
> responsabilidad principal.

---

## Previo (10 minutos antes de la demo)

Responsables: **Sebastian** (coordinacion) + **Jonathan** (infra).

Acciones a verificar en orden:

1. `docker compose -f infra/docker-compose.yml ps` — los 4 servicios
   (redis, publisher, subscriber, api) deben estar en estado
   `healthy`.
2. `curl http://localhost:8000/api/health` — responder 200 con
   `redis_ok: true`, `mode: "real"` o `"simulator"`, `uptime_seconds`
   razonable (> 30 s).
3. Abrir `http://localhost:5173` en el navegador del presentador —
   banner de modo visible, KPIs poblados, mapa con 5 marcadores.
4. Terminal 1 con `make logs-publisher` y terminal 2 con
   `make logs-subscriber` listas, ocultas hasta que toque mostrar.
5. Tener `make redis-cli` disponible (sirve para el paso 3 y el paso 8).

Si alguno de los puntos falla, activar **Plan B** (ver seccion Cierre).

---

## Flujo de la demo en vivo — 9 pasos

### Paso 1 — Mostrar el Publisher en marcha

**Presenta**: Sebastian (con Edwar de backup para preguntas tecnicas).

**Comandos**:

```bash
make logs-publisher
```

(Tambien se puede correr antes `docker compose -f infra/docker-compose.yml ps`
para que el profesor vea los 4 contenedores.)

**Que dice en voz alta**:

> "El publisher es el primer eslabon del pipeline. Es un proceso Python
> asincrono que en cada ciclo decide de donde sacar los datos — si de
> XM real o del simulador de respaldo — y los normaliza al modelo
> `Event` que compartimos con el resto del sistema. En los logs ven
> tres campos: la demanda nacional del SIN, la generacion, y la fuente
> del dato: `real` cuando viene de XM, `simulator` cuando es
> sintetico. La cadencia depende del modo: cinco segundos para el
> simulador, cinco minutos para XM, que es la cadencia regulatoria del
> operador."

**Que valida visualmente**:

- Logs corriendo con lineas del estilo
  `ciclo publicado ciclo=42 demanda_sin_mw=10500 generacion_sin_mw=10800 fuente=real`.
- Estado del contenedor `publisher-red-electrica` con `healthy` en
  `docker compose ps`.

---

### Paso 2 — Generar un nuevo evento

**Presenta**: Edwar.

**Comandos**:

Opcion A — modo automatico (publisher genera solo, no requiere accion):

```bash
# Esperar el siguiente ciclo del publisher y observar logs.
```

Opcion B — forzar un escenario de stress desde el dashboard (boton en
el panel inferior "Pruebas de stress"). Esto escribe un flag en Redis
con TTL 30 s y el siguiente ciclo del simulador lo aplica:

```bash
# Equivalente por CLI si el dashboard no estuviera disponible
docker compose -f infra/docker-compose.yml exec redis redis-cli \
  SET stress:demand_surge 1 EX 30
```

Opcion C — forzar el switch a simulador y observar el log:

```bash
docker compose -f infra/docker-compose.yml exec redis redis-cli \
  SET health:failures 3
```

**Que dice en voz alta**:

> "Para generar un evento nuevo tenemos tres caminos. El primero es
> automatico: el publisher publica en cada ciclo sin intervencion. El
> segundo es inyectar un escenario de stress desde el dashboard — por
> ejemplo, este boton 'Pico de demanda' suma 25 % al consumo nacional
> durante los proximos 30 segundos. El tercero es forzar tres fallos
> consecutivos de XM tocando `health:failures` y ver como el sistema
> conmuta a simulador."

**Que valida visualmente**:

- Si se uso la opcion B: en el siguiente ciclo del log aparece
  `simulando con stress activo stress=[demand_surge]` y `demanda_sin_mw`
  un 25 % mas alto que el ciclo anterior.
- Si se uso la opcion C: aparece el banner del dashboard
  cambiando de `MODO: REAL` a `MODO: DEGRADADO` y eventualmente a
  `MODO: SIMULADOR`.

---

### Paso 3 — Mostrar el evento en Redis

**Presenta**: Alejandro.

**Comandos**:

```bash
make redis-cli
```

Una vez dentro del shell de redis-cli:

```
> SUBSCRIBE energy-events
```

(Dejar abierto. En otra terminal o en otra pestana del navegador se
puede inspeccionar el estado. Si no se quiere mantener el SUBSCRIBE
abierto, sustituir por:)

```
> XLEN energy:stream
> LRANGE alerts:recent 0 -1
> HGETALL state:sin
> HGETALL state:zone:ANT
> KEYS '*'
```

**Que dice en voz alta**:

> "Una vez que el publisher publico, los datos quedan en Redis en tres
> estructuras distintas y con propositos distintos. Si se suscriben al
> canal `energy-events` con `SUBSCRIBE energy-events` ven pasar cada
> evento en tiempo real — eso es Pub/Sub, es un bus broadcast efimero.
> Si miran `XLEN energy:stream` ven cuantos ticks llevamos en el
> Stream, que es donde queda la auditoria acotada con un MAXLEN de
> aproximadamente mil entradas. Y si piden `HGETALL state:sin` ven el
> ultimo estado consolidado del sistema en un Hash, que es lo que el
> dashboard lee para mostrar los KPIs."

**Que valida visualmente**:

- Mientras `SUBSCRIBE energy-events` esta activo, mensajes cada 5 s
  (modo simulador) o cada 5 min (modo real).
- `XLEN energy:stream` incrementa monotonicamente.
- `HGETALL state:sin` devuelve los campos `entity_id`, `timestamp`,
  `demanda_mw`, `generacion_mw`, `generacion_solar_mw`, `generacion_eolica_mw`,
  `generacion_hidraulica_mw`, `generacion_termica_mw`, `fuente`.

---

### Paso 4 — Mostrar el procesamiento

**Presenta**: Sebastian (con Alejandro de backup).

**Comandos**:

```bash
make logs-subscriber
```

Y en paralelo, en otra terminal:

```bash
make redis-cli
> HGETALL metrics:renewable
> HGETALL metrics:balance
> HGETALL metrics:demand:variation
```

**Que dice en voz alta**:

> "El subscriber es el segundo proceso del pipeline. Esta suscrito al
> mismo canal `energy-events` que vimos en el paso anterior. Por cada
> tick que recibe ejecuta cinco pasos en orden: calcula las tres
> metricas derivadas — porcentaje renovable, balance
> demanda-generacion, variacion porcentual de la demanda — las
> persiste en los hashes `metrics:*`, agrega el valor de demanda al
> Sorted Set de historico con score igual al timestamp Unix, evalua
> las dos reglas de alerta con debounce de dos ciclos y auto-clear al
> levantar la condicion, y para cada alerta nueva hace una escritura
> multiple en Redis. Si miran los logs del subscriber ven cada uno de
> estos pasos. Y si consultan los hashes `metrics:*` en Redis ven el
> resultado: porcentaje, balance y variacion."

**Que valida visualmente**:

- Lineas en los logs del subscriber consistentes con la actividad del
  publisher.
- Los hashes `metrics:renewable`, `metrics:balance`,
  `metrics:demand:variation` poblados con campos `value`, `unit`,
  `timestamp`, `zone_id`, `fuente`.
- El Sorted Set `metrics:demand:history` crece con cada tick
  (`ZCARD metrics:demand:history`).

---

### Paso 5 — Mostrar la actualizacion del dashboard

**Presenta**: David.

**Comandos**:

No requiere comandos — la actualizacion es visible en el navegador ya
abierto en `http://localhost:5173`.

(Opcional: en otra terminal, suscribirse al SSE para ver el flujo raw.)

```bash
curl -N http://localhost:8000/api/stream
```

**Que dice en voz alta**:

> "El dashboard consume dos canales. Por un lado Server-Sent Events,
> `/api/stream`, que le entrega en tiempo real cada tick del SIN, cada
> alerta nueva y cada conmutacion de fuente. Por otro lado hace polling
> cada cinco segundos a `/api/metrics` para las metricas derivadas y a
> `/api/health` para el banner de modo. La primera carga se hace por
> HTTP a `/api/state` para que la pagina no se vea vacia mientras
> llega el primer tick. Miren como los KPIs de demanda y generacion se
> actualizan en pantalla apenas el publisher publica, sin recargar la
> pagina."

**Que valida visualmente**:

- KPIs de demanda y generacion cambiando numericamente en pantalla
  mientras habla.
- Grafica de "Demanda vs generacion (tiempo real)" acumulando puntos
  en el extremo derecho.
- Banner superior mostrando `MODO: SIMULADOR` o `MODO: REAL` segun
  corresponda.

---

### Paso 6 — Generar una condicion de alerta

**Presenta**: Edwar (con Alejandro de soporte tecnico).

**Comandos**:

Opcion A — desde el dashboard (recomendado, visible para el publico):

Click en el boton **"Déficit crítico"** del panel "Pruebas de stress".
La generacion cae al 80 % durante 30 segundos, lo que dispara A1
(`DEMAND_GENERATION_GAP > 800 MW`).

Opcion B — desde Redis directamente (alternativa didactica):

```bash
docker compose -f infra/docker-compose.yml exec redis redis-cli \
  SET stress:critical_deficit 1 EX 30
```

Esperar dos ciclos del publisher (~10 segundos en modo simulador)
para superar el debounce de 2 ciclos.

**Que dice en voz alta**:

> "Para que se dispare una alerta necesitamos que la condicion se
> mantenga durante dos ciclos consecutivos — ese es el debounce, y
> existe para no spamear alertas por picos espurios. Voy a inyectar un
> escenario de deficit critico desde el dashboard. La generacion cae
> al 80 % durante treinta segundos. Al segundo ciclo deberiamos ver la
> alerta `DEMAND_GENERATION_GAP` aparecer en el panel."

**Que valida visualmente**:

- Boton "Déficit crítico" del dashboard muestra feedback "activado".
- Despues de 2 ciclos (10 s en simulador), aparece una entrada en el
  panel "Alertas recientes" con etiqueta `Déficit de generación`,
  badge `SISTEMA` (porque A1 es de alcance global) y severidad HIGH
  (rojo).
- KPI "Balance" se pinta en rojo cuando supera el umbral de 800 MW.

---

### Paso 7 — Mostrar como cambia el dashboard

**Presenta**: David.

**Comandos**:

No requiere comandos. La observacion es en el navegador.

(Opcional, para mostrar el auto-clear: click en el boton
**"Recuperación"** del panel "Pruebas de stress". Eso limpia los
escenarios activos.)

**Que dice en voz alta**:

> "Miren como el panel de alertas ahora muestra la alerta critica con
> la severidad en rojo. La alerta queda activa mientras la condicion
> se mantenga. Cuando la condicion levanta — por ejemplo porque
> tocan el boton Recuperacion, o porque expira el TTL de 30 segundos —
> el subscriber emite una segunda alerta con `state: cleared`, que el
> dashboard atenua visualmente en lugar de borrarla. Eso es
> importante para el operador: durante la operacion interesa ver
> tanto que la alerta se disparo como que el sistema la resolvio solo.
> Ademas, los KPIs `Generación renovable` y `Balance` cambiaron de
> color: el balance a rojo porque esta por encima del umbral de 800
> MW, y el porcentaje de generacion renovable a amarillo si bajo del
> 30 %."

**Que valida visualmente**:

- Panel de alertas con la alerta `Déficit de generación` activa.
- KPI `Balance` (dem − gen) en rojo.
- Si se pulso `Recuperacion` despues, una segunda entrada con la
  misma alerta pero atenuada y con sufijo " · resuelta".

---

### Paso 8 — Consultar el estado actual

**Presenta**: Sebastian.

**Comandos**:

```bash
make redis-cli
> KEYS '*'
> HGETALL state:sin
> HGETALL state:zone:ANT
```

O desde la API:

```bash
curl http://localhost:8000/api/state | python -m json.tool
```

**Que dice en voz alta**:

> "Para consultar el estado actual tenemos dos caminos. El primero es
> ir directamente a Redis: las claves `state:zone:*` y `state:sin`
> guardan el ultimo tick publicado por zona. La diferencia es que
> `state:zone:*` tiene TTL de 24 horas — si una zona deja de publicar
> durante un dia, desaparece y el dashboard la pinta en gris. El
> segundo camino es la API REST en `/api/state`, que devuelve los
> mismos datos ya validados como Pydantic, en formato JSON, listos
> para consumir desde el dashboard u otro servicio."

**Que valida visualmente**:

- `KEYS '*'` devuelve el conjunto completo de claves del sistema
  (`state:zone:ANT`, `state:zone:VAL`, ..., `metrics:renewable`,
  `energy:stream`, etc.).
- `HGETALL state:sin` devuelve el snapshot consolidado mas reciente.
- La respuesta JSON de `/api/state` tiene la forma
  `{sin: ZoneState, zones: [ZoneState × 5]}`.

---

### Paso 9 — Consultar el historico reciente

**Presenta**: Sebastian.

**Comandos**:

```bash
make redis-cli
> XLEN energy:stream
> XRANGE energy:stream - + COUNT 5
> ZRANGE metrics:demand:history 0 -1 WITHSCORES
> XLEN alerts:stream
```

O desde la API:

```bash
curl http://localhost:8000/api/alerts | python -m json.tool
curl http://localhost:8000/api/metrics | python -m json.tool
```

**Que dice en voz alta**:

> "Para el historico tenemos cuatro estructuras distintas en Redis.
> `energy:stream` guarda los ultimos aproximadamente mil ticks como
> log append-only con `MAXLEN`. `metrics:demand:history` es un Sorted
> Set donde el score es el timestamp Unix y el miembro es el valor de
> demanda — eso nos da el historico temporal con consultas por rango
> de tiempo. `alerts:stream` audita las ultimas cien alertas. Y
> `alerts:recent` es una List con los veinte IDs mas recientes para
> acceso rapido. La API expone `/api/alerts` y `/api/metrics` que ya
> reconstruyen esos datos crudos en respuestas validadas, incluyendo
> la conversion del literal `NaN` a `null` cuando M3 aun no tiene
> historico."

**Que valida visualmente**:

- `XLEN energy:stream` con un valor cercano al MAXLEN (1000).
- `XRANGE energy:stream - + COUNT 5` muestra los 5 eventos mas
  recientes con todos sus campos.
- `ZRANGE metrics:demand:history 0 -1 WITHSCORES` muestra pares
  `(demanda, unix_ts)`.
- La respuesta de `/api/alerts` tiene la forma
  `{recent: list[Alert], active: dict[str, int]}`.

---

## Preguntas frecuentes

Veinte preguntas que el profesor o el jurado pueden hacer durante la
presentacion, con respuestas cortas y tecnicamente correctas basadas
en el codigo real del proyecto.

### 1. ¿Por que utilizar Redis en este proyecto?

Redis ofrece latencia sub-milisegundo, multiples estructuras
especializadas en un solo proceso (Pub/Sub, Streams, Hashes, Sorted
Sets, Lists, Counters) y operaciones atomicas nativas como `INCR` o
`XADD ... MAXLEN`. Para un bus de eventos en tiempo real eso evita
tener que mantener una cola externa, una base de datos relacional y
un broker de mensajes por separado. La capa gratuita de Redis Cloud
o un contenedor local de Redis 7 alcanzan para la carga del taller.

### 2. ¿Que informacion se mantiene en memoria?

Estado actual por zona en hashes `state:zone:*` y `state:sin` (TTL 24
h); metricas derivadas en `metrics:*`;Sorted Set de historico de
demanda en `metrics:demand:history` (ventana 1 h); alertas en Stream
`alerts:stream`, List `alerts:recent` y counters `alerts:active:*`;
claves de salud `health:*` con modo, fallos y timestamps; flags de
stress `stress:*` con TTL 30 s; y el Stream `energy:stream` con los
ultimos mil ticks como auditoria acotada.

### 3. ¿Que informacion representa el estado actual?

El ultimo tick publicado por el publisher para cada zona geografica y
para el consolidado nacional. Vive en los hashes `state:zone:{ANT,
VAL, ATL, BOG, SAN}` con los campos `demanda_mw`, `generacion_mw`,
`generacion_solar_mw`, `generacion_eolica_mw`,
`generacion_hidraulica_mw`, `generacion_termica_mw`, `timestamp`,
`fuente`, y en `state:sin` con la misma estructura para el agregado
nacional. TTL de 24 horas.

### 4. ¿Que informacion representa el historico?

Cuatro estructuras: el Stream `energy:stream` con los ultimos
aproximadamente mil ticks en orden cronologico (auditoria completa);
el Sorted Set `metrics:demand:history` con los valores de demanda
como miembros y los timestamps Unix como score (consultable por rango
de tiempo, ventana de 1 h); el Stream `alerts:stream` con las
ultimas cien alertas; y la List `alerts:recent` con los veinte IDs
mas recientes como indice rapido al Stream.

### 5. ¿Por que seleccionaron las estructuras de Redis utilizadas?

Cada estructura cumple un rol distinto que las otras no cumplen tan
bien. Pub/Sub es broadcast en tiempo real y es lo unico que entrega
"ahora mismo" sin pedirlo. Stream es append-only con MAXLEN y
consumer groups para auditoria acotada. Hash da lookup O(1) del
ultimo estado por zona sin recorrer el Stream. Sorted Set permite
consultas por rango de tiempo (`ZRANGEBYSCORE`) que las demas no.
List capada con LPUSH + LTRIM es lo mas simple para "ultimos N".
Counter con INCR es atomico, no se puede simular igual con un Hash.

### 6. ¿Cual es la funcion del Publisher?

Capturar datos de la fuente comprometida (XM real o simulador) en
cada ciclo, normalizarlos al modelo `Event` definido en
`common/models.py`, y publicarlos en tres destinos Redis en una sola
transaccion `MULTI/EXEC`: `PUBLISH energy-events` para entrega en
tiempo real, `XADD energy:stream MAXLEN ~ 1000` para auditoria, y
`HSET state:zone:{zid}` con `EXPIRE 86 400` para estado actual. El
aislamiento per-evento garantiza que un fallo de Redis en una de las
escrituras no aborte el resto del lote.

### 7. ¿Cual es la funcion del Subscriber?

Consumir el canal `energy-events` con polling acotado
(`pubsub.get_message(timeout=1.0)`) y, por cada tick recibido,
calcular las tres metricas derivadas, persistirlas en los hashes
`metrics:*`, agregar la demanda al Sorted Set `metrics:demand:history`,
evaluar las reglas de alerta A1 y A2 con debounce de 2 ciclos y
auto-clear al levantar condicion, y publicar por el mismo canal cada
alerta nueva. Tambien corre housekeeping cada 60 segundos para podar
el Sorted Set a la ventana de 1 hora.

### 8. ¿Que informacion se transmite mediante Pub/Sub?

Todo viaja por un unico canal `energy-events` con un discriminador
`type` en el payload JSON. Tres tipos: `tick` (publisher, contiene el
`Event` aplanado), `alert` (subscriber, contiene la `Alert`
serializada con `id`, `code`, `rule`, `severity`, `zone_id`, `value`,
`threshold`, `state`, `consecutive_cycles`, `timestamp`, `message`),
y `source_switch` (publisher, anuncia cambio de modo con `mode`,
`timestamp` y `consecutive_failures`).

### 9. ¿Que sucederia si el Subscriber se desconecta?

Pub/Sub es broadcast efimero: los mensajes que se publiquen mientras
el subscriber esta caido se pierden para el. Pero el estado actual
sobrevive en los hashes `state:zone:*` (TTL 24 h) y en `state:sin`: al
reconectarse, el dashboard vuelve a leerlos y el sistema sigue
operando. El historico completo esta en `energy:stream` (MAXLEN 1000)
y se puede reprocesar con `XREAD` desde el ultimo ID visto. La
ventana de perdida depende de cuanto tiempo este caido el subscriber
y cuantos ticks se publican mientras tanto.

### 10. ¿Que sucede con los datos cuando Redis se reinicia?

Por defecto Redis es volatil (sin RDB ni AOF). En este proyecto el
`docker-compose.yml` define un volumen `redis-data:/data`, asi que el
daemon de Redis 7 aplica su politica RDB por defecto (snapshots
periodicos), pero no esta configurado explicitamente para AOF. Si el
contenedor se reinicia con persistencia, los datos sobreviven; sin
ella, todo el estado en memoria se pierde y el banner muestra
`SIN CONEXION` brevemente hasta que publisher, subscriber y API se
reconectan y arrancan vacios. El sistema es resilient: el primer
ciclo post-reinicio repuebla todos los hashes y streams.

### 11. ¿Como controlan el crecimiento de los datos?

Con cinco mecanismos combinados. `XADD ... MAXLEN ~ 1000 approximate`
en `energy:stream` y `MAXLEN ~ 100` en `alerts:stream` para acotar el
Stream. `LPUSH alerts:recent` seguido de `LTRIM 0 19` para mantener
exactamente veinte entradas en la lista. `ZREMRANGEBYSCORE
metrics:demand:history -inf (now - 3600)` ejecutado por el
housekeeping cada 60 segundos para mantener la ventana de 1 hora en
el Sorted Set. `EXPIRE 86 400` en cada escritura de los hashes
`state:zone:*` para que zonas inactivas desaparezcan al cabo de un
dia. Y `EX 30` en los flags `stress:*` para que un escenario
inyectado se autoexpire a los 30 segundos.

### 12. ¿Como generan los eventos?

Dos fuentes, seleccionadas por `SourceSelector`. La fuente real hace
un `GET` HTTP asincrono al endpoint publico de XM
`DemandaTiempoReal` (sin autenticacion, cadencia regulatoria de 5
minutos). La fuente simulada genera demanda nacional sintetica con un
paseo aleatorio acotado sobre una curva circadiana que tiene valle a
las 3-5 de la madrugada y pico a las 19 horas. Ambos caminos pasan
por `publisher/normalizer.py:build_events` para producir los seis
eventos por ciclo (cinco zonas geograficas mas el SIN global) con
el mismo formato `Event` de Pydantic. Para inyectar escenarios
anomalos hay cuatro botones en el dashboard que escriben flags en
Redis con TTL de 30 segundos.

### 13. ¿Que metricas se calculan?

Tres metricas. M1 es el porcentaje de generacion renovable, calculada
como `(solar + eolica + hidraulica) / generacion_total * 100`; si la
generacion es cero, M1 vale cero. M2 es el balance
demanda-generacion en MW, con signo positivo cuando hay deficit. M3
es la variacion porcentual de la demanda entre el tick actual y el
anterior, calculada como `(demanda_actual - demanda_previa) /
demanda_previa * 100`; si no hay historico previo, M3 es `None` y se
persiste como literal `"NaN"` que la API coacciona a `null` en JSON.

### 14. ¿Como se generan las alertas?

El `AlertEngine` en `subscriber/alerts.py` evalua dos reglas por cada
tick: `DEMAND_GENERATION_GAP` (A1, severidad HIGH, se dispara cuando
`demanda - generacion > 800 MW`, alcance global del SIN con
`zone_id=""`) y `LOW_RENEWABLE` (A2, severidad MEDIUM, se dispara
cuando M1 cae por debajo del 30 %, alcance por zona). Cada regla
tiene su propio contador `consecutive_cycles` por tupla `(rule,
zone_id)`, y aplica debounce de dos ciclos: el primer ciclo se
suprime, el segundo publica la alerta como `active`, los siguientes
son idempotentes, y cuando la condicion levanta se publica una
alerta con `state: "cleared"`.

### 15. ¿Que parte del sistema funciona en tiempo real?

El flujo completo de extremo a extremo: publisher captura, normaliza y
publica; subscriber consume, calcula, evalua y persiste; la API lee y
mantiene una conexion SSE por cliente con `pubsub.get_message
(timeout=1.0)` y heartbeat cada 15 segundos; el dashboard recibe los
eventos por SSE y actualiza los KPIs, las graficas y el panel de
alertas sin recargar la pagina. La latencia entre el `PUBLISH` del
publisher y el repintado en el navegador es inferior a un segundo en
modo simulador.

### 16. ¿Que ventajas tiene utilizar una base de datos en memoria?

Latencia sub-milisegundo en operaciones simples, lo que hace viable el
flujo en tiempo real. Estructuras especializadas que cubren casos que
en una base relacional requieren tablas adicionales, codigo de
aplicacion e indices: Pub/Sub para broadcast, Streams para auditoria
con MAXLEN, Sorted Sets para series temporales, Counters atomicos con
INCR, TTL nativo en cualquier clave, LTRIM y ZREMRANGEBYSCORE para
acotar crecimiento. Tambien simplifica la operacion: un solo proceso
hace de bus de eventos, almacenamiento temporal y broker de mensajes.

### 17. ¿Que limitaciones tiene Redis frente a una base de datos tradicional?

Sin durabilidad por default: si Redis se cae sin AOF configurado, los
datos en memoria se pierden. En modo simple es un solo nodo (Redis
Cluster existe pero anade complejidad operacional y requiere que las
claves caigan en el mismo slot hash para transacciones). No tiene
lenguaje de consultas tipo SQL: no hay joins, subqueries, indices
secundarios, ni constraints relacionales. Los Streams tienen semantica
propia (entries con ID auto-generado, no tablas con primary key). Y
el modelo de memoria asume que el dataset completo cabe en RAM, lo
que escala verticalmente bien pero horizontalmente requiere sharding
manual.

### 18. ¿Que ocurriria si el numero de sensores o eventos aumentara significativamente?

El loop secuencial del publisher se saturaria al consultar muchas
fuentes externas. La solucion natural es paralelizar: multiples
publishers publicando a canales distintos shardeados por zona, o un
solo publisher con un pool de fetches asincronos. El subscriber
tambien se saturaria con un solo proceso: pasariamos de Pub/Sub a
Redis Streams con consumer groups, donde cada consumer del grupo
procesa un subconjunto de las entradas y el sistema escala
horizontalmente. La API ya es stateless y escala detras de un
balanceador. El SSE tiene una conexion por cliente, asi que el pool
de Redis se llenaria rapido con cientos de clientes; en ese caso se
mueve a un pubsub dedicado por shard.

### 19. ¿Como modificarian la arquitectura para soportar multiples Publishers y Subscribers?

Multiples subscribers es el camino mas directo: usar Redis Streams
con consumer groups en lugar de Pub/Sub, donde cada consumer del
grupo recibe un subconjunto de las entradas y el grupo garantiza que
cada entrada la procesa exactamente uno. Asi no hay duplicacion de
trabajo y el sistema escala horizontalmente. Para multiples
publishers el reto es coordinar para no duplicar publicaciones: o
bien se particiona el espacio (un publisher por zona geografica, por
ejemplo), o bien se introduce un lock distribuido basado en Redis
para que solo el publisher que obtiene el lock publique en cada
ciclo. La API no requiere cambios: sigue leyendo los mismos hashes y
streams, agnostics a cuantos publishers hay detras.

### 20. ¿Que componentes podrian escalar horizontalmente?

El subscriber es el candidato mas claro: pasar de Pub/Sub a Redis
Streams con consumer groups permite que N instancias del subscriber
se repartan el trabajo sin duplicar. La API FastAPI es stateless (no
guarda estado entre requests, solo abre un cliente Redis por proceso)
y escala trivialmente detras de un balanceador HTTP, aunque hay que
tener en cuenta que cada instancia abre conexiones SSE con su propio
pubsub. El dashboard es un build estatico y se sirve desde una CDN
sin cambios. El publisher es el caso mas complejo: si hay varios,
hay que coordinar para no publicar el mismo evento dos veces; una
estrategia es particionar por zonas, otra es un lock distribuido.
Redis mismo escala con Cluster, pero entonces todas las claves
tocadas por una transaccion `MULTI/EXEC` tienen que caer en el
mismo slot hash, lo que requiere hashtag routing en los nombres de
clave.

---

## Cierre

### Tiempo total estimado

La demo en vivo esta pensada para durar entre 10 y 15 minutos,
distribuida aproximadamente asi:

| Paso | Duracion estimada |
|------|-------------------|
| Previo (verificacion de servicios) | 1 minuto |
| Paso 1 (publisher en marcha) | 1 minuto |
| Paso 2 (generar evento) | 1 minuto |
| Paso 3 (evento en Redis) | 1 minuto |
| Paso 4 (procesamiento) | 1 minuto |
| Paso 5 (dashboard) | 1 minuto |
| Paso 6 (condicion de alerta) | 1 minuto |
| Paso 7 (cambio visual del dashboard) | 1 minuto |
| Paso 8 (estado actual) | 1 minuto |
| Paso 9 (historico reciente) | 1 minuto |
| Preguntas del jurado | 5 minutos de buffer |

Sumando los 9 pasos: ~9 minutos de demo + verificacion previa + Q&A.

### Plan B — Si algo falla durante la demo

Si la demo local se cae (Docker no levanta, puerto ocupado,
backend no responde, navegador no abre), la opcion es Plan B: exponer
el stack a traves de un tunnel `cloudflared` desde otra maquina donde
todo este funcionando. Sebastian o Jonathan deben tener un dominio
configurado bajo Cloudflare (`cloudflared tunnel login`,
`cloudflared tunnel create red-electrica-demo`, `cloudflared tunnel
route dns red-electrica-demo red.tudominio.com`) y el comando
`cloudflared tunnel --url http://localhost:8000 run red-electrica-demo`
listo para correr. La guia completa esta en
[`docs/DEPLOY.md`](docs/DEPLOY.md) seccion 2.

### Notas finales para los presentadores

- **No asumir que el publico vio el README**: cuando un termino
  tecnico pueda ser ambiguo, anadir una frase de contexto en voz
  alta. Por ejemplo, la primera vez que se menciona "consumer group"
  decir "que es el mecanismo de Redis Streams para repartir trabajo
  entre varios consumers".
- **No improvisar numeros**: si una pregunta pide un valor concreto
  (umbral, MAXLEN, TTL), tenerlo en este guion o leerlo del codigo
  en vivo antes de responder.
- **Si una pieza no responde, no alargar**: pasar al siguiente paso
  y volver al que fallo solo si queda tiempo. La mayoria de los
  flujos estan desacoplados: si Redis Pub/Sub funciona, los pasos 1,
  3 y 4 son independientes del dashboard.
- **Tener siempre una terminal abierta con `make redis-cli`**: los
  pasos 3, 8 y 9 son inspeccionables en cualquier momento aunque no
  sean el foco del momento.
