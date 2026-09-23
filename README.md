# Monitor de Red Eléctrica Colombiana en Tiempo Real con Redis

Sistema de monitoreo en tiempo real del Sistema Interconectado Nacional
(SIN) colombiano que captura, procesa, almacena y visualiza datos de
demanda, generación y composición energética consumidos desde el portal
público de [XM, operador del mercado eléctrico colombiano](https://www.xm.com.co/).

> **Disclaimer**: la magnitud nacional agregada sí refleja el dato real
> publicado por XM cuando el modo es REAL. El reparto por zonas y el
> despacho de generación son una simplificación documentada del diseño
> (no una medición); para datos auditables consultar XM directamente.
> Ver §1 más abajo.

---
## Integrantes
- Edwar Fonseca
- Alejandro Huerfano
- Sebastian Lizarazo
- David Quijano
- Jonathan Romero

## Tabla de contenidos

1. [Descripción del problema](#1-descripción-del-problema)
2. [Arquitectura](#2-arquitectura)
3. [Tecnologías utilizadas](#3-tecnologías-utilizadas)
4. [Fuente de datos](#4-fuente-de-datos)
5. [Estructura de los eventos](#5-estructura-de-los-eventos)
6. [Estructuras Redis utilizadas](#6-estructuras-redis-utilizadas)
7. [Canales Pub/Sub](#7-canales-pubsub)
8. [Streams](#8-streams)
9. [Procesamiento de extremo a extremo](#9-procesamiento-de-extremo-a-extremo)
10. [Métricas derivadas](#10-métricas-derivadas)
11. [Reglas de alerta](#11-reglas-de-alerta)
12. [Diseño del dashboard](#12-diseño-del-dashboard)
13. [Instalación](#13-instalación)
14. [Ejecución del stack completo](#14-ejecución-del-stack-completo)
15. [Dificultades encontradas](#15-dificultades-encontradas)
16. [Conclusiones](#16-conclusiones)

---

## 1. Descripción del problema

El SIN colombiano publica datos de demanda en tiempo real con cadencia
regulatoria de cinco minutos. Esa frecuencia es suficiente para análisis
histórico y despacho de generación, pero deja una ventana temporal en la
que un operador no puede ver el efecto de una perturbación (salida de
una unidad térmica, baja súbita de generación hidráulica, pico de
demanda residencial) hasta el siguiente ciclo de XM. El sistema
propuesto resuelve esa ventana integrando:

- **Captura continua** desde el endpoint público `DemandaTiempoReal` de
  XM, con fallback automático a un simulador cuando la fuente real no
  está disponible o cae por debajo de tres respuestas exitosas seguidas.
- **Cómputo de métricas operativas** (% renovable, balance
  demanda-generación, variación porcentual de la demanda) sobre cada
  evento recibido.
- **Generación de alertas** con debounce y ciclo de vida
  `active` / `cleared`, según umbrales configurables.
- **Distribución en tiempo real** vía Pub/Sub y Server-Sent Events
  (SSE) hacia un dashboard web que actualiza sin recargar.

El alcance es académico: la magnitud nacional agregada es real cuando
el modo es REAL, pero el reparto por zonas y el despacho de generación
son una simplificación documentada del diseño, no una medición auditada
de XM. Ver `docs/README_TECNICO.md` §1 para el detalle.

## 2. Arquitectura

```
                    API XM pública                    SIMULADOR (fallback)
                  (serviciosfacturacion)                (en proceso)
                          │                                   │
                          │   GET DemandaTiempoReal           │
                          ▼                                   ▼
   ┌──────────────────────────────────────────────────────────────┐
   │                      PUBLISHER  (Python)                    │
   │                                                              │
   │   SourceSelector  ──► Normalizer  ──► _publicar(evento)      │
   │   (REAL/SIM/DEGRADADO)   (Pydantic)    (pipeline MULTI/EXEC) │
   └──────────────────────────────────────────────────────────────┘
                          │
                          │  3 escrituras atómicas:
                          │  - PUBLISH energy-events
                          │  - XADD    energy:stream   (MAXLEN 1000)
                          │  - HSET    state:zone:*   + EXPIRE 24 h
                          ▼
   ┌──────────────────────────────────────────────────────────────┐
   │                       REDIS  (bus de datos)                  │
   │                                                              │
   │  Pub/Sub    Streams      Hashes         Sorted Sets   Lists  │
   │  energy-    energy:      state:zone:*   metrics:      alerts:│
   │  events     stream       state:sin      demand:       recent │
   │             alerts:      metrics:*      history                 │
   │             stream       alerts:active:*                       │
   │                                                              │
   │  Counters   Health keys  Stress flags                         │
   │  alerts:    health:mode  stress:demand_surge (TTL 30s)       │
   │  total      health:failures                                 │
   │             health:source_switches                           │
   └──────────────────────────────────────────────────────────────┘
                          │
                          │  SUBSCRIBE energy-events
                          ▼
   ┌──────────────────────────────────────────────────────────────┐
   │              SUBSCRIBER + PROCESSOR  (Python)                │
   │                                                              │
   │   _handle_tick:                                              │
   │     1. compute_metrics(event, previous_demand_mw)           │
   │     2. persist_metrics(HSET x 3)                            │
   │     3. ZADD metrics:demand:history <unix_ts> <demanda>       │
   │     4. alert_engine.evaluate(event, metrics)                │
   │     5. for each NEW alert: publish_alert                    │
   │                                                              │
   │   Housekeeping cada 60 s: ZREMRANGEBYSCORE (ventana 1 h)     │
   └──────────────────────────────────────────────────────────────┘
                          │
                          │  HGETALL / ZRANGE / LRANGE / XREVRANGE
                          ▼
   ┌──────────────────────────────────────────────────────────────┐
   │               API  (FastAPI + uvicorn, async)                │
   │                                                              │
   │  GET /api/state     estado actual SIN + 5 zonas             │
   │  GET /api/metrics   3 métricas derivadas                     │
   │  GET /api/alerts    alertas recientes (20) + counters        │
   │  GET /api/health    modo, failures, uptime, redis_ok         │
   │  GET /api/stream    SSE: tick / alert / source_switch        │
   │  POST /api/stress/{event}  inyectar escenario (TTL 30 s)    │
   └──────────────────────────────────────────────────────────────┘
                          │
                          │  HTTP polling (banner + métricas)
                          │  SSE text/event-stream (tick + alert)
                          ▼
   ┌──────────────────────────────────────────────────────────────┐
   │              DASHBOARD  (Vite + Chart.js + Leaflet)          │
   │                                                              │
   │  Banner modo · 4 KPI cards · 2 charts · mapa 5 zonas ·      │
   │  panel alertas · 4 botones stress                            │
   └──────────────────────────────────────────────────────────────┘
```

El publisher, subscriber y API corren como procesos separados dentro de
contenedores Docker orquestados por `infra/docker-compose.yml` y se
comunican únicamente a través de Redis, que actúa como bus de datos
compartido (no hay llamadas HTTP entre módulos del backend). El
dashboard es estático y consume la API por HTTP + SSE.

## 3. Tecnologías utilizadas

| Capa | Tecnología | Versión | Rol |
|------|------------|---------|-----|
| Backend | Python | 3.11+ | Lenguaje único del backend |
| Backend | FastAPI | 0.115+ | Framework HTTP async de la API |
| Backend | uvicorn | (último estable) | Servidor ASGI |
| Redis client | `redis-py` asyncio | 5.x | Cliente Redis async |
| Validación | Pydantic v2 | 2.x | Modelos del dominio y validación |
| Config | `pydantic-settings` | 2.x | Carga de `.env` + env vars |
| HTTP async | `httpx` | (último estable) | Cliente HTTP al endpoint XM |
| SSE | `sse-starlette` | (último estable) | Server-Sent Events para `/api/stream` |
| Tests | `pytest` + `pytest-asyncio` + `pytest-cov` | (último estable) | Suite con `fakeredis` |
| Mock HTTP | `respx` | (último estable) | Mock de `httpx` para tests |
| Redis fake | `fakeredis` | 2.x | Tests sin contenedor Redis |
| Frontend | Vite | 5 | Build del dashboard |
| Charts | Chart.js | 4 | Gráficas de líneas |
| Mapa | Leaflet | 1.9 | Mapa de zonas del SIN |
| Infra | Docker + docker compose | (último estable) | Pipeline reproducible |

## 4. Fuente de datos

El sistema soporta dos fuentes, seleccionadas en cada ciclo por
`publisher/source_selector.py`:

### XM real (`XMRealSource`)

- Endpoint: `https://serviciosfacturacion.xm.com.co/XM.Portal.Indicadores/api/Operacion/DemandaTiempoReal`
- Sin autenticación.
- Cadencia: cinco minutos (regulatoria, no técnica). El cliente la respeta
  releyendo cada `PUBLISHER_INTERVAL_SECONDS_REAL` segundos (default 300).
- Política de errores: tres reintentos con backoff exponencial sobre
  fallos transitorios (timeout, 5xx, error de red). Los 4xx no se
  reintentan — son deterministas.
- Excepciones tipadas: `DataSourceTimeoutError`, `DataSourceError`,
  `InvalidXMResponseError` (en `common/data_source.py`).

### Simulador (`SimulatorSource`)

- Genera eventos sintéticos con patrón circadiano (valle 3-5 h, pico
  19 h) mediante un paseo aleatorio acotado para que las curvas sean
  continuas y no ruido blanco.
- Cuatro escenarios de estrés inyectables (TTL 30 s en Redis):
  `demand_surge` (+25 %), `hydro_drop` (hidro al 45 %),
  `critical_deficit` (generación al 80 %), `recovery` (limpia los otros).
- Distribución por zonas y despacho por recurso: lo hace
  `publisher/normalizer.py`, el mismo código que consume la fuente real
  (publisher agnóstico de la fuente).

### Modo automático (auto)

Si la variable de entorno `FORCE_SOURCE` está vacía (default), el
selector opera en modo automático con la siguiente máquina de estados:

1. Arranca en `REAL`. Cada fallo consecutivo de XM incrementa un
   contador.
2. Al tercer fallo consecutivo, conmuta a `SIM` y persiste
   `health:mode = "simulator"`.
3. El retorno a `REAL` se programa con backoff exponencial
   (5 → 10 → 20 → 30 min, capado).
4. Cuando un reintento tiene éxito, vuelve a `REAL` y resetea el
   contador.

Durante la ventana de fallos (modo `real` con `failures > 0`),
cada ciclo se sirve igual con el simulador para que el pipeline no se
seque. El banner del dashboard pinta ese estado como **DEGRADADO**
(distinto del modo `simulator` estable). Ver `dashboard/src/components/banner.js`.

Para forzar una fuente de manera estable (por ejemplo, demo sin
internet): `FORCE_SOURCE=simulator` o `FORCE_SOURCE=real`.

## 5. Estructura de los eventos

Los eventos se modelan con Pydantic v2 (`frozen=True`, fechas
**aware UTC**) en `common/models.py`.

### Tipos principales

```python
class ZoneId = Literal["ANT", "VAL", "ATL", "BOG", "SAN", "SIN"]

class EventData:
    demanda_mw: float             # ≥ 0, ≤ 50 000 MW
    generacion_mw: float           # ≥ 0, ≤ 50 000 MW
    generacion_solar_mw: float     # ≥ 0, ≤ 50 000 MW
    generacion_eolica_mw: float    # ≥ 0, ≤ 50 000 MW
    generacion_hidraulica_mw: float
    generacion_termica_mw: float
    fuente: DataSource            # REAL | SIM

class Event:
    entity_id: ZoneId             # ANT / VAL / ATL / BOG / SAN / SIN
    timestamp: AwareDatetime
    location: Location            # lat/lon WGS84
    data: EventData
```

### Zonas geográficas

| ZoneId | Zona | Coordenada de referencia (lat, lon) |
|--------|------|--------------------------------------|
| `ANT` | Antioquia | 6.2442, -75.5812 |
| `VAL` | Valle del Cauca | 3.4516, -76.5320 |
| `ATL` | Atlántico - Caribe | 10.9639, -74.7964 |
| `BOG` | Bogotá - Cundinamarca | 4.7110, -74.0721 |
| `SAN` | Santander | 7.1193, -73.1227 |
| `SIN` | Agregado nacional | — |

`SIN` no es una zona geográfica sino el consolidado nacional que
alimenta KPIs y charts. Su clave Redis es `state:sin` (sin sufijo
`zone:`).

### Discriminador `fuente`

Cada `EventData` lleva su `fuente` (`REAL` o `SIM`). Ningún evento
publicado se presenta como real cuando no lo es: si XM falla en un
ciclo, el ciclo se sirve con datos del simulador etiquetados como
`SIM`. Esto se valida en `tests/unit/test_publisher_main.py`.

## 6. Estructuras Redis utilizadas

Todas las claves están centralizadas en `common/redis_keys.py`. Los
factories blindan contra typos. Mapeo por estructura:

### Hashes (estado actual, una entrada por zona + métricas)

| Clave | Contenido | TTL |
|-------|-----------|-----|
| `state:sin` | snapshot consolidado (demanda, generación, timestamp, fuente) | 24 h |
| `state:zone:ANT`, `state:zone:VAL`, `state:zone:ATL`, `state:zone:BOG`, `state:zone:SAN` | snapshot por zona | 24 h |
| `metrics:renewable` | `{value, unit, timestamp, zone_id, fuente}` de M1 | sin TTL |
| `metrics:balance` | idem de M2 | sin TTL |
| `metrics:demand:variation` | idem de M3 (`value` puede ser literal `"NaN"` el primer tick) | sin TTL |

TTL `STATE_ZONE_TTL_SECONDS = 86 400` (24 h): zona inactiva = flag
visual, no borrado (ver `api/routers/state.py`).

### Streams (histórico acotado, append-only)

| Clave | MAXLEN | Aprox. cobertura |
|-------|--------|------------------|
| `energy:stream` | 1 000 | ~83 min a 5 s; ~3,5 días a 5 min |
| `alerts:stream` | 100 | últimos ~100 eventos de alerta |

Los streams sobreviven a reinicios del subscriber: un consumer
reconectado puede leer desde el último ID visto con `XREAD ... LAST`.

### Sorted Sets (histórico temporal con score = unix ts)

| Clave | Score | Miembro |
|-------|-------|---------|
| `metrics:demand:history` | unix_ts del tick | demanda_mw serializada |

Ventana: 60 minutos. El housekeeping del subscriber ejecuta
`ZREMRANGEBYSCORE` cada 60 s para podar entradas más viejas que
`now - DEMAND_HISTORY_WINDOW_SECONDS`.

### Lists (capadas con LPUSH + LTRIM)

| Clave | Cap | Uso |
|-------|-----|-----|
| `alerts:recent` | 20 | IDs de las alertas más recientes (newest-first) |

### Counters y strings (INCR / SET)

| Clave | Tipo | Uso |
|-------|------|-----|
| `alerts:total` | INCR | total acumulado de alertas emitidas |
| `alerts:active:DEMAND_GENERATION_GAP` | INCR/DECR | contador activo de A1 |
| `alerts:active:LOW_RENEWABLE` | INCR/DECR | contador activo de A2 |
| `health:mode` | string | `real` o `simulator` |
| `health:failures` | string numérico | fallos consecutivos de XM |
| `health:source_switches` | string numérico | conmutaciones REAL↔SIM |
| `health:xm:last_success` | ISO timestamp | última respuesta exitosa |
| `health:xm:last_failure` | ISO timestamp | último fallo |
| `health:next_retry_at` | ISO timestamp | próximo reintento a XM |
| `health:subscriber:started_at` | ISO timestamp | boot del subscriber |
| `stress:{evento}` | string con TTL 30 s | flag de escenario inyectado |

`KEY_STRESS_TTL_SECONDS = 30`: el blast radius de un escenario
olvidado queda acotado a medio minuto (ver `publisher/simulator.py`).

## 7. Canales Pub/Sub

Un único canal: **`energy-events`**. Toda la información en tiempo
real viaja por él, diferenciada por el discriminador `type` en el
payload JSON. Tres tipos canónicos:

| `type` | Origen | Wire format |
|--------|--------|-------------|
| `tick` | publisher (`_publicar`) | `Event` aplanado como JSON con `type: "tick"` antepuesto |
| `alert` | subscriber (`publish_alert`) | `Alert` serializado con `id`, `code`, `rule`, `severity`, `zone_id`, `value`, `threshold`, `state`, `consecutive_cycles`, `timestamp`, `message` |
| `source_switch` | publisher (`_publicar_source_switch`) | `{type, mode, timestamp, consecutive_failures}` |

### Quién escucha

- El **subscriber** (`subscriber/processor.py:EnergyProcessor.run`) para
  consumir `tick` y `source_switch`.
- Cada conexión SSE abierta en la API (`api/routers/stream.py`): una
  instancia de `redis.pubsub()` por request, suscrita al mismo canal,
  con cleanup garantizado en `try/finally: unsubscribe + aclose`.
- Cualquier cliente externo puede hacer `SUBSCRIBE energy-events` para
  ver el flujo en vivo (`make redis-cli` → `SUBSCRIBE energy-events`).

Pub/Sub es **broadcast efímero**: si un consumidor está desconectado,
pierde los mensajes. Por eso cada `tick` también se persiste en
`energy:stream` y en los hashes `state:zone:*` — la fuente de verdad
para "estado actual" e "histórico" es la estructura durable, no el
canal.

## 8. Streams

El sistema usa dos Streams con `MAXLEN` aproximado:

### `energy:stream` (auditoría de ticks)

Append-only. Cada `tick` que publica el `Publisher` se escribe aquí con
`XADD ... MAXLEN ~ 1000`. Sobrevive a reinicios: un subscriber que se
reconecta puede leer los mensajes que se publicaron mientras estaba
caído, aunque el flujo principal del sistema no los use (replay no es
parte del contrato; ver `api/routers/stream.py` que explícitamente
dice "MUST NOT replay eventos pasados").

### `alerts:stream` (auditoría de alertas)

Append-only, con `MAXLEN ~ 100`. Cada alerta que `publish_alert`
publica también se escribe acá como `{"payload": <json>}`. La API usa
`XREVRANGE alerts:stream + - COUNT 20` en `/api/alerts` para
reconstruir las alertas con todos sus campos.

`XADD` se llama con `approximate=True` para que el `MAXLEN` sea
aproximado y Redis no haga un escaneo exacto en cada escritura
(mejor rendimiento, sin perder la cota superior).

## 9. Procesamiento de extremo a extremo

```
[XM] ── HTTP GET ──► [Publisher] ── PUBLISH+XADD+HSET ──► [Redis]
                                                            │
                                                            │ SUBSCRIBE energy-events
                                                            ▼
                                                       [Subscriber]
                                                            │
                                                            │  1. compute_metrics
                                                            │  2. persist_metrics
                                                            │  3. ZADD demand:history
                                                            │  4. alert_engine.evaluate
                                                            │  5. publish_alert (por cada nueva)
                                                            ▼
[Redis: hashes + sorted sets + alerts stream + counters] ──► [API FastAPI]
                                                            │
                                                            │  HGETALL / ZRANGE / LRANGE / XREVRANGE
                                                            │  + SSE fan-out de energy-events
                                                            ▼
                                                       [Dashboard]
```

### Lado publisher (`publisher/main.py`)

Por cada ciclo:

1. `SourceSelector.fetch_events()` decide si consultar XM o simulador
   (ver §4).
2. `normalizer.build_events()` produce 6 `Event` (5 zonas + SIN).
3. Por cada evento se ejecuta `_publicar()` en una sola `pipeline
   transaction=True` (MULTI/EXEC):
   - `PUBLISH energy-events <json>`
   - `XADD energy:stream ... MAXLEN ~ 1000`
   - `HSET state:zone:{zid} <campos>` + `EXPIRE state:zone:{zid} 86 400`

Cada escritura está envuelta en `try/except` con contador `_event_failures`
para que un fallo de Redis en un evento no aborte el ciclo (patrón
R4-001, replicado del bounded correction del publisher).

### Lado subscriber (`subscriber/processor.py` y `subscriber/metrics.py`)

Loop principal:

1. `pubsub.subscribe("energy-events")`.
2. `while not self._stop.is_set()`:
   - Housekeeping cada 60 s: `ZREMRANGEBYSCORE metrics:demand:history
     -inf (now - 3600)`.
   - `pubsub.get_message(timeout=1.0)` (poll acotado para que el mismo
     task pueda chequear shutdown y housekeeping sin `create_task`).
   - En cada mensaje tipo `tick`:
     1. `compute_metrics(event, previous_demand_mw=...)` produce M1,
        M2, M3.
     2. `persist_metrics(redis, metrics)` escribe los 3 hashes
        `metrics:*`.
     3. `ZADD metrics:demand:history <unix_ts> <demanda_str>`.
     4. `alert_engine.evaluate(event, metrics)` devuelve alertas
        nuevas (transición a `active` o a `cleared`).
     5. `publish_alert(redis, alert)` por cada una.
   - En mensajes `source_switch`, se actualiza `alert_engine._last_fuente`.

Las alertas no se reintentan: si `publish_alert` falla, el caller
incrementa el contador y sigue (patrón R4-001).

### Lado API (`api/routers/`)

- `state.py` lee `state:sin` y los 5 `state:zone:*` en un pipeline.
- `metrics.py` lee los 3 `metrics:*` en pipeline (1 round-trip).
- `alerts.py` combina `LRANGE alerts:recent` (orden de IDs) con
  `XREVRANGE alerts:stream` (payloads) para devolver hasta 20
  alertas parseadas + counters activos.
- `health.py` lee los `health:*` vía `MGET`, hace ping inline y
  deriva `uptime_seconds` desde `health:subscriber:started_at`.
- `stream.py` mantiene una conexión SSE por cliente, con heartbeat de
  15 s y cleanup garantizado.

## 10. Métricas derivadas

Tres métricas calculadas por `subscriber/metrics.py:compute_metrics`,
implementadas y testeadas en `tests/unit/test_metrics.py`:

### M1 — Porcentaje de generación renovable

```
M1 = (solar + eólica + hidráulica) / generación_total × 100
```

- Unidad: `%`.
- Edge case: si `generacion_mw == 0` → M1 = 0.0 (sin división por cero).
- Persistida en hash `metrics:renewable`.

### M2 — Balance demanda-generación

```
M2 = demanda_mw - generación_mw
```

- Unidad: `MW`.
- Signo: positivo = **déficit** (es lo que dispara A1 a partir de 800 MW).
- Negativo = superávit.
- Persistida en hash `metrics:balance`.

### M3 — Variación porcentual de la demanda

```
M3 = (demanda_actual - demanda_previa) / demanda_previa × 100
```

- Unidad: `%`.
- Edge case: si no hay histórico previo (`previous_demand_mw is None`)
  o la previa es 0 → M3 = `None` (persiste como literal `"NaN"` en el
  hash; la API lo coacciona a `null` en JSON para que el dashboard
  muestre "—").
- Persistida en hash `metrics:demand:variation`.

### Orden canónico y duración

El router `/api/metrics` devuelve siempre 3 elementos en el orden
`renewable_pct`, `balance_mw`, `demand_variation_pct`. Esto está
alineado con `_METRIC_KEYS` en `subscriber/metrics.py`.

## 11. Reglas de alerta

Dos reglas implementadas en `subscriber/alerts.py:AlertEngine`,
testeadas en `tests/unit/test_alerts.py` (10 tests).

### A1 — `DEMAND_GENERATION_GAP` (HIGH)

- Condición: `demanda_mw - generación_mw > 800 MW` (configurable vía
  `DEMAND_GENERATION_GAP_THRESHOLD`).
- Severidad: `HIGH`.
- Alcance: **global del SIN** (no por zona). El `zone_id` en la alerta
  es el centinela `""` (string vacío) y se renderiza como "SISTEMA" en
  el dashboard.
- Trigger: cuando el SIN completo entra en déficit crítico.

### A2 — `LOW_RENEWABLE` (MEDIUM)

- Condición: `% renovable (M1) < 30 %` (configurable vía
  `RENEWABLE_THRESHOLD`).
- Severidad: `MEDIUM`.
- Alcance: **por zona** (`zone_id = event.entity_id`). Cada zona tiene
  su propio ciclo de vida independiente (ver SUB-002 en §15).
- Trigger: cuando una zona específica cae por debajo del umbral.

### Debounce y ciclo de vida

Para ambas reglas aplica la misma máquina de estados:

| Condición | Contador previo | Acción | Publica |
|-----------|-----------------|--------|---------|
| Verdadera | 0 → 1 | incrementa | NO (debounce) |
| Verdadera | 1 → 2 | incrementa | SÍ (`active`) |
| Verdadera | 2 → 3, 3 → 4, ... | incrementa | NO (idempotente) |
| Falsa (levantó) | n → 0 | reset | SÍ (`cleared`, con `consecutive_cycles = n`) |

Parámetros:

- `DEBOUNCE_CYCLES = 2` (constante en `subscriber/alerts.py`; también
  configurable vía `ALERT_DEBOUNCE_CYCLES`).
- Aislamiento por zona para A2 (cada `(rule, zone_id)` tiene su propio
  contador).
- A1 usa el centinela `""` como `zone_id`, que no es un `ZoneId`
  válido; el engine construye el `Alert` con `model_construct` para
  esquivar la validación `Literal`.

### Persistencia de cada alerta (`publish_alert`)

En este orden:

1. `PUBLISH energy-events <json>` (llega al dashboard por SSE).
2. `XADD alerts:stream MAXLEN ~ 100 <payload>` (auditoría).
3. `INCR alerts:total`.
4. `LPUSH alerts:recent <alert_id>` + `LTRIM 0 19`.
5. `INCR alerts:active:<code>` (si `state == "active"`),
   `DECR alerts:active:<code>` con clamp a 0 (si `state == "cleared"`).

La atomicidad es **at-most-once**: `alerts:total` y `alerts:stream`
pueden desincronizarse ante un fallo de Redis a mitad del helper. Está
documentado como `known_issue` en `subscriber/alerts.py:publish_alert`.

## 12. Diseño del dashboard

El dashboard es una SPA estática (Vite + vanilla JS, sin framework
reactivo). Vive en `dashboard/`, se construye con `pnpm --filter
dashboard build` y se sirve por Vite dev server (`pnpm dev`) o como
build estático en GH Pages.

### Componentes principales

| Componente | Archivo | Función |
|------------|---------|---------|
| Banner de modo | `src/components/banner.js` | Lee `/api/health` cada 5 s. Tres estados visuales: `REAL` (verde), `DEGRADADO` (amarillo, `real` + `failures > 0`), `SIMULADOR` (amarillo). Cuarto estado `SIN CONEXIÓN` si el backend no responde. |
| KPI cards (4) | `src/components/kpis.js` | Demanda SIN, Generación SIN, % Renovable, Balance (dem-gen). Demanda y generación vienen por SSE (`tick` del SIN); % renovable y balance vienen del polling a `/api/metrics`. |
| Chart en vivo | `src/components/charts.js` | Demanda vs generación, ventana corta (60 puntos ≈ 5 min), alimentado por SSE. |
| Chart histórico | `src/components/charts.js` | Solo demanda, ventana de 30 min, alimentado por SSE. |
| Mapa | `src/components/mapa.js` | Leaflet centrado en Colombia. Cinco `circleMarker` para ANT/VAL/ATL/BOG/SAN. Color verde si la zona cubre su demanda, rojo si tiene déficit, gris si no hay datos. Click → popup con desglose por recurso. |
| Panel de alertas | `src/components/alertas.js` | Hasta 20 alertas recientes (carga inicial vía `/api/alerts`, mantenimiento vía SSE). Alertas `cleared` se atenúan en vez de desaparecer (visibilidad operativa). Texto en español generado en cliente a partir de los campos estructurados (`rule`, `value`, `threshold`). |
| Botones stress | `src/components/stress.js` | Cuatro botones (`demand_surge`, `hydro_drop`, `critical_deficit`, `recovery`) que disparan `POST /api/stress/{event}`. El simulador aplica el escenario en el siguiente ciclo (TTL 30 s). |

### Datos en vivo

- **SSE** (`/api/stream`): `tick`, `alert`, `source_switch`. Es la
  fuente de actualización "lo que cambia constantemente".
- **Polling 5 s** a `/api/metrics` y `/api/health`. Es la fuente de
  "lo que SSE no emite" (métricas derivadas, banner).
- **Carga inicial** vía `GET /api/state` (mapa, KPIs) y `GET
  /api/alerts` (panel). Esto evita que el dashboard se vea vacío en
  modo REAL, donde el primer tick puede tardar 5 minutos.

## 13. Instalación

La guía completa de despliegue está en [`docs/DEPLOY.md`](docs/DEPLOY.md).
Esta sección es el quickstart.

### Prerrequisitos

- Python 3.11 o superior.
- Docker + docker compose (para Redis y opcionalmente el dashboard).
- Opcional: Node.js 18+ y pnpm (solo para hot-reload del dashboard).
  Si no los tenés, `make up-dev` levanta el dashboard dentro de Docker.

### Setup inicial

```bash
git clone https://github.com/sebastianlizarazo/redis-red-electrica-xm.git
cd redis-red-electrica-xm

# Levantar Redis solo
make up-redis

# ¿Falla con "address already in use"? Ya tenés un Redis en el 6379.
# No hace falta apagarlo, publicá el del contenedor en otro puerto:
REDIS_PORT=6380 make up-redis

# (Opcional) entorno virtual Python para procesos a mano
python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux/macOS
source .venv/bin/activate
pip install -e ".[dev]"
```

> Todos los comandos `make` se ejecutan desde la raíz del repo. Si ves
> `make: *** No rule to make target`, estás dentro de una subcarpeta.

> **Si usás `REDIS_PORT`, pasalo en todos los `make` de la sesión** (`up`,
> `up-redis`, `up-dev`). Y si corrés los procesos Python a mano, apuntalos
> al mismo puerto con `REDIS_URL=redis://localhost:6380/0`. Dentro de Docker
> los servicios se siguen hablando por el 6379, así que no hay que tocar
> nada más.

### Variables de entorno relevantes

Variables leídas desde `.env` o del entorno del proceso (definidas en
`common/config.py`):

| Variable | Default | Efecto |
|----------|---------|--------|
| `REDIS_URL` | `redis://localhost:6379/0` | URL completa de Redis |
| `XM_DEMANDA_URL` | endpoint XM público | URL del feed real |
| `XM_TIMEOUT_SECONDS` | 10 | Timeout HTTP estricto |
| `FORCE_SOURCE` | `""` (auto) | `""`, `real` o `simulator` |
| `PUBLISHER_INTERVAL_SECONDS` | 5 | Cadencia del simulador |
| `PUBLISHER_INTERVAL_SECONDS_REAL` | 300 | Cadencia del modo real (5 min) |
| `DEMAND_GENERATION_GAP_THRESHOLD` | 800.0 | Umbral A1 (MW) |
| `RENEWABLE_THRESHOLD` | 30.0 | Umbral A2 (%) |
| `ALERT_DEBOUNCE_CYCLES` | 2 | Ciclos antes de publicar `active` |
| `CORS_ORIGINS` | localhost:5173 + GH Pages | Origins CORS permitidos |
| `API_PORT` | 8000 | Puerto de la API |

Aparte existen dos variables que **no** lee `common/config.py`: las
interpreta `docker compose` al mapear puertos del host. Sirven para cuando
algo ya ocupa el puerto por defecto:

| Variable | Default | Efecto |
|----------|---------|--------|
| `REDIS_PORT` | 6379 | Puerto del host donde se publica el Redis del contenedor |
| `DASHBOARD_PORT` | 5173 | Puerto del host para el dashboard con `make up-dev` |

Solo cambian el mapeo hacia afuera: dentro de la red de Docker los servicios
se siguen comunicando por los puertos internos de siempre.

## 14. Ejecución del stack completo

### Plan A — todo en Docker (recomendado para la demo)

```bash
# Sin internet: simulador cada 3 s, banner siempre en modo SIMULADOR
FORCE_SOURCE=simulator PUBLISHER_INTERVAL_SECONDS=3 make up

# Con internet: XM real cada 5 min (más lento de ver)
make up

# Comprobar
curl localhost:8000/api/health
make ps
```

Si el puerto 6379 ya está ocupado en tu máquina:

```bash
REDIS_PORT=6380 make up
```

El dashboard tiene dos opciones:

```bash
# Opción 1: dentro de Docker (sin Node, sin pnpm)
make up-dev

# Opción 2: hot-reload con Vite en tu máquina
make dev-dashboard
```

### Plan B — procesos a mano (útil para depurar logs por componente)

Deja Redis en Docker y corre el resto en terminales separadas:

```bash
# Terminal 1
make up-redis

# Terminal 2
python -m publisher.main

# Terminal 3
python -m subscriber.main

# Terminal 4
python -m api.server

# Terminal 5
cd dashboard && pnpm dev
```

### URLs del sistema

- API: <http://localhost:8000>
- API docs interactivas (Swagger): <http://localhost:8000/docs>
- Dashboard: <http://localhost:5173>
- Redis: `localhost:6379` (sin auth por defecto)

### Inspección en vivo

```bash
make redis-cli
> KEYS '*'
> SUBSCRIBE energy-events
> XLEN energy:stream
> XLEN alerts:stream
> HGETALL state:zone:ANT
> HGETALL state:sin
> HGETALL metrics:renewable
> LRANGE alerts:recent 0 -1
```

### Tests y calidad

```bash
make test         # 133 tests
make test-unit    # solo tests unitarios (fakeredis, sin red)
make test-cov     # suite + reporte de cobertura en htmlcov/
make lint         # ruff check
make type-check   # mypy
make ci           # lint + types + tests (pipeline completo)
```

### Apagado

```bash
make down         # detiene contenedores (conserva el volumen redis-data)
```

`Ctrl-C` en cada terminal de los procesos a mano.

## 15. Dificultades encontradas

Esta sección documenta los problemas reales del proyecto, no una
lista genérica. Cada item incluye la causa raíz y la resolución.

### 15.1 SUB-002 — alertas `cleared` espurias de `LOW_RENEWABLE`

**Síntoma**: durante el smoke E2E de Day 2, `LOW_RENEWABLE` generaba
decenas de `cleared` por minuto. La regla A2 evaluaba por zona pero el
contador `_breaches` estaba indexado solo por `(rule,)`, no por
`(rule, zone_id)`. Resultado: cuando una zona salía del breach, otra
zona en el mismo ciclo "limpiaba" el contador que no era suyo y emitía
un `cleared` fantasma.

**Resolución** (PR #11, merge `5339aef`): el contador `_breaches` pasó
a ser `dict[tuple[str, str], int]` con la tupla `(rule, zone_id)` como
clave. A1 usa el centinela `""` (alcance global del SIN) y A2 usa
`event.entity_id` (alcance por zona), garantizando claves disjuntas
por construcción. Tests `test_007` y `test_008` en `test_alerts.py`
fijan la regresión.

**Aprendizaje**: bugs sutiles estilo "race" suelen venir de
inconsistencias semánticas (counter global + métrica per-zona con
lógicas distintas), no de concurrencia. Solo apareció en el smoke E2E;
los unit tests por separado no lo exponían.

### 15.2 Cadencia regulatoria de XM (5 minutos)

La API de XM publica cada cinco minutos por regulación, no por
decisión técnica. Si el publisher consulta cada 5 s en modo REAL, el
mismo valor se republica 59 veces antes de que llegue uno nuevo. Para
que la demo se vea viva sin internet, hay que correr en modo simulador
(`FORCE_SOURCE=simulator PUBLISHER_INTERVAL_SECONDS=3`). Sin ese
toggle, el dashboard parece congelado durante los primeros cinco
minutos y da impresión de estar roto.

### 15.3 Spec drift del `zone_id` para alertas globales

`Alert.zone_id` está tipado como `ZoneId | Literal[""]`. El centinela
`""` no es un `ZoneId` válido (`Literal["ANT","VAL","ATL","BOG","SAN","SIN"]`),
así que `model_validate` lo rechaza. El subscriber construye las
alertas A1 con `Alert.model_construct` para esquivar la validación.
Sobrevivió tres PRs sin flagging hasta que apareció una prueba con 13
alertas en Redis en la que `/api/alerts` devolvía solo 1: las 12 A1
estaban siendo descartadas en silencio por la validación de Pydantic
en `api/routers/alerts.py:Alert.model_validate_json(raw)`. PR #11
ensanchó el tipo a `ZoneId | Literal[""]` y resolvió.

### 15.4 Aislamiento de fallos per-evento (patrón R4-001)

Un fallo de Redis en una escritura dentro del ciclo del publisher
(PUBLISH, XADD o HSET) abortaba el batch completo y dejaba los eventos
restantes sin publicar. Lo mismo en el subscriber: un mensaje
malformado mataba el loop de consume. La solución, que terminó
replicándose en publisher, subscriber y API exception handler, fue:

```python
try:
    await self._publicar(evento)
except Exception as exc:
    self._event_failures += 1
    logger.exception("event publish failed", extra={...})
    continue
```

Codificar la resiliencia como patrón replicable vale la pena; copiar
el `except` específico no. Verificado en `tests/unit/test_publisher_main.py:test_008`.

### 15.5 At-most-once vs atomicidad en `publish_alert`

El helper `publish_alert` ejecuta cinco operaciones contra Redis en
secuencia (PUBLISH, XADD, INCR, LPUSH+LTRIM, INCR/DECR). No usa
MULTI/EXEC, así que un fallo a mitad del helper deja `alerts:total` y
`alerts:stream` desincronizados. Se documentó como `known_issue` en el
docstring; el contrato es explícitamente at-most-once. Para datos de
operación (alertas) esa garantía es aceptable; para auditoría dura
habría que mover el helper a un pipeline transaccional.

### 15.6 Tipos `bytes | str` de redis-py 8.x

Desde redis-py 8, los métodos como `hgetall` se tipan como
`dict[bytes | str, bytes | str]` para cubrir los dos modos del
cliente. La API usa `decode_responses=True`, así que en runtime todo
son `str`, pero mypy exige casts explícitos en cada lectura:
`cast(dict[str, str], await redis.hgetall(state_zone_key(...)))`. El
patrón se replica en todos los routers; no es bonito pero está
localizado.

### 15.7 Forzar la demo sin internet

Para que la demo sea reproducible sin depender del estado de XM, hay
dos interruptores complementarios:

- `FORCE_SOURCE=simulator` desactiva XM por completo.
- `XM_TIMEOUT_SECONDS=0.001` mantiene XM activo pero fuerza timeouts
  en cada request, lo que dispara el switch REAL → SIMULADOR tras 3
  fallos consecutivos (visible en el banner).

La diferencia es importante: con `FORCE_SOURCE=simulator` el banner
arranca en `SIMULADOR` y no muestra fallback; con `XM_TIMEOUT_SECONDS`
se ve la transición REAL → DEGRADADO → SIMULADOR en vivo.

### 15.8 `XM_TIMEOUT_SECONDS` vs `PUBLISHER_INTERVAL_SECONDS`

Aclaración común durante la prep de la demo:

- `PUBLISHER_INTERVAL_SECONDS` (default 5): cadencia del **loop** del
  publisher cuando está en modo simulador.
- `PUBLISHER_INTERVAL_SECONDS_REAL` (default 300): cadencia del loop
  en modo REAL (5 min).
- `XM_TIMEOUT_SECONDS` (default 10): timeout HTTP de la **request a
  XM**, no del loop. Si XM no responde en 10 s, ese ciclo falla.

Son ortogonales: el primero controla cada cuánto intenta el publisher;
el segundo, cuánto espera por respuesta.

## 16. Conclusiones

### Qué funcionó

- **Protocol `DataSource` rindió**: el switch entre XM real y
  simulador fue un cambio de cinco líneas en `common/config.py`, no
  un refactor. Tipar las dependencias externas como protocolos
  `runtime_checkable` paga el costo del typing en DX y testabilidad.
- **Sepación de roles por estructura Redis**: Pub/Sub para entrega en
  tiempo real (broadcast efímero), Stream para auditoría acotada
  (MAXLEN), Hash para estado actual (lo que el dashboard lee sin
  recorrer el stream), Sorted Set para histórico temporal con score =
  unix_ts. Cada estructura cumplió un rol distinto sin pisarse.
- **Patrón R4-001 replicado**: el `try/except` por evento con
  contador separado y `continue` apareció en publisher, subscriber y
  API exception handler. Codificar la resiliencia como patrón vale la
  pena.
- **Convenio SHAPE-first en TDD** (un `test_001_*_shape` antes de los
  tests de comportamiento) atrapó dos bugs de contrato temprano: la
  forma del evento `tick` en el publisher y el wire format de alertas.
- **Pipeline `transaction=True`**: garantiza atomicidad del
  PUBLISH+XADD+HSET en el publisher. Un fallo de Redis a mitad del
  MULTI hace rollback completo — no hay commits parciales.
- **Shortcuts `make`**: `make up`, `make test`, `make ci` dejaron a
  cada contribuidor productivo en sus primeros diez minutos.

### Qué costó más

- **Drift per-zone vs global en A2** (SUB-002): bug sutil que solo
  apareció en el smoke E2E, no en los unit tests. La deviation
  `Alert.zone_id="SIN"` (REQ-SUB-ALERTS-002) sobrevivió tres PRs sin
  flagging. Lección: un "spec adherence check" explícito en el
  checklist de review paga.
- **Negociación del wire format** publisher ↔ subscriber ↔ API: el
  caso `fuente real vs simulador` se renegoció al menos dos veces
  durante el desarrollo. Versionar cualquier cambio en el contrato
  con un campo `schema_version` en `Event` es el siguiente paso.
- **M3 sin histórico**: persistir el literal `"NaN"` en el hash y
  coaccionarlo a `None` en JSON requirió un field union `float | None`
  en `Metric` y un coerce explícito en el router. Tres lugares
  acoplados a una decisión que parecía trivial.

### Con más tiempo

- `EVAL` de Lua para atomicidad estricta en `publish_alert` y
  en `HSET + EXPIRE` (hoy se hace en dos comandos; un fallo entre
  ellos deja la zona sin TTL).
- Métricas per-zona persistidas, no un solo hash global por métrica
  (mejor observabilidad por región).
- Tests de integración contra un cluster Redis real para validar
  sharding y redirecciones `MOVED`.
- `schema_version` en `Event` para romper la dependencia dura del
  shape actual.

---


