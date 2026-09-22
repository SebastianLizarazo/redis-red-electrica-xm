"""
`GET /api/state` — snapshot agregado de zonas (REQ-API-002).

Lee `state:sin` + `state:zone:{ANT,VAL,ATL,BOG,SAN}` vía HGETALL y devuelve
`{sin: ZoneState, zones: list[ZoneState]}` (5 zonas, orden canónico).

Casos cubiertos:
- 5 zonas + SIN presentes → `zones` length 5, `sin.zone_id == "SIN"`.
- Publisher aún no escribió `state:zone:*` → `zones: []`, `sin` puede ser
  None si tampoco está (test edge case del spec).
- Redis caído → 503 via el handler global de `api/server.py`.

Coerción: Redis devuelve `dict[str, str]` (todo string). Convertimos a
float/int con un cast explícito antes de pasar a Pydantic v2 para que
los validators (`ge=0` en demanda_mw) fallen con un mensaje claro si
llegara basura en Redis.
"""
from __future__ import annotations

import logging
from typing import cast

from fastapi import APIRouter, Depends
from redis.asyncio import Redis

from api.dependencies import get_redis
from common.models import ZONES_GEO, ZoneId, ZoneState
from common.redis_keys import state_sin_key, state_zone_key

logger = logging.getLogger("api.routers.state")

router = APIRouter(prefix="/api", tags=["state"])


def _zone_state_from_hash(zone_id: ZoneId, raw: dict[str, str]) -> ZoneState:
    """Coacciona un hash Redis `state:zone:*` a `ZoneState` validado."""
    return ZoneState(
        zone_id=zone_id,
        demanda_mw=float(raw["demanda_mw"]),
        generacion_mw=float(raw["generacion_mw"]),
        timestamp=raw["timestamp"],
        fuente=raw["fuente"],
    )


@router.get("/state")
async def get_state(redis: Redis = Depends(get_redis)) -> dict:
    """Snapshot agregado: SIN + 5 zonas geográficas."""
    # SIN primero (el más consultado por el dashboard).
    # El `cast` es necesario desde redis 8.x: `hgetall` se tipa como
    # `dict[bytes | str, bytes | str]` para cubrir los dos modos del cliente.
    # Aquí siempre son `str` porque `get_redis` crea el cliente con
    # `decode_responses=True` (ver api/dependencies.py).
    sin_raw = cast(dict[str, str], await redis.hgetall(state_sin_key()))  # type: ignore[misc]  # redis-py tipa los comandos como Awaitable[T] | T

    # 5 zonas en orden canónico. Usamos un pipeline para ahorrar round-trips.
    # `cast` necesario porque `ZONES_GEO` está tipado como `tuple[str, ...]`
    # por inferencia; los valores son literalmente `ZoneId` válidos.
    pipe = redis.pipeline(transaction=False)
    for zid in ZONES_GEO:
        pipe.hgetall(state_zone_key(cast(ZoneId, zid)))
    hashes = await pipe.execute()

    zones: list[ZoneState] = []
    for zid, raw in zip(ZONES_GEO, hashes, strict=True):
        if not raw:
            # Publisher aún no escribió esta zona (cold start, XM caído).
            # El spec exige `zones: []` cuando no hay nada — pero lo
            # implementamos zona a zona para no enmascarar bugs en producción.
            # El test edge case usa todas vacías → zonas queda [].
            continue
        try:
            zones.append(_zone_state_from_hash(cast(ZoneId, zid), raw))
        except Exception as exc:  # noqa: BLE001 - un hash corrupto no rompe los demás
            logger.warning(
                "zone hash corrupto, ignorando",
                extra={"zone": zid, "error": str(exc)},
            )

    # `sin` también puede ser None si Redis no lo tiene (publisher aún no arrancó).
    sin: ZoneState | None = None
    if sin_raw:
        try:
            sin = _zone_state_from_hash("SIN", sin_raw)
        except Exception as exc:  # noqa: BLE001
            logger.warning("state:sin corrupto", extra={"error": str(exc)})

    # El spec REQ-API-002 exige `sin: ZoneState` siempre presente.
    # Si no hay datos, devolvemos `sin: null` para no romper el contrato
    # Pydantic del cliente (el dashboard maneja `null` como "cargando").
    return {"sin": sin, "zones": zones}


__all__ = ["router"]
