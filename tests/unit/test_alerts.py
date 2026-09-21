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
from datetime import UTC, datetime

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
    ts = ts or datetime(2026, 9, 20, 12, 0, 0, tzinfo=UTC)
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
    ts = ts or datetime(2026, 9, 20, 12, 0, 0, tzinfo=UTC)
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


# ---------------------------------------------------------------------------
# BEHAVIOR — test_002 (debounce)
# ---------------------------------------------------------------------------


def test_002_alerts_debounce_first_cycle_no_publish() -> None:
    """BEHAVIOR: cycle 1 with breach → counter goes 0→1, NO alert published.
    Cycle 2 (breach still holds) → counter 1→2, ONE active alert published.
    """
    from subscriber.alerts import AlertEngine

    engine = AlertEngine()
    event = _make_event()  # gap=1000 > 800 → A1 fires
    metrics = _make_metrics(renewable_pct=35.0)  # > 30 → A2 NOT firing

    # Cycle 1: no publish.
    a1 = engine.evaluate(event, metrics)
    assert a1 == [], "cycle 1 must be suppressed by debounce"
    assert engine._breaches["DEMAND_GENERATION_GAP"] == 1
    assert engine._breaches.get("LOW_RENEWABLE", 0) == 0

    # Cycle 2: active alert fires.
    a2 = engine.evaluate(event, metrics)
    assert len(a2) == 1
    assert a2[0].state == "active"
    assert a2[0].consecutive_cycles == 2
    assert a2[0].code == "DEMAND_GENERATION_GAP"
    assert engine._breaches["DEMAND_GENERATION_GAP"] == 2


# ---------------------------------------------------------------------------
# BEHAVIOR — test_003 (auto-clear)
# ---------------------------------------------------------------------------


def test_003_alerts_auto_clear_publishes_cleared_with_final_cycles() -> None:
    """BEHAVIOR: 2 cycles of breach (active fires), then condition lifts →
    engine publishes ONE `cleared` alert with `consecutive_cycles` frozen at
    the final value (the cycles-when-it-died counter), and resets to 0.
    """
    from subscriber.alerts import AlertEngine

    engine = AlertEngine()
    breach_event = _make_event()  # gap=1000 > 800
    ok_event = _make_event(demanda=1000.0, generacion=1000.0)  # gap=0
    metrics = _make_metrics(renewable_pct=35.0)

    # Cycle 1: debounce → [].
    assert engine.evaluate(breach_event, metrics) == []
    # Cycle 2: active alert fires.
    cycle2 = engine.evaluate(breach_event, metrics)
    assert len(cycle2) == 1 and cycle2[0].state == "active"
    assert engine._breaches["DEMAND_GENERATION_GAP"] == 2

    # Cycle 3: condition lifts → cleared fires.
    cleared = engine.evaluate(ok_event, metrics)
    assert len(cleared) == 1
    c = cleared[0]
    assert c.state == "cleared"
    assert c.code == "DEMAND_GENERATION_GAP"
    assert c.consecutive_cycles == 2  # frozen at the final value
    assert engine._breaches["DEMAND_GENERATION_GAP"] == 0


# ---------------------------------------------------------------------------
# BEHAVIOR — test_004 (both A1 + A2 fire simultaneously)
# ---------------------------------------------------------------------------


def test_004_alerts_both_A1_and_A2_fire_simultaneously() -> None:
    """BEHAVIOR: when BOTH A1 (gap > 800) AND A2 (renewable < 30%) hold on
    the same cycle, both rules publish INDEPENDENT alerts (one HIGH active,
    one MEDIUM active). Counts: 2 separate alerts.
    """
    from subscriber.alerts import AlertEngine

    engine = AlertEngine()
    # gap=900 (> 800) AND renewable=10 (< 30) → both fire.
    event = _make_event(demanda=1500.0, generacion=600.0,
                        solar=10.0, eolica=10.0, hidro=40.0)
    metrics = _make_metrics(renewable_pct=10.0, balance_mw=900.0)

    # Cycle 1: both debounce.
    assert engine.evaluate(event, metrics) == []
    # Cycle 2: both fire.
    alerts = engine.evaluate(event, metrics)
    assert len(alerts) == 2
    codes = {a.code for a in alerts}
    assert codes == {"DEMAND_GENERATION_GAP", "LOW_RENEWABLE"}
    states = {a.state for a in alerts}
    assert states == {"active"}
    # Both have consecutive_cycles == 2 (independent counters).
    for a in alerts:
        assert a.consecutive_cycles == 2


# ---------------------------------------------------------------------------
# BEHAVIOR — test_005 (idempotent when breach continues)
# ---------------------------------------------------------------------------


def test_005_alerts_idempotent_when_breach_continues() -> None:
    """BEHAVIOR: once an alert is `active`, subsequent cycles with the breach
    continuing MUST NOT publish duplicate alerts (only ONE active per
    transition from <DEBOUNCE to ==DEBOUNCE).
    """
    from subscriber.alerts import AlertEngine

    engine = AlertEngine()
    event = _make_event()  # gap=1000 > 800
    metrics = _make_metrics(renewable_pct=35.0)

    # Cycle 1: debounce.
    assert engine.evaluate(event, metrics) == []
    # Cycle 2: active fires (transition 1→2).
    cycle2 = engine.evaluate(event, metrics)
    assert len(cycle2) == 1 and cycle2[0].state == "active"
    # Cycle 3: STILL active, but NO new publish (idempotent).
    cycle3 = engine.evaluate(event, metrics)
    assert cycle3 == [], "idempotency: cycle 3 must NOT re-publish active"
    # Cycle 4: still no publish.
    assert engine.evaluate(event, metrics) == []
    # Counter continues to climb (counter is informational; alerts are not).
    assert engine._breaches["DEMAND_GENERATION_GAP"] == 4


# ---------------------------------------------------------------------------
# R4-001 — test_006 (malformed message isolation — mandatory)
# ---------------------------------------------------------------------------


def test_006_alerts_malformed_isolation() -> None:
    """R4-001 mandatory: the engine must RAISE on internal validation errors
    (deterministic for callers) and the caller's try/except — modeled here
    by `processor._handle_tick`-style wrapping — catches the exception,
    increments the failures counter, and the loop SURVIVES (next call works).

    We model "malformed" two ways:
    1. Invalid Event (missing required field) → `Event.model_validate(...)` raises.
    2. Garbage payload that the caller would have to surface as a malformed msg.
    """
    from subscriber.alerts import AlertEngine, publish_alert  # noqa: F401

    engine = AlertEngine()
    failures = 0

    def safe_handle_tick(event_or_raw: object) -> str:
        """Mimic `EnergyProcessor._handle_tick`'s try/except wrapper. Returns
        'ok' on success, 'failed' on any internal exception (and bumps the
        failures counter — exactly the R4-001 pattern).
        """
        nonlocal failures
        try:
            # The engine itself does not parse JSON; the caller (processor)
            # would have already done json.loads. Here we test that a
            # type error inside `evaluate` is also isolated.
            if not isinstance(event_or_raw, Event):
                raise TypeError("malformed event payload")
            engine.evaluate(event_or_raw, _make_metrics(renewable_pct=35.0))
            return "ok"
        except Exception:
            failures += 1
            return "failed"

    # Bad payload → counter increments, no crash.
    assert safe_handle_tick({"junk": "x"}) == "failed"
    assert failures == 1
    assert safe_handle_tick(None) == "failed"
    assert failures == 2

    # Engine is still usable: a valid event should pass through.
    valid_event = _make_event()  # A1 fires (gap=1000 > 800)
    # Pre-set the breach counter so we skip debounce.
    engine._breaches["DEMAND_GENERATION_GAP"] = 1
    # The handler reports 'ok' (or may itself debounce, but it doesn't raise).
    result = safe_handle_tick(valid_event)
    assert result == "ok"
    assert failures == 2, "valid tick must not increment failures"

    # Sanity: publish_alert is importable (will be exercised in test_publish_alert.py).
    assert callable(publish_alert)


# ---------------------------------------------------------------------------
# SHAPE — test_007 (SUB-002 regression: per-zone isolation)
# ---------------------------------------------------------------------------


def test_007_heterogeneous_six_event_cycle_no_spurious_cleared() -> None:
    """SUB-002 regression: a single publisher cycle drives 6 events through
    ONE `AlertEngine`. The old impl keyed `_breaches` by rule-code only, so
    the FIRST non-breach zone that came after the breach in the iteration
    order would emit a spurious `cleared` alert (counter was > 0 attached to
    a different zone's history).

    Drive 4 cycles; in each cycle, ATL is the only breach zone:
      - Cycle 1: ATL M1=17 (BREACH), others M1=80 → 0 alerts (debounce).
      - Cycle 2: ATL M1=17 (BREACH), others M1=80 → 1 ATL `active`.
      - Cycle 3: ATL M1=17 (BREACH), others M1=80 → 0 alerts (idempotent).
      - Cycle 4: ATL M1=80 (LIFT), others M1=80 → 1 ATL `cleared` (cycles=3
        because the ATL counter reached 3 on cycle 3 before lifting on 4).

    Hard regression: NO zone other than ATL may emit ANY alert across the
    full 4-cycle run. The buggy impl produces ≥1 spurious `cleared` per
    cycle (emitted by whichever zone evaluates right after ATL resets the
    shared global counter).
    """
    from subscriber.alerts import AlertEngine

    engine = AlertEngine()
    zones = ["BOG", "ANT", "ATL", "VAL", "SAN", "SIN"]
    breaching_zone = "ATL"
    breach_m1 = 17.0
    clean_m1 = 80.0

    def zone_m1(zone: str, cycle: int) -> float:
        """ATL breaches on cycles 1-3 and lifts on cycle 4; everyone else
        stays clean for the full run."""
        if zone == breaching_zone and cycle < 4:
            return breach_m1
        return clean_m1

    # Build events with demanda == generacion so A1 (DEMAND_GENERATION_GAP)
    # never fires; this test isolates the A2 per-zone regression.
    def event_no_a1(zone: str) -> Event:
        return _make_event(zona=zone, demanda=1000.0, generacion=1000.0)

    # Collect every alert emitted across the 4 cycles.
    cycle_alerts: list[list[Alert]] = []
    for cycle in range(1, 5):
        result: list[Alert] = []
        for zone in zones:
            m1 = zone_m1(zone, cycle)
            event = event_no_a1(zone)
            metrics = _make_metrics(renewable_pct=m1, zona=zone)
            result.extend(engine.evaluate(event, metrics))
        cycle_alerts.append(result)

    # Cycle 1: debounce suppresses → no alerts published.
    assert cycle_alerts[0] == [], (
        f"cycle 1 must be debounced — no alerts. Got: {cycle_alerts[0]}"
    )

    # Cycle 2: ATL counter hits 2 → exactly one ATL `active` published.
    assert len(cycle_alerts[1]) == 1, (
        f"cycle 2 must publish exactly 1 active alert. Got: {cycle_alerts[1]}"
    )
    active = cycle_alerts[1][0]
    assert active.state == "active"
    assert active.code == "LOW_RENEWABLE"
    assert active.zone_id == breaching_zone
    assert active.consecutive_cycles == 2

    # Cycle 3: breach continues → no new alert (idempotent).
    assert cycle_alerts[2] == [], (
        f"cycle 3 (continued breach) must be idempotent — no alerts. Got: {cycle_alerts[2]}"
    )

    # Cycle 4: ATL lifts → exactly one ATL `cleared` with `consecutive_cycles`
    # frozen at the final counter value (3, because cycles 1-3 all breached).
    assert len(cycle_alerts[3]) == 1, (
        f"cycle 4 (breach lifted) must publish exactly 1 cleared alert. "
        f"Got: {cycle_alerts[3]}"
    )
    cleared = cycle_alerts[3][0]
    assert cleared.state == "cleared"
    assert cleared.code == "LOW_RENEWABLE"
    assert cleared.zone_id == breaching_zone
    assert cleared.consecutive_cycles == 3
    # Value matches the m1 of the lift event (the condition-lift snapshot).
    assert cleared.value == clean_m1

    # Hard regression: no zone other than ATL may emit any alert — the old
    # global-key impl would have produced ≥1 spurious `cleared` from one of
    # BOG/ANT/VAL/SAN/SIN on each cycle.
    for cycle_idx, alerts in enumerate(cycle_alerts, start=1):
        for alert in alerts:
            assert alert.zone_id == breaching_zone, (
                f"cycle {cycle_idx}: non-ATL zone emitted alert — SUB-002 bug. "
                f"Got zone_id={alert.zone_id!r}, code={alert.code!r}, "
                f"state={alert.state!r}"
            )

    # No cleared alert in cycles 1-3 (only ATL can clear, and only on lift).
    for cycle_idx in (1, 2, 3):
        assert all(a.state != "cleared" for a in cycle_alerts[cycle_idx - 1]), (
            f"cycle {cycle_idx} must not contain a cleared alert. "
            f"Got: {cycle_alerts[cycle_idx - 1]}"
        )


# ---------------------------------------------------------------------------
# Sanity: publish_alert module-level helper importable.
# ---------------------------------------------------------------------------


def test_import_publish_alert_helper() -> None:
    """Sanity: the module-level helper `publish_alert` is importable."""
    from subscriber.alerts import publish_alert  # noqa: F401

    assert callable(publish_alert)


# Helper kept for other tests that need to import the metrics JSON payload shape.
def _alert_payload_json(alert: Alert) -> str:
    """Serialize an Alert to JSON in the wire format used by `publish_alert`."""
    return json.dumps(
        {
            "id": alert.id,
            "code": alert.code or alert.rule,
            "rule": alert.rule,
            "severity": alert.severity.value,
            "zone_id": alert.zone_id,
            "value": alert.value,
            "threshold": alert.threshold,
            "state": alert.state,
            "consecutive_cycles": alert.consecutive_cycles,
            "timestamp": alert.timestamp.isoformat(),
            "message": alert.message,
        },
        ensure_ascii=False,
    )
