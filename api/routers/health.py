"""
`GET /api/health` — estado agregado del pipeline (REQ-API-005, REQ-API-008).

Lee los strings de salud persistidos por el publisher (`health:*`) y el
subscriber (`health:subscriber:*`) en un solo MGET. Computa `redis_ok` con
un `await redis.ping()` inline. Deriva `uptime_seconds` parseando el ISO
timestamp de `health:subscriber:started_at` (si está) o devolviendo 0.

Casos cubiertos:
- Healthy completo: `mode`, `failures`, `source_switches`, `uptime>0`, `redis_ok=True`.
- Subscriber nunca arrancó: `started_at` ausente → `uptime_seconds=0`.
- Redis caído: el `ping()` levanta RedisError → handler global responde
  503 + `Retry-After: 5` (REQ-API-008). No devolvemos data stale.
"""
from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from redis.asyncio import Redis

from api.dependencies import get_redis
from common.models import DataSource, HealthStatus
from common.redis_keys import (
    KEY_HEALTH_FAILURES,
    KEY_HEALTH_MODE,
    KEY_HEALTH_NEXT_RETRY_AT,
    KEY_HEALTH_SOURCE_SWITCHES,
    KEY_HEALTH_SUBSCRIBER_STARTED_AT,
    KEY_HEALTH_XM_LAST_FAILURE,
    KEY_HEALTH_XM_LAST_SUCCESS,
)

router = APIRouter(prefix="/api", tags=["health"])


# KEYS que componen el MGET. Las claves que falten devuelven None (no
# rompemos el contrato si el publisher aún no arrancó).
_HEALTH_KEYS: tuple[str, ...] = (
    KEY_HEALTH_MODE,
    KEY_HEALTH_FAILURES,
    KEY_HEALTH_SOURCE_SWITCHES,
    KEY_HEALTH_XM_LAST_SUCCESS,
    KEY_HEALTH_XM_LAST_FAILURE,
    KEY_HEALTH_NEXT_RETRY_AT,
)


def _parse_iso_aware(raw: str | None) -> datetime | None:
    """Parsea un ISO 8601 con tzinfo. None/absent → None. Parse fail → None
    (defensivo: un timestamp corrupto en Redis no debe tumbar `/api/health`)."""
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    # `fromisoformat` acepta naive en Python 3.11+; forzamos aware si falta tz.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


@router.get("/health")
async def get_health(redis: Redis = Depends(get_redis)) -> HealthStatus:
    """Estado agregado del pipeline + redis_ok inline."""
    # MGET pack de las claves publisher-side. El subscriber-side
    # (`started_at`) se lee aparte porque la política de errores es
    # diferente: missing → uptime=0, pero missing en `mode` → fallback
    # a `simulator` (no bloqueamos el endpoint si publisher aún no arrancó).
    values = await redis.mget(_HEALTH_KEYS)
    mode_raw, failures_raw, switches_raw, last_success_raw, last_failure_raw, next_retry_raw = values

    # `mode` siempre presente (default: simulator si publisher aún no escribió).
    try:
        mode = DataSource(mode_raw) if mode_raw else DataSource.SIM
    except ValueError:
        # Modo desconocido en Redis (forward-compat): caemos a SIM.
        mode = DataSource.SIM

    failures = int(failures_raw) if failures_raw and failures_raw.isdigit() else 0
    switches = int(switches_raw) if switches_raw and switches_raw.isdigit() else 0

    # Uptime del subscriber (PR-B2): `health:subscriber:started_at` se setea
    # en el boot del subscriber (subscriber/main.py:61). Si no está
    # (subscriber nunca arrancó, o el publish falló best-effort), uptime=0.
    started_raw = await redis.get(KEY_HEALTH_SUBSCRIBER_STARTED_AT)
    started_at = _parse_iso_aware(started_raw)
    if started_at is None:
        uptime_seconds = 0
    else:
        uptime_seconds = max(0, int((datetime.now(UTC) - started_at).total_seconds()))

    # Inline ping: si Redis está caído, RedisError sube al handler global
    # que responde 503 + Retry-After (REQ-API-008). Si todo OK, redis_ok=True.
    await redis.ping()

    return HealthStatus(
        mode=mode,
        last_xm_success=_parse_iso_aware(last_success_raw),
        failures=failures,
        source_switches=switches,
        last_failure=_parse_iso_aware(last_failure_raw),
        next_retry_at=_parse_iso_aware(next_retry_raw),
        redis_ok=True,
        uptime_seconds=uptime_seconds,
    )


__all__ = ["router"]
