"""Integration tests for subscriber.processor.

`tests/integration/test_processor.py` exercises the `EnergyProcessor`
end-to-end against a `fakeredis_async_client`, using an in-process
publisher-stub (no real Redis required).

Convención Strict TDD:
- test_001 = SHAPE: `EnergyProcessor(redis).run()` consumes a single tick
  from `energy-events` and persists the 3 metric hashes + ZSet history.
- test_002 = BEHAVIOR: a tick that crosses the DEMAND_GENERATION_GAP
  threshold publishes alerts to 5 Redis keys (stream + counters + list).
- test_003 = BEHAVIOR: `_housekeep()` prunes ZSet entries older than the
  60-min window (DEMAND_HISTORY_WINDOW_SECONDS).
- test_004 = BEHAVIOR: setting `_stop` breaks the consume loop and Redis
  is closed cleanly within a bounded window (SIGTERM-equivalent).

Test ordering caveat (RISK-N3 from spec #493):
- fakeredis pub/sub is in-process. `await redis.publish(...)` to a channel
  with NO subscriber is a no-op. The test must `await pubsub.subscribe(...)`
  BEFORE the stub publishes, in the same event loop. We use
  `asyncio.gather(subscribe_then_capture, consume_loop)` to keep the order
  deterministic.

For PR-B2 we also exercise module-private handlers (`_handle_tick`,
`_housekeep`, `_stop`) directly. They are intentionally module-private
per the processor's design contract (only `run()` is the public surface
for the consumer), so we silence `attr-defined` warnings at the call site.
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime

import pytest

from common.models import DataSource, Event, EventData, Location

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(
    *,
    zone: str = "SIN",
    demanda: float = 2000.0,
    generacion: float = 1500.0,
    solar: float = 200.0,
    eolica: float = 100.0,
    hidro: float = 700.0,
    termica: float = 500.0,
    fuente: DataSource = DataSource.SIM,
    ts: datetime | None = None,
) -> Event:
    """Fabrica un Event del sistema (zone='SIN' para el global)."""
    ts = ts or datetime(2026, 9, 20, 12, 0, 0, tzinfo=UTC)
    return Event(
        entity_id=zone,  # type: ignore[arg-type]
        timestamp=ts,
        location=Location(latitude=4.5, longitude=-74.1),
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
# SHAPE — test_001 (fakeredis roundtrip; subscribe-then-publish)
# ---------------------------------------------------------------------------


async def test_001_processor_fakeredis_roundtrip(
    fakeredis_async_client,
) -> None:
    """End-to-end: publisher-stub publishes one tick → `EnergyProcessor.run()`
    consumes it → asserts `metrics:*` hashes populated + ZSet history entry.

    Critical ordering (RISK-N3):
      1. Subscriber registers (`pubsub.subscribe`).
      2. Publisher publishes.
      3. Processor polls and handles.
    All three happen in the same event loop. We use a side subscriber
    (`subscribe_then_capture`) only to PROVE the publish actually lands
    (the processor also subscribes).
    """
    from subscriber.processor import EnergyProcessor  # noqa: F401  (FAIL pre-impl)

    redis = fakeredis_async_client
    processor = EnergyProcessor(redis)

    side_event_received = asyncio.Event()

    async def subscribe_then_capture() -> None:
        """Side subscriber that confirms the publish is delivered."""
        pubsub = redis.pubsub()
        await pubsub.subscribe("energy-events")
        try:
            # First message is the subscribe ack; skip.
            msg = await pubsub.get_message(timeout=2.0)
            assert msg is not None, "side subscriber never received ack"
            # Now wait for the real tick.
            msg = await pubsub.get_message(timeout=2.0)
            assert msg is not None, "side subscriber never received tick"
            assert msg.get("type") == "message"
            side_event_received.set()
        finally:
            try:
                await pubsub.unsubscribe("energy-events")
                await pubsub.aclose()
            except Exception:
                pass

    async def publish_one_tick() -> None:
        """Wait for both subscribers to register, then publish."""
        # Tiny grace period so both subscriptions land.
        await asyncio.sleep(0.05)
        event = _make_event()
        payload = {"type": "tick", **event.model_dump(mode="json")}
        await redis.publish("energy-events", json.dumps(payload, ensure_ascii=False))

    capture_task = asyncio.create_task(subscribe_then_capture())
    consume_task = asyncio.create_task(processor.run())
    publish_task = asyncio.create_task(publish_one_tick())

    # Wait for the side subscriber to confirm the publish landed.
    await asyncio.wait_for(side_event_received.wait(), timeout=3.0)
    # Give the processor a moment to consume + persist.
    await asyncio.sleep(0.3)

    # Trigger graceful shutdown.
    processor._stop.set()

    await asyncio.gather(capture_task, consume_task, publish_task, return_exceptions=True)

    # Metrics hashes populated.
    m_renew = await redis.hgetall("metrics:renewable")
    assert m_renew, "metrics:renewable should be populated"
    assert "value" in m_renew
    assert m_renew["zone_id"] in ("SIN", "ANT")  # zone_id from the Event

    m_balance = await redis.hgetall("metrics:balance")
    assert m_balance, "metrics:balance should be populated"

    m_variation = await redis.hgetall("metrics:demand:variation")
    assert m_variation, "metrics:demand:variation should be populated"
    # First-ever tick → M3 = None → stored as "NaN".
    assert m_variation["value"] == "NaN"

    # ZSet history has at least one member.
    history = await redis.zrange("metrics:demand:history", 0, -1, withscores=True)
    assert len(history) >= 1


# ---------------------------------------------------------------------------
# BEHAVIOR — test_002 (alert roundtrip via fakeredis)
# ---------------------------------------------------------------------------


async def test_002_processor_alert_roundtrip_publishes_to_streams_and_counters(
    fakeredis_async_client,
) -> None:
    """A tick that crosses DEMAND_GENERATION_GAP (with debounce pre-bumped to 1)
    must publish one alert and write to all 5 alert sinks:
      1. XADD alerts:stream    (audit log)
      2. INCR alerts:total     (lifetime counter)
      3. LPUSH+LTRIM alerts:recent (capped recent list)
      4. INCR alerts:active:DEMAND_GENERATION_GAP (per-code active counter)

    Strategy: drive `_handle_tick` directly (no consume loop) to keep the
    test focused on the alert pipeline, not on Pub/Sub ordering. We
    pre-set `_breaches[("DEMAND_GENERATION_GAP", "")] = 1` so the very first
    tick transitions the counter to 2 and triggers the `active` publish.
    """
    from subscriber.alerts import RULE_A1  # noqa: F401  (FAIL pre-impl)
    from subscriber.processor import EnergyProcessor  # noqa: F401  (FAIL pre-impl)

    redis = fakeredis_async_client
    processor = EnergyProcessor(redis)

    # Pre-set breach counter to skip debounce cycle 1.
    processor.alert_engine._breaches[("DEMAND_GENERATION_GAP", "")] = 1

    # Build an event with gap = 2000 - 1000 = 1000 > threshold (800).
    event = Event(
        entity_id="ANT",
        timestamp=datetime(2026, 9, 20, 12, 0, 0, tzinfo=UTC),
        location=Location(latitude=6.0, longitude=-75.0),
        data=EventData(
            demanda_mw=2000.0,
            generacion_mw=1000.0,
            generacion_solar_mw=100.0,
            generacion_eolica_mw=50.0,
            generacion_hidraulica_mw=200.0,
            generacion_termica_mw=650.0,
            fuente=DataSource.SIM,
        ),
    )
    await processor._handle_tick(event)  # type: ignore[attr-defined]

    # 1. alerts:stream has 1 entry.
    stream_entries = await redis.xrange("alerts:stream", "-", "+")
    assert len(stream_entries) == 1, (
        f"expected 1 stream entry, got {len(stream_entries)}"
    )

    # 2. alerts:total counter = 1 (decode_responses=True → str).
    total = await redis.get("alerts:total")
    assert total == "1", f"expected alerts:total=1, got {total!r}"

    # 3. alerts:recent list has 1 entry (LTRIM keeps cap 20).
    recent = await redis.lrange("alerts:recent", 0, -1)
    assert len(recent) == 1, f"expected 1 recent entry, got {len(recent)}"

    # 4. alerts:active:DEMAND_GENERATION_GAP counter = 1.
    active = await redis.get("alerts:active:DEMAND_GENERATION_GAP")
    assert active == "1", f"expected active counter=1, got {active!r}"

    # Sanity: the breach counter advanced from 1 to 2.
    assert processor.alert_engine._breaches[("DEMAND_GENERATION_GAP", "")] == 2


# ---------------------------------------------------------------------------
# BEHAVIOR — test_003 (housekeeping ZSet prune)
# ---------------------------------------------------------------------------


async def test_003_processor_housekeeping_prunes_old_history(
    fakeredis_async_client,
) -> None:
    """`_housekeep()` removes ZSet entries whose score (unix_ts) is older than
    `DEMAND_HISTORY_WINDOW_SECONDS` (3600s).

    Strategy: seed the ZSet with two entries (one fresh, one stale) and
    call `_housekeep()` directly. Only the fresh entry should survive.
    """
    from subscriber.processor import EnergyProcessor  # noqa: F401  (FAIL pre-impl)

    redis = fakeredis_async_client
    processor = EnergyProcessor(redis)

    now = time.time()
    await redis.zadd("metrics:demand:history", {"1000.0": now - 100})    # fresh
    await redis.zadd("metrics:demand:history", {"800.0": now - 7200})    # old

    await processor._housekeep()  # type: ignore[attr-defined]

    members = await redis.zrange("metrics:demand:history", 0, -1, withscores=True)
    assert len(members) == 1, (
        f"expected 1 entry (the fresh one), got {len(members)}"
    )
    only_member = members[0]
    # fakeredis with decode_responses=True returns the member as str;
    # convert to float for the numeric compare.
    member_str = only_member[0]
    if isinstance(member_str, (bytes, bytearray)):
        member_str = member_str.decode()
    assert float(member_str) == 1000.0


# ---------------------------------------------------------------------------
# BEHAVIOR — test_004 (SIGTERM-equivalent shutdown)
# ---------------------------------------------------------------------------


async def test_004_processor_shutdown_stops_cleanly(
    fakeredis_async_client,
) -> None:
    """Setting `_stop` must break the consume loop within a bounded window
    (≤3s under fakeredis; in practice well under 1s because
    `pubsub.get_message(timeout=1.0)` is the only blocker). The pubsub is
    closed in `finally:` and the fixture's redis is closed by the fixture.
    """
    from subscriber.processor import EnergyProcessor  # noqa: F401  (FAIL pre-impl)

    redis = fakeredis_async_client
    processor = EnergyProcessor(redis)

    # Start the consume loop.
    consume_task = asyncio.create_task(processor.run())

    # Give the loop a moment to subscribe before signalling shutdown.
    await asyncio.sleep(0.2)

    # SIGTERM-equivalent: PR-B2's `subscriber/main.py` does exactly this
    # inside the signal handler. We mirror it at the test boundary.
    processor._stop.set()

    # The loop must exit within ~1 poll cycle (≤1s) — we pad to 3s to
    # absorb event-loop scheduling jitter.
    try:
        await asyncio.wait_for(consume_task, timeout=3.0)
    except asyncio.TimeoutError:
        consume_task.cancel()
        pytest.fail("processor.run() did not exit within 3s after _stop.set()")
