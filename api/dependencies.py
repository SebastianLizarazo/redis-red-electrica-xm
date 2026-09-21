"""
Dependencias de FastAPI (inyección de Redis).

Convención: la app pone un cliente `redis.asyncio.Redis` en `app.state.redis`
durante el lifespan (ver `api/server.py`). Esta dependencia lo expone a los
routers sin acoplarlos al cliente concreto — los tests sustituyen `get_redis`
vía `app.dependency_overrides` para inyectar `fakeredis`.

Por qué una función tan fina:
- Es el idiom FastAPI 0.115+: `request.app.state.redis` es la fuente canónica
  para dependencias cross-cutting que necesitan el lifespan-bound state.
- Mantener una sola dependencia de cliente simplifica el mock en tests: una
  sola entrada en `dependency_overrides` cubre los N routers.
"""
from __future__ import annotations

from fastapi import Request
from redis.asyncio import Redis


async def get_redis(request: Request) -> Redis:
    """Devuelve el cliente Redis async abierto en lifespan.

    Lanzará `AttributeError` si el lifespan no se ejecutó antes (tests que
    instancian la app sin `ASGITransport` o sin `async with`). En producción
    esto es imposible porque `uvicorn` siempre corre el lifespan antes del
    primer request.
    """
    return request.app.state.redis  # type: ignore[no-any-return]


__all__ = ["get_redis"]
