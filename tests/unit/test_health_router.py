"""Tests for `api.routers.health` (REQ-API-005 + REQ-API-008 de spec #510).

Convención STANDARD (obs #458): TDD laxo, tests con código OK.

Cobertura:
- test_001 HEALTHY: health:* + subscriber:started_at poblados → redis_ok=True, uptime>0.
- test_002 EDGE: subscriber nunca arrancó (started_at ausente) → uptime_seconds=0.
- test_003 REDIS DOWN: ping falla → 503 + Retry-After: 5 (handler global).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from common.redis_keys import (
    KEY_HEALTH_FAILURES,
    KEY_HEALTH_MODE,
    KEY_HEALTH_NEXT_RETRY_AT,
    KEY_HEALTH_SOURCE_SWITCHES,
    KEY_HEALTH_SUBSCRIBER_STARTED_AT,
    KEY_HEALTH_XM_LAST_FAILURE,
    KEY_HEALTH_XM_LAST_SUCCESS,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed_healthy(fakeredis_async_client) -> None:
    """Sembrar las claves health:* tal como las escribiría el publisher
    tras un ciclo exitoso + el subscriber tras su boot."""
    now = datetime.now(UTC)
    await fakeredis_async_client.mset({
        KEY_HEALTH_MODE: "real",
        KEY_HEALTH_FAILURES: "0",
        KEY_HEALTH_SOURCE_SWITCHES: "0",
        KEY_HEALTH_XM_LAST_SUCCESS: now.isoformat(),
        KEY_HEALTH_NEXT_RETRY_AT: (now + timedelta(minutes=5)).isoformat(),
    })
    # El subscriber arrancó hace 10 segundos.
    await fakeredis_async_client.set(
        KEY_HEALTH_SUBSCRIBER_STARTED_AT,
        (now - timedelta(seconds=10)).isoformat(),
    )


# ---------------------------------------------------------------------------
# test_001 — HAPPY: uptime>0, redis_ok=True, mandatory fields populated
# ---------------------------------------------------------------------------


async def test_001_health_happy_path_exposes_uptime_and_ping(
    app_client, fakeredis_async_client
):
    """HEALTHY: todas las claves presentes → 200 con mode=real, redis_ok=True,
    uptime_seconds > 0, failures/source_switches/last_xm_success poblados."""
    await _seed_healthy(fakeredis_async_client)

    resp = await app_client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()

    assert body["mode"] == "real"
    assert body["redis_ok"] is True
    assert body["uptime_seconds"] >= 10  # sembramos hace 10s, ahora es >= 10s
    assert body["failures"] == 0
    assert body["source_switches"] == 0
    assert body["last_xm_success"] is not None
    assert body["next_retry_at"] is not None
    # `last_failure` no se sembró → None.
    assert body["last_failure"] is None


# ---------------------------------------------------------------------------
# test_002 — EDGE: subscriber nunca arrancó → uptime_seconds == 0
# ---------------------------------------------------------------------------


async def test_002_health_absent_subscriber_started_at_returns_uptime_zero(
    app_client, fakeredis_async_client
):
    """Sin `health:subscriber:started_at` → `uptime_seconds: 0`, `redis_ok: True`."""
    # Sembramos SOLO las claves publisher-side; subscriber NO ha arrancado.
    await fakeredis_async_client.mset({
        KEY_HEALTH_MODE: "simulator",
        KEY_HEALTH_FAILURES: "3",
        KEY_HEALTH_SOURCE_SWITCHES: "1",
    })

    resp = await app_client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()

    assert body["uptime_seconds"] == 0
    assert body["redis_ok"] is True
    assert body["mode"] == "simulator"
    assert body["failures"] == 3
    assert body["source_switches"] == 1


# ---------------------------------------------------------------------------
# test_003 — REDIS DOWN: 503 + Retry-After: 5 (REQ-API-008)
# ---------------------------------------------------------------------------


async def test_003_health_redis_down_returns_503_with_retry_after(
    app_client, fakeredis_async_client
):
    """Si el `await redis.ping()` levanta RedisError, el handler global
    responde 503 + `Retry-After: 5`. No devolvemos data stale."""
    # Monkey-patch del cliente fakeredis para que el `ping()` levante
    # RedisError, simulando Redis caído en el momento del request.
    from redis.exceptions import ConnectionError as RedisConnectionError

    async def _broken_ping(*args, **kwargs):
        raise RedisConnectionError("simulated redis down")

    fakeredis_async_client.ping = _broken_ping  # type: ignore[method-assign]

    resp = await app_client.get("/api/health")
    assert resp.status_code == 503
    assert resp.headers.get("Retry-After") == "5"
    body = resp.json()
    assert body["detail"] == "redis unavailable"
    assert "simulated redis down" in body["error"]


# ---------------------------------------------------------------------------
# test_004 — EDGE: recuperación inmediata sin cache stale (REQ-API-008)
# ---------------------------------------------------------------------------


async def test_004_health_redis_recovers_to_200_with_fresh_data(
    app_client, fakeredis_async_client
):
    """Después de un 503 por `ping` fallido, la siguiente request debe
    ver `ping` sano y devolver 200 con `redis_ok=True`. Esto prueba que
    cada request ejecuta su propio `ping()` — no hay respuesta cacheada.

    Patrón: guardar el `ping` original, monkey-patch al broken, hacer
    el primer GET (esperamos 503), restaurar el original, hacer el
    segundo GET (esperamos 200). Mismo patrón que test_003, extendido
    con un recovery round-trip."""
    from redis.exceptions import ConnectionError as RedisConnectionError

    async def _broken_ping(*args, **kwargs):
        raise RedisConnectionError("simulated redis down")

    # Guardamos el ping real del fakeredis para restaurarlo tras el failure.
    original_ping = fakeredis_async_client.ping

    try:
        # --- Round 1: Redis caído → 503 ---
        fakeredis_async_client.ping = _broken_ping  # type: ignore[method-assign]
        resp_down = await app_client.get("/api/health")
        assert resp_down.status_code == 503
        assert resp_down.headers.get("Retry-After") == "5"
    finally:
        # Restauramos siempre, incluso si la primera aserción explota,
        # para no contaminar el siguiente test del mismo fixture.
        fakeredis_async_client.ping = original_ping  # type: ignore[method-assign]

    # --- Round 2: Redis recuperado → 200 con data fresca ---
    resp_up = await app_client.get("/api/health")
    assert resp_up.status_code == 200
    body = resp_up.json()
    assert body["redis_ok"] is True
    # La respuesta es fresca (no stale): cada request hits Redis.
    assert body["mode"] in {"real", "simulator"}
    assert body["uptime_seconds"] >= 0
