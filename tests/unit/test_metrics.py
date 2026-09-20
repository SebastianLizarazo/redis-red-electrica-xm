"""Tests for subscriber.metrics — SHAPE first, then BEHAVIOR.

Convención Strict TDD (subscriber-core-2026-09):
- test_001 = SHAPE (data structure del output de `compute_metrics`).
- test_002..006 = BEHAVIOR (M1 / M2 / M3 con casos típicos y de borde).
- test_007 = persistencia (round-trip via `fakeredis_async_client`).
"""
from __future__ import annotations

from datetime import datetime, timezone

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
