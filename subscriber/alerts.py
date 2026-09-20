"""Subscriber alert engine + publish helper.

The engine (`AlertEngine`) is stateful across ticks — it keeps a per-rule
`consecutive_breaches` counter so the debounce (2 cycles before active) and
auto-clear (cleared on condition lift) behavior work correctly.

The publish helper (`publish_alert`) writes to five Redis structures in
this exact order:
  1. PUBLISH energy-events <json_payload>
  2. XADD alerts:stream MAXLEN ~ 100 * payload
  3. INCR alerts:total
  4. LPUSH alerts:recent <id> ; LTRIM alerts:recent 0 19
  5. INCR alerts:active:<code>   on state=active
     DECR alerts:active:<code>   on state=cleared (clamped at 0)

Per-event failure isolation (R4-001 pattern, inherited from publisher
bounded correction) is enforced by the caller's try/except — the engine
itself RAISES on internal errors so the caller can decide.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from typing import Any

from redis.asyncio import Redis

from common.models import Alert, AlertSeverity, DataSource, Event
from common.redis_keys import (
    KEY_ALERTS_TOTAL,
    KEY_RECENT_ALERTS,
    PUBSUB_CHANNEL_ENERGY,
    STREAM_ALERTS,
    STREAM_ALERTS_MAXLEN,
    alerts_active_key,
)


logger = logging.getLogger("subscriber.alerts")

# Threshold constants from spec REQ-SUB-ALERTS-001 / common.config.
THRESHOLD_A1_DEMAND_GEN_GAP_MW = 800.0
THRESHOLD_A2_RENEWABLE_PCT = 30.0
DEBOUNCE_CYCLES = 2

# Rule codes — wire format and per-code counter keys share these strings.
RULE_A1 = "DEMAND_GENERATION_GAP"
RULE_A2 = "LOW_RENEWABLE"

# Cap for the alerts:recent list (matches RECENT_ALERTS_LENGTH in redis_keys).
_RECENT_ALERTS_TRIM_TO = 19  # LTRIM 0 19 → 20 entries retained.


class AlertEngine:
    """Stateful across ticks. Maintains per-rule consecutive-breach counters.

    The engine's `_breaches` dict is keyed by `code` (the wire-format
    discriminator) so adding a new rule (A3, A4, ...) just means another
    key in the dict.

    Lifecycle:
      - `__init__` initializes counters at 0.
      - `set_fuente(...)` updates `last_fuente` so the published `Alert`
        carries the source of the tick that triggered the alert.
      - `evaluate(event, metrics)` returns ONLY NEW transitions (active OR
        cleared). Empty list = no transition to publish.
    """

    def __init__(self, last_fuente: DataSource = DataSource.SIM) -> None:
        self._breaches: dict[str, int] = {RULE_A1: 0, RULE_A2: 0}
        self._last_fuente: DataSource = last_fuente

    def set_fuente(self, fuente: DataSource) -> None:
        self._last_fuente = fuente

    def evaluate(
        self, event: Event, metrics: dict[str, dict[str, Any]]
    ) -> list[Alert]:
        """Evaluate both rules against the latest tick + metrics.

        Returns list of NEW alerts to publish (active on transition
        debounce → cleared on condition lift). Empty list means no
        transition (idempotent continuation or quiet condition).
        """
        alerts: list[Alert] = []
        gap = event.data.demanda_mw - event.data.generacion_mw
        alerts.extend(
            self._evaluate_rule(
                rule=RULE_A1,
                condition=(gap > THRESHOLD_A1_DEMAND_GEN_GAP_MW),
                severity=AlertSeverity.HIGH,
                value=gap,
                threshold=THRESHOLD_A1_DEMAND_GEN_GAP_MW,
                zone_id=event.entity_id,
                timestamp=event.timestamp,
            )
        )

        # A2 reads renewable_pct from the computed metrics; if missing or None
        # (no history yet for related metric), the rule simply does not fire.
        renewable_metric = metrics.get("renewable_pct")
        m1_value = renewable_metric.get("value") if renewable_metric else None
        if m1_value is not None:
            m1 = float(m1_value)
            alerts.extend(
                self._evaluate_rule(
                    rule=RULE_A2,
                    condition=(m1 < THRESHOLD_A2_RENEWABLE_PCT),
                    severity=AlertSeverity.MEDIUM,
                    value=m1,
                    threshold=THRESHOLD_A2_RENEWABLE_PCT,
                    zone_id=event.entity_id,
                    timestamp=event.timestamp,
                )
            )
        return alerts

    def _evaluate_rule(
        self,
        *,
        rule: str,
        condition: bool,
        severity: AlertSeverity,
        value: float,
        threshold: float,
        zone_id: str,
        timestamp: datetime,
    ) -> list[Alert]:
        """Apply the debounce + auto-clear decision table for ONE rule.

        Decision table (per spec REQ-SUB-ALERTS-001 §5):
        | condition holds | prev counter | action                | publish? |
        | ---             | ---          | ---                   | ---      |
        | T               | 0→1          | increment             | NO       |
        | T               | 1→2          | increment             | YES (active) |
        | T               | 2→3          | increment             | NO (idempotent) |
        | F (was active)  | n→0          | reset + cleared       | YES (cleared, cycles=n) |
        """
        counter = self._breaches.get(rule, 0)
        if condition:
            counter += 1
            self._breaches[rule] = counter
            if counter >= DEBOUNCE_CYCLES and counter == DEBOUNCE_CYCLES:
                # Exactly on transition into DEBOUNCE: publish `active`.
                return [
                    self._build_alert(
                        rule=rule,
                        state="active",
                        severity=severity,
                        value=value,
                        threshold=threshold,
                        zone_id=zone_id,
                        timestamp=timestamp,
                        consecutive_cycles=counter,
                    )
                ]
            # Cycle 1 (debounce suppressed) OR subsequent active cycles (idempotent).
            return []
        else:
            # Condition lifted.
            if counter > 0:
                cleared_alert = self._build_alert(
                    rule=rule,
                    state="cleared",
                    severity=severity,
                    value=value,
                    threshold=threshold,
                    zone_id=zone_id,
                    timestamp=timestamp,
                    consecutive_cycles=counter,
                )
                self._breaches[rule] = 0
                return [cleared_alert]
            return []

    @staticmethod
    def _build_alert(
        *,
        rule: str,
        state: str,
        severity: AlertSeverity,
        value: float,
        threshold: float,
        zone_id: str,
        timestamp: datetime,
        consecutive_cycles: int,
    ) -> Alert:
        """Build a fully-populated `Alert` (new fields included)."""
        return Alert(
            id=str(uuid.uuid4()),
            rule=rule,
            code=rule,  # wire-format discriminator == rule for now
            severity=severity,
            zone_id=zone_id,  # type: ignore[arg-type]
            value=value,
            threshold=threshold,
            state=state,  # type: ignore[arg-type]
            consecutive_cycles=consecutive_cycles,
            timestamp=timestamp,
            message=(
                f"{rule} {state} for zone {zone_id}: "
                f"{value:.2f} vs threshold {threshold:.2f}"
            ),
        )


async def publish_alert(redis: Redis, alert: Alert) -> None:
    """Publish an alert to Pub/Sub + Stream + counters + list.

    Order matters:
      1. PUBLISH energy-events <json_payload>   — downstream listeners.
      2. XADD alerts:stream MAXLEN ~ 100 * payload — append-only audit log.
      3. INCR alerts:total — total alerts ever published.
      4. LPUSH alerts:recent <id> ; LTRIM 0 19 — recent (cap 20).
      5. INCR/DECR alerts:active:<code> — current active per code
         (clamped at 0 on cleared to never expose negative).

    Atomicity is at-most-once (Q9 LOCKED); counter desync between
    `alerts:total` and `alerts:stream` is a documented known_issue.
    """
    code = alert.code or alert.rule
    payload = {
        "type": "alert",
        "id": alert.id,
        "code": code,
        "rule": alert.rule,
        "severity": alert.severity.value,
        "zone_id": alert.zone_id,
        "value": alert.value,
        "threshold": alert.threshold,
        "state": alert.state,
        "consecutive_cycles": alert.consecutive_cycles,
        "timestamp": alert.timestamp.isoformat(),
        "message": alert.message,
    }
    raw = json.dumps(payload, ensure_ascii=False)

    # 1. Pub/Sub.
    await redis.publish(PUBSUB_CHANNEL_ENERGY, raw)
    # 2. Stream (capped to MAXLEN ~ 100).
    await redis.xadd(
        STREAM_ALERTS,
        {"payload": raw},
        maxlen=STREAM_ALERTS_MAXLEN,
        approximate=True,
    )
    # 3. Total counter.
    await redis.incr(KEY_ALERTS_TOTAL)
    # 4. Recent list (cap 20).
    await redis.lpush(KEY_RECENT_ALERTS, alert.id)
    await redis.ltrim(KEY_RECENT_ALERTS, 0, _RECENT_ALERTS_TRIM_TO)
    # 5. Per-code active counter (INCR on active, DECR clamped on cleared).
    counter_key = alerts_active_key(code)
    if alert.state == "active":
        await redis.incr(counter_key)
    elif alert.state == "cleared":
        new_val = await redis.decr(counter_key)
        if new_val < 0:
            # Clamp to 0 — never expose negative counters (R4-001 spirit:
            # bad message must not produce nonsensical state).
            await redis.set(counter_key, 0)


__all__ = [
    "AlertEngine",
    "DEBOUNCE_CYCLES",
    "RULE_A1",
    "RULE_A2",
    "THRESHOLD_A1_DEMAND_GEN_GAP_MW",
    "THRESHOLD_A2_RENEWABLE_PCT",
    "publish_alert",
]
