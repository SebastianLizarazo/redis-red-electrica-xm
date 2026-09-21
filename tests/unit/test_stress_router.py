"""Tests for `api.routers.stress` (REQ-API-006 de spec #510).

Convención STANDARD (obs #458): TDD laxo, tests con código OK.

Cobertura:
- test_001 HAPPY: `POST /api/stress/demand_surge` → 204 + Redis tiene
  `stress:demand_surge == "1"` con TTL > 0 (≤ KEY_STRESS_TTL_SECONDS).
- test_002 EDGE: `POST /api/stress/unknown` → 400 con detail conteniendo
  `"escenario desconocido"` y listando los válidos (mensaje espejado del
  publisher/simulator.py:inject_stress).

Estrategia: usamos `app_client` (fixture de `tests/conftest.py`) que
crea una FastAPI app con `dependency_overrides[get_redis] = fakeredis`.
fakeredis respeta `SET ... EX <ttl>` y `TTL` igual que Redis real, así
que podemos assert sobre TTL>0 directamente.
"""
from __future__ import annotations

import pytest

from common.redis_keys import KEY_STRESS_TTL_SECONDS, stress_key
from publisher.simulator import STRESS_EVENTS

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# test_001 — HAPPY: evento válido → 204 + flag seteado con TTL>0
# ---------------------------------------------------------------------------


async def test_001_stress_valid_event_returns_204_and_sets_flag_with_ttl(
    app_client, fakeredis_async_client
):
    """`POST /api/stress/demand_surge` → 204 No Content + Redis tiene
    `stress:demand_surge == "1"` con TTL > 0 (y ≤ KEY_STRESS_TTL_SECONDS)."""
    event = "demand_surge"
    flag_key = stress_key(event)

    # Sanity: Redis empieza vacío.
    assert await fakeredis_async_client.exists(flag_key) == 0

    resp = await app_client.post(f"/api/stress/{event}")
    assert resp.status_code == 204
    # 204 No Content: el body debe estar vacío.
    assert resp.content == b""

    # Flag seteado con valor "1".
    assert await fakeredis_async_client.get(flag_key) == "1"

    # TTL > 0 y ≤ KEY_STRESS_TTL_SECONDS. fakeredis cuenta tiempo real,
    # así que pedimos un valor mínimo laxo (>0) y un máximo estricto.
    ttl = await fakeredis_async_client.ttl(flag_key)
    assert ttl > 0, f"el flag debería tener TTL > 0, tiene {ttl}"
    assert ttl <= KEY_STRESS_TTL_SECONDS, (
        f"el TTL ({ttl}) no debería exceder KEY_STRESS_TTL_SECONDS "
        f"({KEY_STRESS_TTL_SECONDS})"
    )


# ---------------------------------------------------------------------------
# test_002 — EDGE: evento inválido → 400 con mensaje espejado
# ---------------------------------------------------------------------------


async def test_002_stress_invalid_event_returns_400_with_spanish_detail(
    app_client, fakeredis_async_client
):
    """`POST /api/stress/unknown` → 400 Bad Request + `detail` contiene
    `"escenario desconocido"` Y lista todos los `STRESS_EVENTS` válidos
    (mismo texto que `publisher.simulator.SimulatorSource.inject_stress`)."""
    event = "unknown"

    resp = await app_client.post(f"/api/stress/{event}")
    assert resp.status_code == 400
    body = resp.json()

    # El detail es español y espeja el publisher.
    assert "escenario desconocido" in body["detail"]
    assert repr(event) in body["detail"]  # el repr del event aparece en el mensaje
    # Los 4 escenarios válidos están listados para que el operador sepa cuáles usar.
    for valid_event in STRESS_EVENTS:
        assert valid_event in body["detail"], (
            f"el detail debería listar el escenario válido {valid_event!r}, "
            f"pero el body fue: {body['detail']!r}"
        )

    # Y, crucialmente, NO escribió el flag en Redis.
    assert await fakeredis_async_client.exists(stress_key(event)) == 0
