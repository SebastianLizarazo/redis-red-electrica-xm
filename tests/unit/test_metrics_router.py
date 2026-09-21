"""Tests for `api.routers.metrics` (REQ-API-003 de spec #510).

Convención STANDARD: TDD laxo, tests con código OK.

Cobertura:
- test_001 HAPPY: 3 hashes con valores numéricos → 3 items, todos con value numérico.
- test_002 EDGE: M3 con `value == "NaN"` (string en el hash) → el 3er item tiene
  `value: null` en el JSON (REQ-API-003 scenario 2, design #511 §6).

Estrategia: usamos `app_client` (fixture de `tests/conftest.py`) que
crea una FastAPI app con `dependency_overrides[get_redis] = fakeredis`.
Sembramos los hashes directamente con HSET y verificamos el GET.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from common.redis_keys import (
    KEY_METRICS_BALANCE,
    KEY_METRICS_DEMAND_VARIATION,
    KEY_METRICS_RENEWABLE,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers — wire format del subscriber (subscriber/metrics.py:persist_metrics)
# ---------------------------------------------------------------------------


def _hash_for(
    *,
    value: str,
    unit: str = "%",
    zone_id: str = "SIN",
    fuente: str = "simulator",
    ts: datetime | None = None,
) -> dict[str, str]:
    """Fabrica el hash `metrics:*` con el wire format del subscriber.

    El campo `value` se pasa como STRING porque el subscriber escribe
    todo como string (incluso los floats) y el cliente real Redis nunca
    te devuelve floats para HGETALL.
    """
    ts = ts or datetime(2026, 9, 21, 12, 0, 0, tzinfo=UTC)
    return {
        "value": value,
        "unit": unit,
        "timestamp": ts.isoformat(),
        "zone_id": zone_id,
        "fuente": fuente,
    }


async def _seed_three_numeric(fakeredis_async_client) -> None:
    """Sembrar las 3 métricas con valores numéricos válidos."""
    await fakeredis_async_client.hset(
        KEY_METRICS_RENEWABLE,
        mapping=_hash_for(value="72.5", unit="%"),
    )
    await fakeredis_async_client.hset(
        KEY_METRICS_BALANCE,
        mapping=_hash_for(value="-300.0", unit="MW"),
    )
    await fakeredis_async_client.hset(
        KEY_METRICS_DEMAND_VARIATION,
        mapping=_hash_for(value="1.25", unit="%"),
    )


# ---------------------------------------------------------------------------
# test_001 — HAPPY: 3 metrics con valores numéricos
# ---------------------------------------------------------------------------


async def test_001_metrics_three_numeric_values_returned_in_order(
    app_client, fakeredis_async_client
):
    """Los 3 hashes poblados con strings numéricos → 3 items con value numérico,
    en orden canónico (renewable_pct, balance_mw, demand_variation_pct)."""
    await _seed_three_numeric(fakeredis_async_client)

    resp = await app_client.get("/api/metrics")
    assert resp.status_code == 200
    body = resp.json()

    # Exactamente 3 items en orden canónico.
    assert isinstance(body, list)
    assert len(body) == 3
    assert [m["name"] for m in body] == [
        "renewable_pct",
        "balance_mw",
        "demand_variation_pct",
    ]

    # Cada item tiene value numérico (no null, no string).
    assert body[0]["value"] == 72.5
    assert body[1]["value"] == -300.0
    assert body[2]["value"] == 1.25

    # Units preservados del wire format.
    assert body[0]["unit"] == "%"
    assert body[1]["unit"] == "MW"
    assert body[2]["unit"] == "%"


# ---------------------------------------------------------------------------
# test_002 — EDGE: M3 con "NaN" → JSON `null` (REQ-API-003 scenario 2)
# ---------------------------------------------------------------------------


async def test_002_metrics_M3_NaN_serializes_as_null_in_json(
    app_client, fakeredis_async_client
):
    """M3 sin histórico (subscriber escribe `value: "NaN"` literal) →
    el 3er item tiene `value: null` en el JSON. M1 y M2 son numéricos
    normalmente."""
    # M1 y M2 numéricos.
    await fakeredis_async_client.hset(
        KEY_METRICS_RENEWABLE,
        mapping=_hash_for(value="50.0", unit="%"),
    )
    await fakeredis_async_client.hset(
        KEY_METRICS_BALANCE,
        mapping=_hash_for(value="150.0", unit="MW"),
    )
    # M3 con el literal "NaN" (lo que subscriber/metrics.py:persist_metrics
    # escribe cuando m3 = None).
    await fakeredis_async_client.hset(
        KEY_METRICS_DEMAND_VARIATION,
        mapping=_hash_for(value="NaN", unit="%"),
    )

    resp = await app_client.get("/api/metrics")
    assert resp.status_code == 200
    body = resp.json()

    assert len(body) == 3
    # M1 y M2 normales.
    assert body[0]["value"] == 50.0
    assert body[1]["value"] == 150.0
    # M3: el contrato clave del spec. None en Python == null en JSON.
    assert body[2]["value"] is None
    assert body[2]["name"] == "demand_variation_pct"
