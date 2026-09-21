# TESTING — Cómo correr y extender la suite

> La suite de tests usa `pytest` con `pytest-asyncio` (modo `auto`) y
> `fakeredis` para evitar dependencias de red. Total al cierre del
> change `docs-day4-2026-09`: **131 tests en 16 archivos** (127
> unitarios + 4 de integración).

## 1. Cómo correr la suite

Todos los comandos viven en el `Makefile` raíz y son wrappers de
`pytest`:

| Comando | Qué hace | Aprox. tests |
|---------|----------|--------------|
| `make test` | Suite completa (unit + integration). | 131 |
| `make test-unit` | Solo `tests/unit/` — no requiere Redis real (usa `fakeredis`). | 127 |
| `make test-int` | Solo `tests/integration/` — requiere Redis en `localhost:6379` (o se skip-ea con mensaje claro). | 4 |
| `make test-cov` | Suite + reporte de cobertura en `htmlcov/` (target: 80%+ en `publisher/`, `subscriber/`, `api/`). | 131 |
| `make ci` | Pipeline completo: `lint` (`ruff check .`) + `type-check` (`mypy .`) + `test`. | — |

Para correr un archivo o test puntual sin el wrapper:

```bash
python -m pytest tests/unit/test_normalizer.py -v
python -m pytest tests/unit/test_alerts.py::TestAlerts::test_001_alerts_shape_returns_list_of_alerts
```

Para filtrar por marker:

```bash
python -m pytest -m unit
python -m pytest -m integration
```

## 2. Qué testea cada archivo

Conteos verificados con `python -m pytest tests/ --collect-only -q`
(131 tests totales).

| Path | Propósito | # tests |
|------|-----------|---------|
| `tests/integration/test_processor.py` | Round-trip del `processor` contra `fakeredis` (alert → stream + counters, housekeeping, shutdown limpio). | 4 |
| `tests/unit/test_alerts.py` | Motor de alertas A1/A2: debounce, auto-clear, idempotencia, aislamiento por zona, helpers de publish. | 10 |
| `tests/unit/test_alerts_router.py` | Router `/api/alerts`: respuesta con recientes + activos, vacío retorna contenedores vacíos. | 2 |
| `tests/unit/test_health_router.py` | Router `/api/health`: uptime, ping, comportamiento con Redis caído (503) y recuperado (200). | 4 |
| `tests/unit/test_log_redaction.py` | `safe_redis_url`: redacción de credenciales en logs (password, user+password, IPv6 con/sin puerto, TLS). | 6 |
| `tests/unit/test_metrics.py` | Métricas M1 (renovable %), M2 (balance), M3 (variación demanda) + persistencia de hashes + edge cases (generación 0). | 13 |
| `tests/unit/test_metrics_router.py` | Router `/api/metrics`: 3 valores numéricos en orden, `NaN` → `null` en JSON. | 2 |
| `tests/unit/test_normalizer.py` | `extract_latest_demand` (parseo robusto del payload XM) + `build_events` (reparto por zonas, despacho, edge cases temporales). | 27 |
| `tests/unit/test_publish_alert.py` | Helper `publish_alert`: Pub/Sub + Stream + `INCR` + `LPUSH`, y `cleared` decrementa contador activo. | 2 |
| `tests/unit/test_publisher_main.py` | Entry point publisher: redacción URL, aplanar a strings, fallback sin evento → `state:sin`, isolation per-event, pipeline atómico, source-switch raise isolation. | 12 |
| `tests/unit/test_smoke_scaffolding.py` | Sanity checks de Fase 0: imports, modelos Pydantic, factories de keys Redis, Protocol `DataSource`, `Settings` singleton. | 9 |
| `tests/unit/test_source_selector.py` | `SourceSelector`: fallback XM → simulador tras 3 fallos, backoff creciente, `force_source`, persistencia del modo en Redis. | 20 |
| `tests/unit/test_state_router.py` | Router `/api/state`: 5 zonas + SIN, parcial sin zonas (skip, no 500), preflight CORS desde origin permitida. | 4 |
| `tests/unit/test_stream_router.py` | Router `/api/stream`: publica `tick` recibido por cliente en <1s; desconexión a mitad de vuelo cierra Pub/Sub con `aclose`. | 2 |
| `tests/unit/test_stress_router.py` | Router `/api/stress/{event}`: evento válido retorna 204 y setea flag con TTL; evento inválido retorna 400 con detalle en español. | 2 |
| `tests/unit/test_xm_client.py` | `XMClient`: `fetch_events` (6 eventos marcados `real`), política de reintentos (timeout/500/404/red), payloads malformados, health check. | 12 |

## 3. Cómo agregar nuevos tests

### Convención de archivos y nombres

- Unitarios: `tests/unit/test_<modulo>.py` — no requieren Redis real
  (usan `fakeredis_async_client` o `respx`).
- Integración: `tests/integration/test_<modulo>.py` — pueden requerir
  Redis en `localhost:6379` (el fixture `redis_client` skip-ea si no
  está disponible).
- Clases: prefijo `Test*` (configurado en `pyproject.toml`).
- Funciones: prefijo `test_*` (idem).

### Markers

Los markers están declarados en `pyproject.toml`:

- `@pytest.mark.unit` — default para tests en `tests/unit/` (el hook
  `pytest_collection_modifyitems` de `tests/conftest.py` lo aplica
  automáticamente).
- `@pytest.mark.integration` — default para tests en
  `tests/integration/`.

### Convención SHAPE-first (subscriber convention)

Cuando el módulo es nuevo y aún no tiene tests, el primer test valida
la **forma** del output (estructura de keys, tipos, presencia de
campos), no el comportamiento:

```python
def test_001_<modulo>_shape_returns_<forma_esperada>():
    """SHAPE: el output tiene la estructura esperada (clases/tipos/campos)."""
    ...
```

Los tests `test_002+` validan el **comportamiento** (reglas, edge
cases, errores). Convención adoptada en `subscriber/` y replicada en
`api/`.

### Fixtures disponibles en `tests/conftest.py`

Diez fixtures listos para usar (verificados contra `tests/conftest.py`):

| Fixture | Tipo | Para qué sirve |
|---------|------|----------------|
| `fakeredis_client` | sync | Cliente Redis fake en proceso, API sync. |
| `fakeredis_async_client` | async | Variante async para `await redis.hset(...)` y contextos `async with`. |
| `redis_client` | sync (skip si no hay Redis) | Cliente real apuntando a `localhost:6379`. Skip-ea con mensaje claro si Redis no está levantado. |
| `xm_mock` | context manager (`respx`) | Mock de la API XM para tests que llaman a `httpx`. |
| `event_sample` | sync | `Event` realista (`entity_id="SIN"`, demanda 10 500 MW, fuente SIM). |
| `zone_event_sample` | sync | `Event` de zona individual (Antioquia). |
| `alert_sample` | sync | `Alert` operativa (Alerta 1 alta, `zone_id="ANT"`). |
| `utc_now` | sync | `datetime.now(timezone.utc)` con tipo explícito. |
| `zones_default` | sync | Lista canónica `["ANT", "VAL", "ATL", "BOG", "SAN"]`. |
| `app_client` | async | App FastAPI con `dependency_overrides[get_redis]` apuntando a `fakeredis_async_client` — lista para `await client.get("/api/health")`. |

### Coverage target

`make test-cov` genera reporte en `htmlcov/index.html`. Target del
equipo: **80%+ en código de aplicación** (`publisher/`, `subscriber/`,
`api/`). Los `__init__.py` y los routers cuentan; los fixtures y los
tests no.

Para ver solo el porcentaje por archivo:

```bash
make test-cov | grep -E '^TOTAL|^(publisher|subscriber|api)/'
```

### Antes de abrir un PR

1. `make ci` debe pasar limpio (lint + types + 131 tests).
2. Si agregaste fixtures nuevos, documentarlos arriba en este archivo.
3. Si agregaste un marker nuevo, declararlo en `pyproject.toml` (la
   opción `--strict-markers` aborta la colección si el marker no está
   en la lista).