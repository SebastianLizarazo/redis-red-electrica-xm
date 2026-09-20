"""
Orquestador del publisher (T-PUB-005).

Bucle asíncrono: decide fuente → obtiene eventos → los escribe en Redis.

    XM real ──┐
              ├──► SourceSelector ──► normalizer ──┬──► PUBLISH energy-events
    Simulador ┘                                    ├──► XADD    energy:stream
                                                   └──► HSET    state:zone:*

Responsabilidad ÚNICA: capturar, normalizar y publicar. No calcula métricas
ni evalúa alertas — eso es del subscriber (Alejandro). Mantener esa frontera
es lo que permite que ambos componentes se desplieguen y fallen por separado.

Las tres escrituras cumplen roles distintos, y por eso están las tres:
  - **Pub/Sub** entrega en tiempo real a quien esté escuchando *ahora*.
  - **Stream** deja histórico acotado (MAXLEN ~1000) que sobrevive a un
    subscriber que se reinicia.
  - **Hash** guarda el último estado por zona, que es lo que `/api/state`
    necesita sin tener que recorrer el stream.

Uso:
    python -m publisher.main
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import signal
from datetime import UTC, datetime
from typing import Any

import redis.asyncio as aioredis

from common.config import settings
from common.logging_config import get_logger, setup_logging
from common.models import DataSource, Event
from common.redis_keys import (
    KEY_HEALTH_UPTIME,
    KEY_STATE_SIN,
    PUBSUB_CHANNEL_ENERGY,
    STATE_ZONE_TTL_SECONDS,
    STREAM_ENERGY,
    STREAM_ENERGY_MAXLEN,
    state_zone_key,
)
from publisher.simulator import SimulatorSource
from publisher.source_selector import SourceSelector
from publisher.xm_client import XMRealSource

logger = get_logger("publisher")


class Publisher:
    """Ciclo de vida del publisher: conectar, publicar en bucle, cerrar."""

    def __init__(self, *, redis_client: Any, selector: SourceSelector) -> None:
        self._redis = redis_client
        self._selector = selector
        self._stop = asyncio.Event()
        self._ciclos = 0
        self._eventos_publicados = 0

    async def run(self) -> None:
        """Bucle principal. Sale limpio cuando llega SIGINT/SIGTERM."""
        await self._redis.set(KEY_HEALTH_UPTIME, _ahora_iso())
        logger.info(
            "publisher arrancando",
            extra={
                "modo": self._selector.mode.value,
                "canal": PUBSUB_CHANNEL_ENERGY,
                "intervalo_s": self._selector.interval_seconds,
            },
        )

        while not self._stop.is_set():
            try:
                await self._ciclo()
            except Exception as exc:  # noqa: BLE001 - un ciclo malo no mata el proceso
                logger.exception("ciclo fallido, se reintenta", extra={"error": str(exc)})

            # El intervalo se relee cada vuelta: si el selector conmutó a
            # simulador, la cadencia baja de 300 s a 5 s sin reiniciar nada.
            intervalo = self._selector.interval_seconds
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=intervalo)

    async def _ciclo(self) -> None:
        eventos = await self._selector.fetch_events()

        conmutacion = self._selector.take_switch_notice()
        if conmutacion is not None:
            await self._publicar_source_switch(conmutacion)

        for evento in eventos:
            await self._publicar(evento)

        self._ciclos += 1
        sin = next((e for e in eventos if e.entity_id == "SIN"), None)
        if sin is not None:
            logger.info(
                "ciclo publicado",
                extra={
                    "ciclo": self._ciclos,
                    "demanda_sin_mw": sin.data.demanda_mw,
                    "generacion_sin_mw": sin.data.generacion_mw,
                    "fuente": sin.data.fuente.value,
                    "eventos": len(eventos),
                },
            )

    async def _publicar(self, evento: Event) -> None:
        """Escribe un evento en los tres destinos, en un solo round-trip."""
        payload = evento.model_dump(mode="json")
        mensaje = json.dumps({"type": "tick", **payload}, ensure_ascii=False)
        plano = _aplanar(evento)

        pipe = self._redis.pipeline(transaction=False)
        pipe.publish(PUBSUB_CHANNEL_ENERGY, mensaje)
        pipe.xadd(STREAM_ENERGY, plano, maxlen=STREAM_ENERGY_MAXLEN, approximate=True)

        clave_estado = (
            KEY_STATE_SIN if evento.entity_id == "SIN" else state_zone_key(evento.entity_id)
        )
        pipe.hset(clave_estado, mapping=plano)
        pipe.expire(clave_estado, STATE_ZONE_TTL_SECONDS)
        await pipe.execute()

        self._eventos_publicados += 1

    async def _publicar_source_switch(self, modo: DataSource) -> None:
        """Anuncia el cambio de fuente para que el dashboard mueva el banner."""
        mensaje = json.dumps(
            {
                "type": "source_switch",
                "mode": modo.value,
                "timestamp": _ahora_iso(),
                "consecutive_failures": self._selector.consecutive_failures,
            },
            ensure_ascii=False,
        )
        await self._redis.publish(PUBSUB_CHANNEL_ENERGY, mensaje)
        logger.warning("anunciada conmutación de fuente", extra={"modo": modo.value})

    def request_stop(self) -> None:
        self._stop.set()

    async def aclose(self) -> None:
        logger.info(
            "publisher detenido",
            extra={"ciclos": self._ciclos, "eventos": self._eventos_publicados},
        )
        await self._selector.aclose()
        await self._redis.aclose()


def _aplanar(evento: Event) -> dict[str, str]:
    """Aplana un evento a campos string.

    Streams y Hashes de Redis solo admiten valores planos: no hay JSON
    anidado. El subscriber reconstruye lo que necesite desde estas claves.
    """
    d = evento.data
    return {
        "entity_id": evento.entity_id,
        "timestamp": evento.timestamp.isoformat(),
        "latitude": str(evento.location.latitude),
        "longitude": str(evento.location.longitude),
        "demanda_mw": str(d.demanda_mw),
        "generacion_mw": str(d.generacion_mw),
        "generacion_solar_mw": str(d.generacion_solar_mw),
        "generacion_eolica_mw": str(d.generacion_eolica_mw),
        "generacion_hidraulica_mw": str(d.generacion_hidraulica_mw),
        "generacion_termica_mw": str(d.generacion_termica_mw),
        "fuente": d.fuente.value,
    }


def _ahora_iso() -> str:
    return datetime.now(UTC).isoformat()


async def _build_publisher() -> Publisher:
    redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
    await redis_client.ping()
    logger.info("conectado a Redis", extra={"url": settings.redis_url})

    selector = SourceSelector(
        real=XMRealSource(),
        simulator=SimulatorSource(redis=redis_client),
        redis=redis_client,
    )
    return Publisher(redis_client=redis_client, selector=selector)


async def main() -> None:
    setup_logging(level=settings.log_level, json_format=settings.log_json)

    try:
        publisher = await _build_publisher()
    except Exception as exc:  # noqa: BLE001 - sin Redis no hay nada que hacer
        logger.error(
            "no se pudo conectar a Redis; ¿corriste `make up`?",
            extra={"url": settings.redis_url, "error": str(exc)},
        )
        raise SystemExit(1) from exc

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # Windows no soporta add_signal_handler
            loop.add_signal_handler(sig, publisher.request_stop)

    try:
        await publisher.run()
    finally:
        await publisher.aclose()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
