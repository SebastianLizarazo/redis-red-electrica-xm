"""Tests for subscriber.alerts — SHAPE first, then BEHAVIOR.

Convención Strict TDD (subscriber-core-2026-09):
- test_001 = SHAPE (`AlertEngine().evaluate()` returns `list[Alert]`).
- test_002 = BEHAVIOR: debounce (cycle 1 no publish, cycle 2 active).
- test_003 = BEHAVIOR: auto-clear (`state=cleared` when condition lifts).
- test_004 = BEHAVIOR: both rules fire simultaneously.
- test_005 = BEHAVIOR: idempotent when breach continues (3rd cycle no dup).
- test_006 = R4-001 mandatory: malformed-msg isolation — engine RAISES on
  bad payload; caller (`processor._handle_tick`) catches via try/except and
  increments its failure counter, but does NOT crash the loop.

Each test deliberately exercises ONE behavior so failures localize cleanly.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from common.models import (
    Alert,
    AlertSeverity,
    DataSource,
    Event,
    EventData,
    Location,
)


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(
    *,
    zona: str = "ANT",
    demanda: float = 2000.0,
    generacion: float = 1000.0,
    solar: float = 100.0,
    eolica: float = 50.0,
    hidro: float = 200.0,
    termica: float = 650.0,
    fuente: DataSource = DataSource.SIM,
    ts: datetime | None = None,
) -> Event:
    """Fabrica un Event con valores por defecto que disparan A1
    (`demanda - generacion = 1000 > 800`). Tests pueden sobrescribir."""
    ts = ts or datetime(2026, 9, 20, 12, 0, 0, tzinfo=timezone.utc)
    return Event(
        entity_id=zona,  # type: ignore[arg-type]
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


def _make_metrics(
    *,
    renewable_pct: float | None = 35.0,
    balance_mw: float | None = 1000.0,
    demand_variation_pct: float | None = None,
    zona: str = "ANT",
    fuente: DataSource = DataSource.SIM,
    ts: datetime | None = None,
) -> dict[str, dict[str, object]]:
    """Construye el dict de métricas que `evaluate()` recibe como segundo
    argumento (forma devuelta por `subscriber.metrics.compute_metrics`)."""
    ts = ts or datetime(2026, 9, 20, 12, 0, 0, tzinfo=timezone.utc)
    iso = ts.isoformat()
    base: dict[str, dict[str, object]] = {
        "renewable_pct": {
            "value": renewable_pct,
            "unit": "%",
            "timestamp": iso,
            "zone_id": zona,
            "fuente": fuente.value,
        },
        "balance_mw": {
            "value": balance_mw,
            "unit": "MW",
            "timestamp": iso,
            "zone_id": zona,
            "fuente": fuente.value,
        },
        "demand_variation_pct": {
            "value": demand_variation_pct,
            "unit": "%",
            "timestamp": iso,
            "zone_id": zona,
            "fuente": fuente.value,
        },
    }
    return base


# ---------------------------------------------------------------------------
# SHAPE — test_001
# ---------------------------------------------------------------------------


def test_001_alerts_shape_returns_list_of_alerts() -> None:
    """SHAPE: `AlertEngine().evaluate(event, metrics)` returns `list[Alert]`
    with the new fields (`code`, `state`, `consecutive_cycles`) populated.

    On the FIRST cycle with a breach, debounce suppresses publish → assert
    `evaluate` returns `[]` to honor "no transition on cycle 1". On the
    SECOND cycle we manually pre-set the breach counter (simulating prior
    cycle) and assert a fully-formed `Alert` with the canonical fields.
    """
    from subscriber.alerts import AlertEngine  # noqa: F401  (FAIL pre-impl)

    engine = AlertEngine()
    event = _make_event()  # demanda=2000, generacion=1000 → gap=1000 > 800
    metrics = _make_metrics(renewable_pct=35.0, balance_mw=1000.0)

    # Cycle 1: breach holds, counter goes 0→1, debounce suppresses.
    alerts_cycle1 = engine.evaluate(event, metrics)
    assert isinstance(alerts_cycle1, list)
    assert alerts_cycle1 == [], "cycle 1 must be debounced — no alert published"

    # Simulate prior cycle already incremented the counter (cycle 2 effectively).
    engine._breaches["DEMAND_GENERATION_GAP"] = 1
    alerts_cycle2 = engine.evaluate(event, metrics)
    assert len(alerts_cycle2) == 1, "cycle 2 must publish exactly one active alert"

    a = alerts_cycle2[0]
    assert isinstance(a, Alert)
    assert a.code == "DEMAND_GENERATION_GAP"
    assert a.state == "active"
    assert a.consecutive_cycles == 2
    assert a.severity == AlertSeverity.HIGH
    assert a.threshold == 800.0
    assert a.value == 1000.0  # demanda - generacion
    assert a.zone_id == "ANT"
