"""Integration tests for subscriber.processor.

`tests/integration/test_processor.py` exercises the `EnergyProcessor`
end-to-end against a `fakeredis_async_client`, using an in-process
publisher-stub (no real Redis required).

Convención Strict TDD:
- test_001 = SHAPE: `EnergyProcessor(redis).run()` consumes a single tick
  from `energy-events` and persists the 3 metric hashes + ZSet history.

Test ordering caveat (RISK-N3 from spec #493):
- fakeredis pub/sub is in-process. `await redis.publish(...)` to a channel
  with NO subscriber is a no-op. The test must `await pubsub.subscribe(...)`
  BEFORE the stub publishes, in the same event loop. We use
  `asyncio.gather(subscribe_then_capture, consume_loop)` to keep the order
  deterministic.

This file ONLY contains test_001 in PR-B1. Tests test_002..004 are
deferred to PR-B2 (they cover alerts roundtrip + housekeeping + SIGTERM
shutdown, which need `subscriber/main.py`).
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

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
    ts = ts or datetime(2026, 9, 20, 12, 0, 0, tzinfo=timezone.utc)
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
