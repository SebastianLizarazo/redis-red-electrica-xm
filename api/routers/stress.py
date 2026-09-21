"""`POST /api/stress/{event}` — inyecta escenarios de stress en Redis (REQ-API-006).

Endpoint WRITE-only del API (los otros 5 son read-only). Valida `event`
contra `publisher.simulator.STRESS_EVENTS` (la lista canónica del taller)
y, si es válido, escribe el flag `stress:{event} = 1` con TTL
`KEY_STRESS_TTL_SECONDS` (30s por default en common/redis_keys.py).

El simulador del publisher (`publisher/simulator.py:_leer_stress`) lee
estos flags en cada ciclo y los aplica como factores de stress
(demand_surge, hydro_drop, critical_deficit, recovery). El TTL corto
acota el blast radius si un operador olvida limpiar — 30s es suficiente
para una demo y suficiente para que un ataque/abuso se autoexpire.

Respuestas:
- 204 No Content: evento válido, flag seteado con TTL > 0.
- 400 Bad Request: evento desconocido. El detail espeja el `ValueError`
  literal del publisher para que la API tenga una única fuente de verdad
  del mensaje de error: `f"escenario desconocido: {event!r}; use uno de {STRESS_EVENTS}"`.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Path, Response
from redis.asyncio import Redis

from api.dependencies import get_redis
from common.redis_keys import KEY_STRESS_TTL_SECONDS, stress_key
from publisher.simulator import STRESS_EVENTS

router = APIRouter(prefix="/api", tags=["stress"])


@router.post("/stress/{event}", status_code=204)
async def post_stress(
    event: str = Path(..., description="Nombre del escenario de stress"),
    redis: Redis = Depends(get_redis),
) -> Response:
    """Setea el flag `stress:{event}` con TTL corto. Devuelve 204 o 400."""
    if event not in STRESS_EVENTS:
        # Mismo texto que `publisher.simulator.SimulatorSource.inject_stress`
        # (consumido en tests/demo) para que el operador vea el mismo
        # mensaje tanto si dispara desde el dashboard como si usa la CLI.
        raise HTTPException(
            status_code=400,
            detail=f"escenario desconocido: {event!r}; use uno de {STRESS_EVENTS}",
        )

    await redis.set(stress_key(event), 1, ex=KEY_STRESS_TTL_SECONDS)
    # 204 No Content: el caller confirma recepción; el flag ya está en Redis.
    return Response(status_code=204)


__all__ = ["router"]
