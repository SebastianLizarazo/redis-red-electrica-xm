"""
Fixtures compartidos para pytest.

Recordatorio de política: este proyecto usa TDD STANDARD (laxo),
pero mantener fixtures limpios evita regresiones silenciosas y mejora la
DX del equipo. Los tests son recomendados, no bloqueantes.

Convención:
- Fixtures `*_client` devuelven clientes Redis (real o fake).
- Fixtures `xm_*` mockean la API XM.
- Fixtures `event_*` devuelven eventos de ejemplo.
- Fixtures `app` exponen la FastAPI app con `dependency_overrides`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio

from common.models import (
    Alert,
    AlertSeverity,
    DataSource,
    Event,
    EventData,
    Location,
    ZoneId,
)

# ----------------------------------------------------------------------
# Redis (fake + real)
# ----------------------------------------------------------------------


@pytest.fixture
def fakeredis_client():
    """
    Cliente Redis fake (en proceso, sin red) para tests rápidos.

    Devuelve la versión asíncrona (`fakeredis.aioredis.FakeRedis`) que
    respeta la API de `redis.asyncio` para que el código bajo test no
    tenga que distinguir entre real y fake.

    Scope: por defecto `function` (cada test su instancia limpia).
    """
    import fakeredis.aioredis as fakeredis_aio

    client = fakeredis_aio.FakeRedis(decode_responses=True)
    try:
        yield client
    finally:
        # `close()` está disponible en redis>=5.0; si no, `aclose()`.
        try:
            client.close()
        except AttributeError:  # pragma: no cover - compat vieja
            pass


@pytest_asyncio.fixture
async def fakeredis_async_client() -> AsyncIterator:
    """Variante async de `fakeredis_client`. Útil cuando el código bajo
    test usa `async with redis_client:` o `await redis_client.hset(...)`."""
    import fakeredis.aioredis as fakeredis_aio

    client = fakeredis_aio.FakeRedis(decode_responses=True)
    try:
        yield client
    finally:
        try:
            await client.aclose()
        except AttributeError:
            # Fallback para versiones donde el async close era `.close()`.
            try:
                client.close()
            except Exception:
                pass


def _redis_available() -> bool:
    """Devuelve True si hay un Redis real escuchando en localhost:6379."""
    import socket

    try:
        with socket.create_connection(("localhost", 6379), timeout=0.5):
            return True
    except OSError:
        return False


@pytest.fixture
def redis_client():
    """
    Cliente Redis real apuntando a localhost:6379.

    Si Redis no está disponible, el test se SKIP-ea con un mensaje claro
    en vez de fallar. Útil cuando un dev quiere correr integration tests
    pero olvidó arrancar `make up`.
    """
    if not _redis_available():
        pytest.skip("Redis no está disponible en localhost:6379 (¿corriste `make up`?)")

    import redis

    client = redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)
    # Verificación de liveness (no queremos acumular keys de tests).
    client.ping()
    try:
        yield client
    finally:
        # Limpiar keys de tests por seguridad; usa prefijo `test:` en keys
        # si el dev quiere sobrevivir un cleanup selectivo.
        try:
            keys = client.keys("test:*")
            if keys:
                client.delete(*keys)
        except Exception:
            pass
        client.close()


# ----------------------------------------------------------------------
# HTTP / XM mocks
# ----------------------------------------------------------------------


@pytest.fixture
def xm_mock():
    """
    Mock de la API XM usando respx.

    Uso típico:
        async with xm_mock:
            xm_mock.post(url).mock(return_value=httpx.Response(200, json={...}))
            await xm_client.fetch(...)

    El fixture monta y desmonta automáticamente con start/stop.
    """
    import respx

    with respx.mock(assert_all_called=False) as mock:
        yield mock


# ----------------------------------------------------------------------
# Eventos / alertas de ejemplo
# ----------------------------------------------------------------------


@pytest.fixture
def event_sample() -> Event:
    """Evento tick realista para usar en tests de processor / API."""
    return Event(
        entity_id="SIN",
        timestamp=datetime(2026, 9, 19, 12, 0, 0, tzinfo=UTC),
        location=Location(latitude=4.5, longitude=-74.1),
        data=EventData(
            demanda_mw=10_500.0,
            generacion_mw=10_800.0,
            generacion_solar_mw=200.0,
            generacion_eolica_mw=50.0,
            generacion_hidraulica_mw=6_500.0,
            generacion_termica_mw=4_050.0,
            fuente=DataSource.SIM,
        ),
    )


@pytest.fixture
def zone_event_sample() -> Event:
    """Evento de zona individual (Antioquia) con valores plausibles."""
    return Event(
        entity_id="ANT",
        timestamp=datetime(2026, 9, 19, 12, 0, 0, tzinfo=UTC),
        location=Location(latitude=6.25, longitude=-75.56),
        data=EventData(
            demanda_mw=2_100.0,
            generacion_mw=1_900.0,
            generacion_solar_mw=80.0,
            generacion_eolica_mw=20.0,
            generacion_hidraulica_mw=1_100.0,
            generacion_termica_mw=700.0,
            fuente=DataSource.SIM,
        ),
    )


@pytest.fixture
def alert_sample() -> Alert:
    """Alerta operativa de ejemplo (Alerta 1 alta)."""
    return Alert(
        id="alert-test-001",
        rule="DEMAND_GENERATION_GAP",
        severity=AlertSeverity.HIGH,
        zone_id="ANT",
        value=950.0,
        threshold=800.0,
        timestamp=datetime(2026, 9, 19, 12, 0, 0, tzinfo=UTC),
        message="Déficit demanda-generación 950 MW supera umbral 800 MW",
    )


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


@pytest.fixture
def utc_now() -> datetime:
    """`datetime.now(timezone.utc)` con tipo explícito (ayuda al linter)."""
    return datetime.now(UTC)


@pytest.fixture
def zones_default() -> list[ZoneId]:
    """Las 5 zonas colombianas en el orden canónico (design OPEN-1)."""
    return ["ANT", "VAL", "ATL", "BOG", "SAN"]


# ----------------------------------------------------------------------
# FastAPI app fixture (api-core-2026-09 PR-A)
# ----------------------------------------------------------------------


@pytest_asyncio.fixture
async def app_client(fakeredis_async_client):  # noqa: ANN001 - pytest fixture composition
    """
    App FastAPI con `dependency_overrides` para tests de routers API.

    - Crea la app via `api.server.create_app()` (mismo path que producción).
    - Sustituye `get_redis` para devolver el `fakeredis_async_client` del fixture
      anterior — un swap de 1 línea, cero cambios en los routers.
    - Devuelve un `httpx.AsyncClient(transport=ASGITransport(app))` listo
      para `await client.get("/api/health")` dentro de tests async.
    - El lifespan se activa con `async with AsyncClient(...)` automáticamente
      (httpx dispara el startup/shutdown de la ASGI app).

    Tests que necesitan sembrar Redis antes del request deben usar
    `fakeredis_async_client` ANTES de hacer `await client.get(...)`
    (las llamadas en serie sobre el mismo fixture funcionan).
    """
    from httpx import ASGITransport, AsyncClient

    from api.dependencies import get_redis
    from api.server import create_app

    app = create_app()
    app.dependency_overrides[get_redis] = lambda: fakeredis_async_client

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client

    # Limpiar overrides para que un próximo test que reuse la app parta limpio.
    app.dependency_overrides.clear()


# ----------------------------------------------------------------------
# Hooks de pytest
# ----------------------------------------------------------------------


def pytest_collection_modifyitems(config, items):  # noqa: ARG001 - pytest hooks
    """Marca automáticamente los tests de tests/unit como `unit` y los de
    `tests/integration` como `integration` para poder filtrar con `-m`."""
    for item in items:
        path = str(item.fspath)
        if "tests/unit" in path:
            item.add_marker(pytest.mark.unit)
        elif "tests/integration" in path:
            item.add_marker(pytest.mark.integration)
