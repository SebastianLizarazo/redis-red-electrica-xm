"""
FastAPI app factory + lifecycle.

Diseño (spec #510, design #511):
- App factory `create_app()` para que los tests puedan crear instancias
  frescas con `dependency_overrides` (un app por test = estado limpio).
- Lifespan: abre `redis.asyncio.Redis.from_url(settings.redis_url, decode_responses=True)`
  en startup, ping para fail-fast si Redis no responde, y `aclose` en shutdown.
- `CORSMiddleware` lee `settings.cors_origins` (env `CORS_ORIGINS`).
- `@app.exception_handler(RedisError)` → `503` + `Retry-After: 5`. Cubre
  cualquier router cuya lectura Redis falle (REQ-API-008).
- Routers incluidos: `state`, `health` (PR-A), `metrics`, `alerts`, `stress`
  (PR-B), y `stream` (PR-C, SSE pubsub per-conn + heartbeat 15s).
  Dejamos los include_router para los routers ya existentes.

Por qué un factory y no un módulo-nivel `app = FastAPI()`:
- El módulo-nivel se evalúa una sola vez al import; los `dependency_overrides`
  se quedan para siempre. Con factory, cada test obtiene un app limpio.
- Es el idiom FastAPI 0.115+ para apps testables.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from redis.exceptions import RedisError

from api.routers import alerts, health, metrics, state, stream, stress
from common.config import settings
from common.logging_config import get_logger

logger = get_logger("api.server")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """
    Abre el cliente Redis async en startup y lo cierra en shutdown.

    Ping en startup: si Redis está caído al boot, fallamos rápido en vez
    de servir 503s para cada request. En producción, el orquestador
    (docker compose) reinicia el contenedor y el loop sigue.
    """
    redis_client = aioredis.Redis.from_url(
        settings.redis_url,
        decode_responses=True,
    )
    try:
        await redis_client.ping()
        logger.info(
            "api: redis conectado",
            extra={"redis_url": settings.redis_url},
        )
    except RedisError as exc:
        # Si el ping falla, logueamos y dejamos que `app.state.redis` quede
        # asignado igual. Los handlers de RedisError devolverán 503 hasta
        # que Redis vuelva. Esto es preferible a matar el proceso en boot:
        # un dashboard puede renderizar aunque el backend esté degraded.
        logger.warning(
            "api: redis ping falló en startup, sigo en modo degradado",
            extra={"error": str(exc)},
        )

    app.state.redis = redis_client
    try:
        yield
    finally:
        try:
            await redis_client.aclose()
            logger.info("api: redis cerrado limpiamente")
        except Exception as exc:  # noqa: BLE001 - shutdown cleanup es best-effort
            logger.warning("api: error cerrando redis: %s", exc)


def create_app() -> FastAPI:
    """
    Construye y devuelve una `FastAPI` lista para `uvicorn api.server:create_app()`
    o para `ASGITransport(app=create_app())` en tests.

    Factory (no módulo-nivel) para que los tests tengan un app fresco
    y limpio de `dependency_overrides`.
    """
    app = FastAPI(
        title="Redis Red Eléctrica XM API",
        version="0.1.0",
        lifespan=lifespan,
    )

    # CORS: el dashboard (Vite dev + GH Pages) consume esta API. Si el
    # origin no está en `cors_origins`, el browser bloquea la respuesta
    # aunque el handler devuelva 200.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,  # API pública sin cookies/auth
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )

    # Handler centralizado de RedisError → 503 + Retry-After.
    # Cubre todos los routers: cualquier `await redis.X(...)` que levante
    # RedisError sale como 503 en lugar de propagar 500.
    @app.exception_handler(RedisError)
    async def _redis_down_handler(_request, exc: RedisError) -> JSONResponse:
        logger.warning("api: redis no disponible", extra={"error": str(exc)})
        return JSONResponse(
            status_code=503,
            content={"detail": "redis unavailable", "error": str(exc)},
            headers={"Retry-After": "5"},
        )

    # Routers (PR-A + PR-B + PR-C).
    # - state/health: read-only, GET (PR-A)
    # - metrics/alerts: read-only, GET (PR-B)
    # - stress: WRITE-only, POST (PR-B) — único endpoint de escritura del API
    # - stream: SSE, GET (PR-C) — pubsub per-conn + heartbeat 15s (REQ-API-007)
    app.include_router(state.router)
    app.include_router(health.router)
    app.include_router(metrics.router)
    app.include_router(alerts.router)
    app.include_router(stress.router)
    app.include_router(stream.router)

    return app


# Módulo-nivel para `uvicorn api.server:app` (convenience CLI). Los tests
# deben usar `create_app()` en su lugar.
app = create_app()


__all__ = ["app", "create_app", "lifespan"]
