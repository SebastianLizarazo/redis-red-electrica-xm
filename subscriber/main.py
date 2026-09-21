"""Async entrypoint for the subscriber.

Lifecycle:
  1. Connect to Redis async.
  2. Set `health:subscriber:uptime` (unix_ts) and `health:subscriber:started_at`
     (iso) on boot. Both are write-once-per-process: a SIGTERM-cycle relaunch
     refreshes them, so a dashboard can spot the actual start time of the
     current instance even after restarts.
  3. Register SIGTERM/SIGINT handlers wrapped in
     `contextlib.suppress(NotImplementedError)` for Windows (where
     `loop.add_signal_handler` raises NotImplementedError on win32 because
     Python's signal handlers don't exist on the Windows event loop the
     same way they do on POSIX). On Windows we rely on KeyboardInterrupt
     or process-level cancellation for shutdown.
  4. Instantiate `EnergyProcessor(redis)` and run its consume loop.
  5. On `_stop` signal: the processor's `run()` exits its while loop, closes
     the pubsub in `finally:`, and we close the Redis client here.

Architecture mirrors `publisher/main.py`:
  - `EnergyProcessor.run()` is the testable consume loop (no signals).
  - `main.py` is the thin lifecycle wrapper (signals + boot keys + shutdown).
  - Boot health keys are best-effort: a Redis failure here logs WARNING
    and continues (the subscriber can still process without them — they
    exist for the dashboard, not for correctness).

Uso:
    python -m subscriber.main
"""
from __future__ import annotations

import asyncio
import contextlib
import signal
import time
from datetime import UTC, datetime

from redis.asyncio import Redis

from common.config import settings
from common.logging_config import get_logger, setup_logging
from common.redis_keys import (
    KEY_HEALTH_SUBSCRIBER_STARTED_AT,
    KEY_HEALTH_SUBSCRIBER_UPTIME,
)
from subscriber.processor import EnergyProcessor

logger = get_logger("subscriber.main")


async def _boot_health(redis: Redis) -> None:
    """Write `health:subscriber:uptime` (unix_ts) and `started_at` (iso) on boot.

    Best-effort: a Redis hiccup here logs WARNING and lets the loop start
    anyway — these keys are observability, not correctness.
    """
    now_unix = time.time()
    now_iso = datetime.now(UTC).isoformat()
    try:
        await redis.set(KEY_HEALTH_SUBSCRIBER_UPTIME, str(int(now_unix)))
        await redis.set(KEY_HEALTH_SUBSCRIBER_STARTED_AT, now_iso)
    except Exception as exc:  # noqa: BLE001 - boot health is best-effort
        logger.warning(
            "could not write health:subscriber:* on boot: %s", exc
        )
        return
    logger.info(
        "subscriber booted at %s (uptime unix_ts=%d)",
        now_iso,
        int(now_unix),
    )


async def run() -> int:
    """Main entrypoint. Returns 0 on graceful shutdown."""
    setup_logging(level=settings.log_level, json_format=settings.log_json)

    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    await _boot_health(redis)

    processor = EnergyProcessor(redis)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # Windows no soporta add_signal_handler
            loop.add_signal_handler(sig, processor._stop.set)

    try:
        await processor.run()
    finally:
        try:
            await redis.aclose()
            logger.info("subscriber: redis connection closed cleanly")
        except Exception as exc:  # noqa: BLE001 - shutdown cleanup is best-effort
            logger.warning("redis close error: %s", exc)
    return 0


def main() -> None:
    """Sync entrypoint used by `python -m subscriber.main`."""
    exit_code = asyncio.run(run())
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
