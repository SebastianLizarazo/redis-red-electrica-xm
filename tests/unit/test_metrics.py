"""Tests for subscriber.metrics — SHAPE first, then BEHAVIOR.

Convención Strict TDD (subscriber-core-2026-09):
- test_001 = SHAPE (data structure del output de `compute_metrics`).
- test_002..006 = BEHAVIOR (M1 / M2 / M3 con casos típicos y de borde).
- test_007 = persistencia (round-trip via `fakeredis_async_client`).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from common.models import DataSource, Event, EventData, Location, ZoneId
from subscriber.metrics import compute_metrics, persist_metrics  # noqa: F401  (FAIL pre-impl)


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(
    *,
    zone_id: ZoneId = "ANT",
    demanda: float = 1000.0,
    generacion: float = 1100.0,
    solar: float = 200.0,
    eolica: float = 100.0,
    hidro: float = 600.0,
    termica: float = 200.0,
    fuente: DataSource = DataSource.SIM,
    ts: datetime | None = None,
) -> Event:
    """Fabrica un Event con valores por defecto plausibles para los tests.

    Defaults: ANT, demanda=1000, generacion=1100, solar=200, eolica=100,
    hidro=600, termica=200 (suma renewable = 900 → M1 ≈ 81.8 %), fuente=SIM.
    """
    ts = ts or datetime(2026, 9, 20, 12, 0, 0, tzinfo=timezone.utc)
    return Event(
        entity_id=zone_id,
        timestamp=ts,
        location=Location(latitude=6.0, longitude=-75.0),
        data=EventData(
            demanda_mw=demanda,
            generacion_mw=generacion,
            generacion_solar_mw=solar,
            generacion_eolica_mw=eolica,
            generacion_hidraulica_mw=hidro,
            generacion_termica_mw=termica,
            fuente=fuente,
        ),
    )


# ---------------------------------------------------------------------------
# SHAPE — test_001
# ---------------------------------------------------------------------------


def test_001_metrics_shape_returns_dict_with_three_metrics() -> None:
    """SHAPE: `compute_metrics` devuelve `dict[str, dict]` con exactamente
    tres claves (`renewable_pct`, `balance_mw`, `demand_variation_pct`) y
    cada métrica incluye los campos `value`, `unit`, `timestamp`,
    `zone_id` y `fuente`.
    """
    event = _make_event()
    metrics = compute_metrics(event, previous_demand_mw=None)

    assert set(metrics.keys()) == {"renewable_pct", "balance_mw", "demand_variation_pct"}
    for name, m in metrics.items():
        assert "value" in m, f"{name} missing 'value'"
        assert "unit" in m, f"{name} missing 'unit'"
        assert "timestamp" in m, f"{name} missing 'timestamp'"
        assert "zone_id" in m, f"{name} missing 'zone_id'"
        assert "fuente" in m, f"{name} missing 'fuente'"
        assert m["zone_id"] == "ANT"


# ---------------------------------------------------------------------------
# BEHAVIOR — test_002..006
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("generacion", "solar", "eolica", "hidro", "expected_pct"),
    [
        # Caso canónico del spec (REQ-SUB-METRICS-001 scenario 1):
        # 200 + 100 + 300 = 600 renewable sobre 1000 total = 60.0 %.
        (1000.0, 200.0, 100.0, 300.0, 60.0),
        # 100 % renewable (toda hidráulica, p.ej. en horas de baja térmica).
        (800.0, 0.0, 0.0, 800.0, 100.0),
        # 0 % renewable (sólo térmica).
        (500.0, 0.0, 0.0, 0.0, 0.0),
        # Mezcla realista madrugada: solar=0, eólica alta, hidro media.
        # 0 + 300 + 450 = 750 / 1000 = 75.0 %.
        (1000.0, 0.0, 300.0, 450.0, 75.0),
    ],
)
def test_002_metrics_M1_renewable_percentage(
    generacion: float, solar: float, eolica: float, hidro: float, expected_pct: float
) -> None:
    """M1 = (solar + eolica + hidro) / generacion * 100, parametrizado sobre
    mezclas típicas. La térmica queda fuera del cómputo por convención.
    """
    event = _make_event(
        generacion=generacion,
        solar=solar,
        eolica=eolica,
        hidro=hidro,
        termica=max(generacion - (solar + eolica + hidro), 0.0),
    )
    metrics = compute_metrics(event, previous_demand_mw=None)

    assert metrics["renewable_pct"]["value"] == pytest.approx(expected_pct)
    assert metrics["renewable_pct"]["unit"] == "%"


def test_003_metrics_M2_balance() -> None:
    """M2 = demanda - generacion. Negativo = superávit."""
    # Déficit: demanda > generacion (300 MW).
    deficit_event = _make_event(demanda=1300.0, generacion=1000.0)
    # Superávit: demanda < generacion (-200 MW).
    surplus_event = _make_event(demanda=800.0, generacion=1000.0)
    # Equilibrio exacto.
    zero_event = _make_event(demanda=1000.0, generacion=1000.0)

    assert compute_metrics(deficit_event, previous_demand_mw=None)["balance_mw"]["value"] == 300.0
    assert compute_metrics(surplus_event, previous_demand_mw=None)["balance_mw"]["value"] == -200.0
    assert compute_metrics(zero_event, previous_demand_mw=None)["balance_mw"]["value"] == 0.0
    # Unit consistente en los 3 casos.
    for ev in (deficit_event, surplus_event, zero_event):
        assert compute_metrics(ev, previous_demand_mw=None)["balance_mw"]["unit"] == "MW"


def test_004_metrics_M3_no_history_returns_None() -> None:
    """Primer tick de la vida del subscriber: no hay histórico previo → M3 = None.

    El processor (PR-B) traduce esto a literal "NaN" en el hash; aquí
    verificamos la capa de cómputo.
    """
    event = _make_event(demanda=1000.0)
    metrics = compute_metrics(event, previous_demand_mw=None)

    assert metrics["demand_variation_pct"]["value"] is None
    assert metrics["demand_variation_pct"]["unit"] == "%"


@pytest.mark.parametrize(
    ("previous", "current", "expected_delta"),
    [
        (1000.0, 1100.0, 10.0),  # escenario canónico del spec
        (1000.0, 900.0, -10.0),  # caída del 10 %
        (2000.0, 2100.0, 5.0),  # subida del 5 %
        (500.0, 500.0, 0.0),  # sin cambio
    ],
)
def test_005_metrics_M3_with_history(previous: float, current: float, expected_delta: float) -> None:
    """M3 = (actual - previa) / previa * 100 cuando hay histórico."""
    event = _make_event(demanda=current)
    metrics = compute_metrics(event, previous_demand_mw=previous)

    assert metrics["demand_variation_pct"]["value"] == pytest.approx(expected_delta)
    assert metrics["demand_variation_pct"]["unit"] == "%"


def test_006_metrics_generacion_zero_returns_M1_zero() -> None:
    """Borde del spec (REQ-SUB-METRICS-001 scenario 2): generacion_mw == 0
    → M1 = 0.0 sin lanzar ZeroDivisionError.
    """
    # Demanda > 0 pero generación = 0 (apagón total simulado).
    event = _make_event(demanda=1500.0, generacion=0.0)
    metrics = compute_metrics(event, previous_demand_mw=None)

    assert metrics["renewable_pct"]["value"] == 0.0
    # M2 sí refleja el desbalance real (1500 - 0 = 1500 MW de déficit).
    assert metrics["balance_mw"]["value"] == 1500.0


# ---------------------------------------------------------------------------
# PERSISTENCE — test_007
# ---------------------------------------------------------------------------


async def test_007_metrics_persist_writes_three_hashes(fakeredis_async_client) -> None:
    """Round-trip via fakeredis: `persist_metrics` debe popular los 3 hashes
    `metrics:renewable`, `metrics:balance`, `metrics:demand:variation` con los
    cinco campos canónicos (`value`, `unit`, `timestamp`, `zone_id`, `fuente`).

    M3 con `previous_demand_mw=None` debe persistirse como literal `"NaN"`.
    """
    event = _make_event(demanda=1000.0, generacion=1100.0, solar=200.0, eolica=100.0, hidro=600.0)
    metrics = compute_metrics(event, previous_demand_mw=None)

    await persist_metrics(fakeredis_async_client, metrics)

    for hash_key, metric in metrics.items():
        redis_key = {
            "renewable_pct": "metrics:renewable",
            "balance_mw": "metrics:balance",
            "demand_variation_pct": "metrics:demand:variation",
        }[hash_key]
        h = await fakeredis_async_client.hgetall(redis_key)
        assert h, f"hash {redis_key} está vacío tras persist_metrics"
        # Campos canónicos (5).
        assert set(h.keys()) == {"value", "unit", "timestamp", "zone_id", "fuente"}
        assert h["zone_id"] == "ANT"
        assert h["fuente"] == "simulator"
        # M3 sin histórico → literal "NaN".
        if metric["value"] is None:
            assert h["value"] == "NaN"
        else:
            assert float(h["value"]) == pytest.approx(float(metric["value"]))
