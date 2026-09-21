"""Tests for `subscriber.alerts.publish_alert` module-level helper.

`publish_alert(redis, alert)` writes a 5-key transaction:
1. PUBLISH energy-events <json_payload>
2. XADD alerts:stream MAXLEN ~ 100 * <payload>
3. INCR alerts:total
4. LPUSH alerts:recent <id> ; LTRIM 0 19
5. INCR alerts:active:<code>  on `state == "active"`
   DECR alerts:active:<code>  on `state == "cleared"` (clamped at 0)

The helper is async. We use the existing `fakeredis_async_client` fixture
from `tests/conftest.py` to drive it without a real Redis instance.
"""
from __future__ import annotations

import json
from datetime import UTC

import pytest

from common.models import Alert, AlertSeverity
from common.redis_keys import (
    KEY_ALERTS_TOTAL,
    KEY_RECENT_ALERTS,
    PUBSUB_CHANNEL_ENERGY,
    STREAM_ALERTS,
    alerts_active_key,
)
from subscriber.alerts import publish_alert  # noqa: F401  (FAIL pre-impl)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_active_alert(code: str = "DEMAND_GENERATION_GAP") -> Alert:
    """Build a fully-populated `active` Alert (new fields included)."""
    from datetime import datetime

    return Alert(
        id="alert-pub-001",
        rule=code,
        code=code,
        severity=AlertSeverity.HIGH,
        zone_id="ANT",
        value=1000.0,
        threshold=800.0,
        state="active",
        consecutive_cycles=2,
        timestamp=datetime(2026, 9, 20, 12, 0, 0, tzinfo=UTC),
        message=f"{code} active for ANT: 1000.00 vs threshold 800.00",
    )


def _make_cleared_alert(code: str = "DEMAND_GENERATION_GAP") -> Alert:
    """Build a `cleared` Alert (same shape, state=cleared)."""
    from datetime import datetime

    return Alert(
        id="alert-pub-002",
        rule=code,
        code=code,
        severity=AlertSeverity.HIGH,
        zone_id="ANT",
        value=200.0,
        threshold=800.0,
        state="cleared",
        consecutive_cycles=2,
        timestamp=datetime(2026, 9, 20, 12, 5, 0, tzinfo=UTC),
        message=f"{code} cleared for ANT",
    )


# ---------------------------------------------------------------------------
# BEHAVIOR — publish_alert writes 5 Redis structures
# ---------------------------------------------------------------------------


async def test_publish_alert_writes_pub_sub_stream_incr_lpush(
    fakeredis_async_client,
) -> None:
    """End-to-end on fakeredis: `publish_alert(active_alert)` populates:
    - Pub/Sub channel `energy-events` (1 message),
    - Stream `alerts:stream` (1 XADD entry),
    - Counter `alerts:total` (= 1),
    - List `alerts:recent` (length 1, head = alert.id),
    - Counter `alerts:active:DEMAND_GENERATION_GAP` (= 1).
    """
    redis = fakeredis_async_client
    alert = _make_active_alert()

    # Set up an in-process subscriber BEFORE publish to capture the payload.
    pubsub = redis.pubsub()
    await pubsub.subscribe(PUBSUB_CHANNEL_ENERGY)
    try:
        # Publish.
        await publish_alert(redis, alert)

        # 1. Pub/Sub: drain the channel.
        msg = await pubsub.get_message(timeout=2.0, ignore_subscribe_messages=True)
        # First non-subscribe message is the payload.
        while msg is None or msg.get("type") != "message":
            msg = await pubsub.get_message(
                timeout=2.0, ignore_subscribe_messages=True
            )
        assert msg is not None
        payload = json.loads(msg["data"])
        assert payload["code"] == "DEMAND_GENERATION_GAP"
        assert payload["state"] == "active"
        assert payload["consecutive_cycles"] == 2
        assert payload["zone_id"] == "ANT"
        assert payload["severity"] == "high"

        # 2. Stream alerts:stream has 1 entry.
        stream_len = await redis.xlen(STREAM_ALERTS)
        assert stream_len == 1

        # 3. Counter alerts:total == 1.
        total = await redis.get(KEY_ALERTS_TOTAL)
        assert total == "1"

        # 4. List alerts:recent has length 1, head = alert.id.
        recent = await redis.lrange(KEY_RECENT_ALERTS, 0, -1)
        assert recent == ["alert-pub-001"]

        # 5. Per-code active counter == 1.
        active_key = alerts_active_key("DEMAND_GENERATION_GAP")
        active_count = await redis.get(active_key)
        assert active_count == "1"
    finally:
        try:
            await pubsub.unsubscribe(PUBSUB_CHANNEL_ENERGY)
            await pubsub.aclose()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# BEHAVIOR — cleared decrements counter (clamped at 0)
# ---------------------------------------------------------------------------


async def test_publish_alert_cleared_decrements_active_counter(
    fakeredis_async_client,
) -> None:
    """When an alert is `cleared`, the per-code counter is DECR'd. If the
    counter would go below 0, it is clamped to 0 (we never expose negative).
    """
    redis = fakeredis_async_client
    active_key = alerts_active_key("DEMAND_GENERATION_GAP")

    # Pre-set counter to 2 (e.g. two zones active, then both clear).
    await redis.set(active_key, 2)

    cleared = _make_cleared_alert()
    await publish_alert(redis, cleared)

    val = await redis.get(active_key)
    assert int(val) == 1, "single cleared should DECR by 1"

    # Publish another cleared → counter clamped at 0.
    await publish_alert(redis, cleared)
    val = await redis.get(active_key)
    assert int(val) == 0

    # One more cleared → still 0 (clamp; never negative).
    await publish_alert(redis, cleared)
    val = await redis.get(active_key)
    assert int(val) == 0
