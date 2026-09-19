# README Técnico — `redis-red-electrica-xm`

> Documento vivo. Esta es la versión de Fase 0 (scaffolding). Las fases
> siguientes (publisher, processor, alerts, API, dashboard) lo van
> extendiendo sin romper enlaces cruzados.

## 1. ¿Qué es?

Monitor en tiempo real del Sistema Interconectado Nacional (SIN) colombiano
que usa [Redis](https://redis.io) como bus de datos principal. El sistema
consume la API pública de [XM](https://www.xm.com.co/) (operador del SIN),
la procesa con un pipeline Pub/Sub + Streams + Hashes + Sorted Sets, y
expone los resultados vía SSE a un dashboard en el navegador.

Esta es la base tecnológica (scaffolding) sobre la que el equipo
construye el sistema durante 8 fases de trabajo. **Nada de este commit
es todavía código de aplicación**: son configs, contratos de datos y
herramientas de soporte.

## 2. Stack confirmado

| Capa | Tecnología | Decisión justificada |
|------|-----------|----------------------|
| Backend | Python 3.11+ · FastAPI · uvicorn | Async nativo, ecosistema maduro |
| Redis client | `redis` 5.x (asyncio) | API asíncrona coherente con FastAPI |
| Validación | `pydantic` 2.x + `pydantic-settings` | Tipos estrictos en runtime |
| HTTP async | `httpx` | Reutilizable en tests y publisher |
| SSE | `sse-starlette` | SSE nativo para FastAPI |
| Mock XM | `respx` | Intercepta `httpx` sin tocar código |
| Redis fake | `fakeredis` 2.x | Tests sin Docker |
| XM enrichment | `pydataxm` (MIT, oficial XM) | Variables que `DemandaTiempoReal` no expone |
| Tests | `pytest` + `pytest-asyncio` + `pytest-cov` | Strict TDD **OFF** (memoria `sdd-init`) |
| Frontend | Vite 5 + Chart.js 4 + Leaflet 1.9 · vanilla JS | Tamaño mínimo, sin framework reactivo |
| Infra | Docker + compose · Redis 7 (`redis:7-alpine`) | Reproducible, ligero |

## 3. Estructura del repositorio (Fase 0)

```
.
├── pyproject.toml           — deps runtime/dev, pytest/ruff/mypy config
├── package.json             — workspace pnpm raíz
├── pnpm-workspace.yaml      — declara `dashboard/` como workspace
├── pnpm-lock.yaml           — versiones exactas (committeado)
├── Makefile                 — `make up/down/test/lint/demo/...`
├── .env.example             — variables de entorno esperadas (no secretos)
├── infra/
│   └── docker-compose.yml   — solo Redis en Fase 0; backend se agrega Fase 5
├── dashboard/
│   ├── package.json
│   ├── vite.config.js
│   ├── index.html
│   └── src/main.js          — placeholder; componentes en Fase 6
├── common/                  — dominio compartido (Sebastián)
│   ├── models.py            — Pydantic v2 (Event, Alert, ZoneState, ...)
│   ├── redis_keys.py        — constantes + factories de keys
│   ├── data_source.py       — Protocol `DataSource` + excepciones
│   ├── config.py            — `Settings` (BaseSettings) + singleton
│   └── logging_config.py    — JSON/humano, configurable vía env
├── publisher/               — fuente de datos (Edwar) — Fase 1+
├── subscriber/              — processor + métricas + alertas (Alejandro) — Fase 2+
├── api/                     — FastAPI + SSE (Sebastián) — Fase 3+
├── tests/
│   ├── conftest.py          — fixtures: fakeredis, respx, eventos
│   └── unit/
│       └── test_smoke_scaffolding.py — verificaciones Fase 0
└── docs/                    — este archivo + DEPLOY + TESTING — Fase 0+
```

## 4. División de roles (acordada en commits previos)

| Integrante | Componente | Fases |
|------------|-----------|-------|
| **Edwar** | `publisher/` (XM, simulador, selector de fuente) | 1, 5 |
| **Alejandro** | `subscriber/` (métricas, alertas, processor) | 2 |
| **David** | `dashboard/` (KPIs, charts, mapa) | 4, 6 |
| **Jonathan** | `infra/` (Dockerfiles, compose) + `tests/` global | 0, 5, 7 |
| **Sebastián** | `common/`, `api/`, integración general, docs | 0, 3, 7 |

## 5. Cómo arrancar (Fase 0)

### Requisitos

- Python 3.11 o superior
- Node.js 18 o superior
- pnpm 9 o superior (recomendado: 11+)
- Docker + docker-compose (para `make up`)

### Setup inicial

```bash
# 1) Python: crear venv e instalar deps (recomendado)
python -m venv .venv
. .venv/Scripts/Activate.ps1     # Windows
# source .venv/bin/activate       # Linux/macOS

pip install -e ".[dev]"

# 2) Frontend: instalar deps de pnpm
pnpm install

# 3) Levantar Redis (sin él, ningún módulo del backend arrancará)
make up
```

### Verificación rápida

```bash
# Estado de Redis
make redis-cli
> KEYS '*'
# (vacío — acabamos de empezar)

# Suite de tests Fase 0 (no requiere Redis real — usa fakeredis)
pytest tests/unit -v

# Linter + type checker
make lint
make type-check
```

### Demo

```bash
make demo
# Sigue las instrucciones en pantalla (de momento, solo Redis y dashboard).
```

## 6. Contratos y formas de Fase 0

### 6.1 Modelos Pydantic (`common/models.py`)

Todos `frozen=True`. Las fechas son siempre **aware UTC**.

```python
from common.models import Event, EventData, DataSource, Location, ZoneId
```

Campos clave:

- `ZoneId = Literal["ANT", "VAL", "ATL", "BOG", "SAN", "SIN"]` —
  cinco zonas geográficas + el SIN global (los `entity_id` de los
  `tick` consolidados).
- `EventData`: demanda + generación por recurso (solar, eólica, hidro,
  térmica) en MW, **todos `≥ 0`**, capped a 50 000 MW para descartar
  basura de la API.
- `Event`: contrato Pub/Sub (`energy-events`), inmutable. Discriminador
  `type` lo agrega el publisher; hoy solo emitimos `tick` desde el
  Modelo, otros tipos (`alert`, `source_switch`) los agregan specs
  futuros.
- `Alert`, `HealthStatus`, `Metric`: consultados vía `/api/state`,
  `/api/alerts`, `/api/health`. También inmutables.

### 6.2 Keys Redis (`common/redis_keys.py`)

Centralizadas para evitar divergencia publisher/subscriber/api. Los
factories Blindan contra typos:

```python
from common.redis_keys import (
    PUBSUB_CHANNEL_ENERGY,    # "energy-events"
    STREAM_ENERGY,            # "energy:stream"
    STREAM_ENERGY_MAXLEN,     # 1000
    state_zone_key,           # "state:zone:ANT"
    alerts_active_key,        # "alerts:active:DEMAND_GENERATION_GAP"
    stress_key,               # "stress:demand_surge"
    alert_severity_in_channel,
)
```

TTLs y ventanas principales:

| Concepto | Valor | Razón |
|----------|-------|-------|
| `STATE_ZONE_TTL_SECONDS` | 86 400 (24h) | Zona inactiva = flag visual, no borrado |
| `DEMAND_HISTORY_WINDOW_SECONDS` | 3 600 (1h) | Cobertura para métrica 3 |
| Backoff base / cap | 300 / 1 800 s (5 / 30 min) | Reintentos de XM tras fallos |

### 6.3 Protocol `DataSource` (`common/data_source.py`)

Contrato que cumplen `XMRealSource` (Fase 1) y `SimulatorSource` (Fase 1).
`@runtime_checkable` para `isinstance(fake, DataSource)` en tests sin
herencia.

```python
from common.data_source import DataSource, DataSourceError, DataSourceTimeoutError
```

Excepciones específicas evitan que el `source_selector` tenga que
adivinar el tipo de error.

### 6.4 Config (`common/config.py`)

`Settings(BaseSettings)` carga del `.env` o de variables de entorno del
sistema. Singleton `settings` al final del módulo — los consumidores
hacen `from common.config import settings`.

Validadores normalizan:

- `log_level` → mayúsculas canónicas (`INFO`, `DEBUG`, ...)
- `force_source` → `"" | "real" | "simulator"`
- `default_zones` → acepta CSV desde `.env` (ej. `ANT,BOG,SAN`)

### 6.5 Logging (`common/logging_config.py`)

Idempotente, formato configurable:

- `LOG_JSON=0` (default): texto legible con colores ANSI si stdout es TTY.
- `LOG_JSON=1`: una línea JSON por log, ingestable por jq/Loki/Datadog.
- Logger names canónicos: `publisher`, `subscriber`, `api`, `dashboard`.

## 7. Mapeo a la rúbrica del taller (sección 27)

| Criterio | Componente | Estado en Fase 0 |
|----------|-----------|-----------------|
| Arquitectura limpia (15%) | Estructura de carpetas + Protocol `DataSource` | **Scaffolding listo** — implementación en Fases 1-4 |
| Uso de Redis (20%) | Constantes y factories centralizados | **Estructura lista** — uso real en Fases 1-2 |
| Publisher (10%) | Pendiente | **Fase 1** |
| Procesamiento + métricas (15%) | Pendiente | **Fase 2** |
| Comunicación real-time (10%) | Constantes Pub/Sub + Stream | **Constantes listas** — uso en Fases 1, 3 |
| Dashboard (15%) | Vite config + Chart.js + Leaflet instalados | **Tooling listo** — UI en Fases 4, 6 |
| Alertas (5%) | Modelos + keys | **Modelos listos** — reglas en Fase 2 |
| Documentación (5%) | Este documento + estructura de `docs/` | **Inicial lista** |
| Presentación (5%) | Pendiente | **Fase 8** |

## 8. Difficultades encontradas (Fase 0)

> Esta sección se actualiza al cerrar cada fase. Cada item es
> retrospectivo y técnico, evitando quejas genéricas.

- **Pydantic-settings + listas desde `.env`**: el validador
  `@field_validator(mode="before")` no se aplica a tiempo durante el
  `DotEnvSettingsSource` si el target es `List[Literal[...]]`. Lo
  resolví tipando el campo como `Union[str, List[ZoneId]]` y añadiendo
  un `@model_validator(mode="before")` redundante que pre-parsea
  CSV. Funciona, pero deja una fragilidad: si alguien quita el
  `Union`, el parseo se rompe en silencio.

- **`pydantic-settings` no estaba en el listado de runtime deps** que
  me dieron para Fase 0. Lo añadí porque `BaseSettings` salió de
  Pydantic en 2.x y la alternativa `BaseModel + manual env_load`
  introduce boilerplate duplicado. Si el equipo prefiere no
  agregar la dep, se puede reemplazar por un cargador custom con
  `dotenv.load_dotenv()` y un dict.

- **ZoneId vs entidad global SIN**: la API XM devuelve agregados
  globales (`Demanda en tiempo real` para todo el SIN) y datos por
  zona simultáneamente. Para no introducir dos tipos diferentes,
  extendí `ZoneId = "ANT" | "VAL" | "ATL" | "BOG" | "SAN" | "SIN"`.
  El factory `state_zone_key()` asume uno de los 5 nombres
  geográficos — si se llama con `"SIN"` el formato produce
  `state:zone:SIN` que NO es lo correcto (la key global es
  `state:sin`). El caller decide; documentado.

## 9. Próximos pasos (Fase 1+)

- **Fase 1 (publisher)**: implementar `XMRealSource`, `SimulatorSource`,
  `normalizer`, `source_selector` en `publisher/`. Publicar en
  `energy-events` (Pub/Sub) + `energy:stream` (Stream) +
  `state:zone:*` (Hashes).
- **Fase 2 (subscriber)**: processar eventos, calcular M1/M2/M3,
  evaluar A1/A2 con debounce, housekeeping 60s.
- **Fase 3 (api)**: FastAPI + SSE con `/api/health`, `/api/state`,
  `/api/stream`, `/api/stress/{event}`.
- **Fase 4 (dashboard inicial)**: SSE consumer + primer KPI card
  funcionando end-to-end con simulador.
- **Fase 5 (infra completa)**: Dockerfiles publisher/subscriber/api +
  healthchecks extendidos.
- **Fase 6 (dashboard completo)**: 4 KPIs, 2 charts, mapa, panel
  alertas, banner modo, 4 botones stress test.
- **Fase 7 (docs)**: DEPLOY.md, TESTING.md, INTEGRANTES.md; video de
  respaldo.
- **Fase 8 (entrega)**: build de Vite, GH Pages (frontend), demo local
  (backend) con plan B de `cloudflared`.

---

**Owner**: Sebastián · **Status**: Fase 0 cerrada · **Próxima fase**: 1
