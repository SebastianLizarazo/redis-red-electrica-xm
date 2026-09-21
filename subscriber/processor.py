"""Subscriber consume loop + tick handler with per-event failure isolation (R4-001).

Architecture mirrors `publisher/main.py`:
  - Engine (`EnergyProcessor`) is testable without signals or lifespan.
  - `run()` is the consume loop; `_handle_tick` parses + dispatches.
  - `_handle_raw` wraps the dispatch in try/except (R4-001): one bad message
    logs + counter + `continue`. The Pub/Sub subscription NEVER aborts.
  - `_housekeep` runs in-loop every 60s (no `asyncio.create_task` per Q-5
    resolution) and prunes `metrics:demand:history` to a 60-min window.

Public surface:
  - `EnergyProcessor(redis)`
  - `await processor.run()` — main consume loop (blocks until `_stop` set).
  - `processor._stop` — module-private `asyncio.Event()` for graceful shutdown
    (PR-B2 will set this from a signal handler in `subscriber/main.py`).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from redis.asyncio import Redis

from common.models import DataSource, Event
from common.redis_keys import (
    DEMAND_HISTORY_WINDOW_SECONDS,
    KEY_DEMAND_HISTORY,
    PUBSUB_CHANNEL_ENERGY,
)
from subscriber.alerts import AlertEngine, publish_alert
from subscriber.metrics import compute_metrics, persist_metrics

logger = logging.getLogger("subscriber.processor")

# Housekeep interval (in-loop, Q-5 resolution).
HOUSEKEEP_INTERVAL_SECONDS = 60.0

# Threshold for the reconnect-degrade counter; >=3 within window → ERROR log.
FAILURE_DEGRADE_THRESHOLD = 3

# `get_message` poll interval — keeps the loop ticking for shutdown/housekeep.
POLL_TIMEOUT_SECONDS = 1.0


class EnergyProcessor:
    """Async consume loop for `PUBSUB_CHANNEL_ENERGY`.

    Uses bounded polling (`pubsub.get_message(timeout=1.0)`) so the same
    task that polls for messages can run housekeeping every 60s WITHOUT
    spawning a background task (avoiding the shutdown race that
    `asyncio.create_task` creates on non-daemon background tasks).

    Failure isolation (R4-001):
      - `_handle_raw` wraps dispatch in try/except. A `RuntimeError`,
        `ValidationError`, `RedisError`, or any other exception is
        captured, logged with WARNING + traceback, and the counter
        `self._failures` is incremented. The loop continues.
      - After `FAILURE_DEGRADE_THRESHOLD` consecutive failures, the loop
        logs an ERROR but still does NOT abort (degrade-don't-die pattern
        inherited from publisher PR-A).
    """

    def __init__(self, redis: Redis) -> None:
        self.redis = redis
        self.alert_engine = AlertEngine()
        self._stop = asyncio.Event()
        self._last_housekeep = time.monotonic()
        self._failures = 0
        self._last_demand: float | None = None

    async def run(self) -> None:
        """Consume loop with degrade-reconnect + per-event isolation + housekeeping.

        Lifecycle:
          1. Subscribe to PUBSUB_CHANNEL_ENERGY.
          2. Loop until `self._stop.is_set()`:
             a. Housekeep every 60s (in-loop timestamp accumulator).
             b. Poll `pubsub.get_message(timeout=POLL_TIMEOUT_SECONDS)`.
             c. On `None` or non-"message" → continue (idle / ack).
             d. Dispatch to `_handle_raw` (which wraps in try/except).
          3. `finally:` unsubscribe + aclose (best-effort).
        """
        pubsub = self.redis.pubsub()
        await pubsub.subscribe(PUBSUB_CHANNEL_ENERGY)
        logger.info("subscriber: subscribed to %s", PUBSUB_CHANNEL_ENERGY)
        try:
            while not self._stop.is_set():
                # Housekeep: every HOUSEKEEP_INTERVAL_SECONDS.
                if (
                    time.monotonic() - self._last_housekeep
                ) >= HOUSEKEEP_INTERVAL_SECONDS:
                    await self._housekeep()

                # Bounded poll — keeps the loop ticking for shutdown signals.
                try:
                    msg = await pubsub.get_message(timeout=POLL_TIMEOUT_SECONDS)
                except Exception as exc:
                    self._failures += 1
                    logger.warning(
                        "get_message failed (%d): %s", self._failures, exc
                    )
                    if self._failures >= FAILURE_DEGRADE_THRESHOLD:
                        logger.error(
                            "subscriber: %d consecutive get_message failures; staying degraded",
                            self._failures,
                        )
                    await asyncio.sleep(0.1)
                    continue

                if msg is None or msg.get("type") != "message":
                    continue
                await self._handle_raw(msg.get("data"))
        finally:
            try:
                await pubsub.unsubscribe(PUBSUB_CHANNEL_ENERGY)
                await pubsub.aclose()
            except Exception:
                # Best-effort: don't mask a real shutdown error.
                pass

    async def _handle_raw(self, raw: Any) -> None:
        """Parse the raw Pub/Sub payload and dispatch to the right handler.

        Wrapped in try/except per R4-001 — one bad message MUST NOT kill the
        loop. Three failure modes are explicitly distinguished:
          1. JSON parse error → `_failures += 1`, WARNING, `return`.
          2. Unknown / missing `type` → DEBUG log, no counter bump (benign).
          3. Handler raise (RuntimeError, ValidationError, etc.) → `_failures
             += 1`, WARNING + traceback, `return`. The next message is
             handled normally.
        """
        if isinstance(raw, (bytes, bytearray)):
            raw_text: Any = raw.decode("utf-8", errors="replace")
        else:
            raw_text = raw
        try:
            payload = json.loads(raw_text) if isinstance(raw_text, str) else raw_text
        except (TypeError, ValueError) as exc:
            self._failures += 1
            logger.warning(
                "malformed message (#%d): %s", self._failures, exc
            )
            return
        if not isinstance(payload, dict):
            self._failures += 1
            logger.warning(
                "non-dict payload (#%d): %r", self._failures, payload
            )
            return

        msg_type = payload.get("type")
        try:
            if msg_type == "tick":
                event = Event.model_validate(
                    {k: v for k, v in payload.items() if k != "type"}
                )
                await self._handle_tick(event)
            elif msg_type == "source_switch":
                mode = payload.get("mode", DataSource.SIM.value)
                try:
                    self.alert_engine.set_fuente(DataSource(mode))
                except ValueError:
                    logger.warning("unknown source_switch mode=%r", mode)
                    return
                logger.info("source_switch → %s", mode)
            else:
                # Unknown / unhandled type — quiet log, no counter.
                logger.debug("ignored message type=%s", msg_type)
        except Exception as exc:
            self._failures += 1
            logger.warning(
                "handler failure (#%d): %s",
                self._failures,
                exc,
                exc_info=True,
            )

    async def _handle_tick(self, event: Event) -> None:
        """Compute + persist metrics, then evaluate + publish alerts.

        All side effects happen in this order:
          1. compute_metrics (pure).
          2. persist_metrics (3 HSET writes).
          3. ZADD metrics:demand:history <unix_ts> <demanda_str>.
          4. alert_engine.evaluate (pure, stateful).
          5. For each NEW alert, publish_alert (5-key write).
        """
        metrics = compute_metrics(event, previous_demand_mw=self._last_demand)
        await persist_metrics(self.redis, metrics)
        # ZSet history AFTER the HSET (next tick reads from it via ZREVRANGE).
        await self.redis.zadd(
            KEY_DEMAND_HISTORY,
            {str(event.data.demanda_mw): event.timestamp.timestamp()},
        )
        self._last_demand = event.data.demanda_mw

        alerts = self.alert_engine.evaluate(event, metrics)
        for alert in alerts:
            await publish_alert(self.redis, alert)

    async def _housekeep(self) -> None:
        """Prune `metrics:demand:history` to the configured window.

        Failure inside housekeeping MUST NOT abort the loop — it's a
        best-effort cleanup. We bump `_failures` only if it's truly a
        Redis issue, but we always reset the timer in `finally` so the
        loop keeps making progress.
        """
        try:
            now = time.time()
            cutoff = now - DEMAND_HISTORY_WINDOW_SECONDS
            removed = await self.redis.zremrangebyscore(
                KEY_DEMAND_HISTORY, "-inf", cutoff
            )
            if removed:
                logger.info("housekeep: removed %d old history entries", removed)
        except Exception as exc:
            logger.warning("housekeep failure: %s", exc)
        finally:
            self._last_housekeep = time.monotonic()


__all__ = [
    "EnergyProcessor",
    "FAILURE_DEGRADE_THRESHOLD",
    "HOUSEKEEP_INTERVAL_SECONDS",
    "POLL_TIMEOUT_SECONDS",
]
