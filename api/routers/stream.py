"""`GET /api/stream` — SSE (Server-Sent Events) para entrega en tiempo real.

Implementa REQ-API-007 (spec #510) + decisión 3 de design #511.

Diseño de lifecycle (per-conexión, no global):

    1. Por cada request, abrimos UN pubsub dedicado (`redis.pubsub()`).
    2. Lo suscribimos a `PUBSUB_CHANNEL_ENERGY` ("energy-events").
    3. Loop: poll acotado (`get_message(timeout=1.0)`) + heartbeat 15s.
    4. Yield de eventos SSE en el formato wire-compatible con publisher
       (`_aplanar`) y subscriber (`publish_alert`): el `data:` es el JSON
       literal publicado, sin re-codificar. El `event:` es el discriminador
       `type` del payload (`tick` | `alert` | `source_switch`).
    5. Heartbeat como comentario SSE (`: heartbeat`) — no es un evento,
       solo mantiene viva la conexión contra proxies intermedios.
    6. `try/finally: unsubscribe + aclose` SIEMPRE, incluso en disconnect
       abrupto. Esto cierra el R1/R5 del risk matrix: un cliente lento
       o que desconecta no debe dejar un pubsub abierto en Redis.

Por qué pubsub per-connection (no compartido):
- Aísla clientes lentos: un suscriptor que no consume no bloquea a otros.
- El Redis async pubsub model es fan-out, no work-queue: cada suscriptor
  recibe su propia copia. Compartir un pubsub sería anti-pattern.
- Cada connection crea y destruye su propio `PubSub` instance, así que
  el cleanup es trivial (un solo `aclose()` por request).

Tradeoff conocido: en alta concurrencia (cientos de SSE clients
simultáneos) el pool de Redis puede saturarse. Aceptable para el
alcance académico del taller — el dashboard suele tener 1-3 conexiones
abiertas a la vez.

Discriminadores del wire format:
- `event: tick` — publisher `_aplanar()` (publisher/main.py:206).
- `event: alert` — subscriber `publish_alert()` (subscriber/alerts.py:208).
- `event: source_switch` — publisher `_publicar_source_switch()`
  (publisher/main.py:180).
- `: heartbeat` — comentario SSE (no es un evento, es keep-alive).

Casos cubiertos:
- Cliente recibe `tick`/`alert`/`source_switch` en <1s tras publish.
- Heartbeat cada 15s en conexiones idle (mantiene vivos los proxies).
- Client disconnect → pubsub cerrado (R1/R5); sin tareas huérfanas.
- Redis caído → exception handler global → 503 con `Retry-After: 5`.

Limitaciones explícitas (out-of-scope):
- NO hay replay histórico. Solo entrega en tiempo real (REQ-API-007
  "MUST NOT replay eventos pasados").
- NO hay filtrado por tipo de evento. El cliente puede decidir ignorar
  ciertos `event:`.
- NO hay autenticación. El endpoint es público; cualquier cliente que
  pueda llegar a la URL puede consumir.
"""
from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, Request
from redis.asyncio import Redis
from sse_starlette.event import ServerSentEvent
from sse_starlette.sse import EventSourceResponse

from api.dependencies import get_redis
from common.logging_config import get_logger
from common.redis_keys import PUBSUB_CHANNEL_ENERGY

logger = get_logger("api.routers.stream")

router = APIRouter(prefix="/api", tags=["stream"])


# Heartbeat cada 15s (REQ-API-007). Constante para que un monkey-patch
# desde tests pueda acortarlo si se necesita verificar el path sin esperar.
HEARTBEAT_INTERVAL_SECONDS = 15.0

# Timeout del poll acotado: 1s es el balance entre responsiveness (un
# mensaje llega al cliente en <=1s) y CPU waste (no estamos spinning
# busy en get_message).
POLL_TIMEOUT_SECONDS = 1.0


async def _event_generator(
    redis: Redis,
    request: Request,
) -> AsyncIterator[dict[str, Any] | ServerSentEvent]:
    """Generador SSE: 1 pubsub por conexión, heartbeat 15s, cleanup garantizada.

    Yields `dict` para eventos con datos (`event:` + `data:`) y
    `ServerSentEvent` para heartbeats (comentarios `:`). sse-starlette
    serializa ambos al formato `text/event-stream` correcto.

    El `request` se usa SOLO para chequear `is_disconnected()` entre
    iteraciones: si el cliente cerró, salimos del loop sin esperar al
    próximo poll timeout (que sería hasta 1s de delay).
    """
    pubsub = redis.pubsub()
    try:
        await pubsub.subscribe(PUBSUB_CHANNEL_ENERGY)
        logger.debug(
            "sse: cliente suscrito",
            extra={"channel": PUBSUB_CHANNEL_ENERGY, "client": request.client},
        )
    except Exception as exc:  # noqa: BLE001 - fallar rápido al subscribe
        # Si subscribe falla (Redis down, etc.), salimos sin entrar al
        # loop. El exception handler global convierte esto en 503.
        logger.warning(
            "sse: fallo al suscribir al pubsub",
            extra={"channel": PUBSUB_CHANNEL_ENERGY, "error": str(exc)},
        )
        await pubsub.aclose()  # type: ignore[attr-defined]
        raise

    last_heartbeat = time.monotonic()
    try:
        while True:
            # Heartbeat primero: independiente del tráfico. Si el cliente
            # está conectado y el proxy intermediario lleva >15s sin
            # actividad, mantenemos viva la conexión con un comentario.
            now = time.monotonic()
            if now - last_heartbeat >= HEARTBEAT_INTERVAL_SECONDS:
                yield ServerSentEvent(comment="heartbeat")
                last_heartbeat = now

            # Chequeo de disconnect: si el cliente cerró la conexión,
            # salimos del loop sin esperar al poll timeout. Esto
            # garantiza que `aclose()` corra rápido en lugar de esperar
            # hasta POLL_TIMEOUT_SECONDS.
            if await request.is_disconnected():
                logger.debug("sse: cliente desconectado, cerrando generador")
                break

            # Poll acotado. Devuelve None si no hay mensaje (timeout
            # = 1s) o un dict {type, pattern, channel, data} si hay.
            # `ignore_subscribe_messages=True` filtra el ack del
            # subscribe para que no aparezca como `event: subscribe`
            # en el cliente.
            msg = await pubsub.get_message(
                timeout=POLL_TIMEOUT_SECONDS,
                ignore_subscribe_messages=True,
            )
            if msg is None:
                continue

            # Despachar por discriminador `type` del payload JSON.
            # El `data` siempre es string JSON (con `decode_responses=True`);
            # pasamos el raw al cliente sin re-codificar para preservar
            # el contrato con el publisher (`_aplanar`) y el subscriber
            # (`publish_alert`).
            data_str = msg.get("data")
            if not isinstance(data_str, str):
                # Algo inesperado (bytes sin decode, None, etc.). No
                # es un payload válido — logueamos y seguimos.
                logger.warning(
                    "sse: payload no es string, omitiendo",
                    extra={"type_payload": type(data_str).__name__},
                )
                continue

            try:
                payload = json.loads(data_str)
            except json.JSONDecodeError:
                logger.warning(
                    "sse: payload no es JSON válido, omitiendo",
                    extra={"data_preview": data_str[:80]},
                )
                continue

            event_type = payload.get("type")
            if event_type not in ("tick", "alert", "source_switch"):
                # Discriminador desconocido: lo logueamos pero no lo
                # emitimos. Toleramos tipos nuevos sin romper clientes.
                logger.debug(
                    "sse: tipo de evento no emitido",
                    extra={"event_type": event_type, "channel": msg.get("channel")},
                )
                continue

            # Yield del evento. `data` es el JSON literal original.
            yield {"event": event_type, "data": data_str}
    finally:
        # Cleanup GARANTIZADO (decisión 3 de design #511): sin este
        # try/finally, un disconnect abrupto deja el pubsub abierto
        # en el cliente Redis (R1/R5 del risk matrix).
        try:
            await pubsub.unsubscribe(PUBSUB_CHANNEL_ENERGY)
        except Exception as exc:  # noqa: BLE001 - best-effort cleanup
            logger.warning(
                "sse: error al desuscribir",
                extra={"channel": PUBSUB_CHANNEL_ENERGY, "error": str(exc)},
            )
        try:
            await pubsub.aclose()  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 - best-effort cleanup
            logger.warning(
                "sse: error al cerrar pubsub",
                extra={"error": str(exc)},
            )
        logger.debug("sse: pubsub cerrado limpiamente")


@router.get("/stream")
async def stream(
    request: Request,
    redis: Redis = Depends(get_redis),
) -> EventSourceResponse:
    """`GET /api/stream` — text/event-stream de `tick`/`alert`/`source_switch`.

    Lifetime: el cliente HTTP mantiene la conexión abierta. El servidor
    emite eventos SSE a medida que llegan al pubsub. Cierra cuando:
    - El cliente desconecta (cancela el request).
    - uvicorn hace shutdown.
    - Algún error fatal (handler global → 503).

    Ver `HEARTBEAT_INTERVAL_SECONDS` y `_event_generator` para los detalles
    de implementación.
    """
    return EventSourceResponse(_event_generator(redis, request))


__all__ = [
    "router",
    "HEARTBEAT_INTERVAL_SECONDS",
    "POLL_TIMEOUT_SECONDS",
    "_event_generator",
]
