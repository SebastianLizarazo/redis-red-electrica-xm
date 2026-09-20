"""
Selector de fuente con fallback automático (T-PUB-004).

Decide en cada ciclo si los datos salen de XM o del simulador, y mantiene el
estado de salud en Redis para que el dashboard pinte el banner de modo.

Máquina de estados
------------------
Arranca en modo REAL. Cada fallo de XM incrementa un contador; a los 3 fallos
consecutivos el sistema **conmuta** a simulador y programa un reintento con
backoff exponencial (5 → 10 → 20 → 30 min, con tope). Cuando el reintento
tiene éxito, vuelve a REAL y el contador se reinicia.

Dos conceptos que no hay que confundir
--------------------------------------
- **Modo comprometido** (`health:mode`): la fuente a la que el sistema está
  adherido. Solo cambia tras 3 fallos, no al primer tropiezo.
- **Dato servido en el ciclo**: si XM falla pero aún no llegamos a 3 fallos,
  el ciclo se sirve igual con el simulador para que el dashboard no se
  congele. Cada evento lleva su propio `EventData.fuente`, así que nunca se
  publica un dato simulado disfrazado de real.

Esa distinción es la que permite el tercer estado del banner (T-DASH-002):
`mode=real` con `health:failures > 0` es **DEGRADADO**, distinto de un
`mode=simulator` estable.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

from common.config import settings
from common.data_source import DataSourceError
from common.logging_config import get_logger
from common.models import DataSource, Event
from common.redis_keys import (
    DEFAULT_BACKOFF_BASE_SECONDS,
    DEFAULT_BACKOFF_CAP_SECONDS,
    KEY_HEALTH_FAILURES,
    KEY_HEALTH_MODE,
    KEY_HEALTH_NEXT_RETRY_AT,
    KEY_HEALTH_SOURCE_SWITCHES,
    KEY_HEALTH_XM_LAST_FAILURE,
    KEY_HEALTH_XM_LAST_SUCCESS,
)
from publisher.simulator import SimulatorSource
from publisher.xm_client import XMRealSource

logger = get_logger("publisher.selector")

# Fallos consecutivos de XM que disparan la conmutación al simulador.
MAX_CONSECUTIVE_FAILURES = 3


class SourceSelector:
    """Fachada `DataSource` que enruta entre XM real y simulador."""

    name: ClassVar[str] = "selector"

    def __init__(
        self,
        *,
        real: XMRealSource | None = None,
        simulator: SimulatorSource | None = None,
        redis: Any | None = None,
        force_source: str | None = None,
        max_failures: int = MAX_CONSECUTIVE_FAILURES,
        backoff_base_seconds: int = DEFAULT_BACKOFF_BASE_SECONDS,
        backoff_cap_seconds: int = DEFAULT_BACKOFF_CAP_SECONDS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._real = real if real is not None else XMRealSource()
        self._simulator = simulator if simulator is not None else SimulatorSource(redis=redis)
        self._redis = redis
        self._force = (force_source if force_source is not None else settings.force_source).strip()
        self._max_failures = max(1, max_failures)
        self._backoff_base = backoff_base_seconds
        self._backoff_cap = backoff_cap_seconds
        self._clock = clock or (lambda: datetime.now(UTC))

        self._mode: DataSource = DataSource.SIM if self._force == "simulator" else DataSource.REAL
        self._consecutive_failures = 0
        self._retry_attempts = 0
        self._next_retry_at: datetime | None = None
        self._switches = 0
        self._last_success: datetime | None = None
        self._last_failure: datetime | None = None
        # Conmutación pendiente de anunciar por Pub/Sub (`source_switch`).
        self._pending_switch: DataSource | None = None

    # --- Estado observable --------------------------------------------------

    @property
    def mode(self) -> DataSource:
        """Fuente a la que el sistema está comprometido ahora mismo."""
        return self._mode

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    @property
    def last_success(self) -> datetime | None:
        return self._last_success

    @property
    def interval_seconds(self) -> int:
        """Cadencia del loop según el modo.

        XM publica cada ~5 minutos: consultarlo cada 5 s solo repetiría el
        mismo valor y gastaría peticiones. El simulador sí genera datos
        nuevos en cada tick, así que va a 5 s para que la demo se vea viva.
        """
        if self._mode is DataSource.REAL:
            return settings.publisher_interval_seconds_real
        return settings.publisher_interval_seconds

    def take_switch_notice(self) -> DataSource | None:
        """Devuelve (una sola vez) el modo al que se acaba de conmutar.

        `main` la consulta para emitir el evento `source_switch` por Pub/Sub.
        """
        pendiente, self._pending_switch = self._pending_switch, None
        return pendiente

    # --- Protocol DataSource ------------------------------------------------

    async def fetch_event(self) -> Event | None:
        eventos = await self.fetch_events()
        return next((e for e in eventos if e.entity_id == "SIN"), None)

    async def fetch_events(self) -> list[Event]:
        """Obtiene los eventos del ciclo, aplicando la política de fallback."""
        if self._force == "simulator":
            # La salud se persiste igual: el banner del dashboard lee
            # `health:mode`, y el modo forzado es justo el de la demo sin
            # internet. Sin esto la clave nunca se escribiría.
            await self._persistir_salud()
            return await self._simulator.fetch_events()

        if self._force == "real":
            # Forzado significa forzado: si XM falla, el error sube. Sirve
            # para que CI detecte una API rota en vez de enmascararla.
            eventos = await self._real.fetch_events()
            await self._on_success()
            return eventos

        if self._mode is DataSource.REAL:
            return await self._ciclo_en_modo_real()
        return await self._ciclo_en_modo_simulador()

    async def health_check(self) -> bool:
        if self._mode is DataSource.REAL:
            return await self._real.health_check()
        return await self._simulator.health_check()

    # --- Ciclos por modo ----------------------------------------------------

    async def _ciclo_en_modo_real(self) -> list[Event]:
        try:
            eventos = await self._real.fetch_events()
        except DataSourceError as exc:
            await self._on_failure(exc)
            if self._consecutive_failures >= self._max_failures:
                await self._switch_to(DataSource.SIM)
            # Pase lo que pase, el ciclo entrega datos: el pipeline no se seca.
            return await self._simulator.fetch_events()
        else:
            await self._on_success()
            return eventos

    async def _ciclo_en_modo_simulador(self) -> list[Event]:
        if self._debe_reintentar():
            try:
                eventos = await self._real.fetch_events()
            except DataSourceError as exc:
                await self._on_failure(exc)
                self._programar_reintento()
                logger.info(
                    "XM sigue caída, continúa el simulador",
                    extra={"proximo_reintento": self._iso(self._next_retry_at)},
                )
            else:
                await self._on_success()
                await self._switch_to(DataSource.REAL)
                return eventos

        return await self._simulator.fetch_events()

    def _debe_reintentar(self) -> bool:
        if self._next_retry_at is None:
            return True
        return self._clock() >= self._next_retry_at

    # --- Transiciones y salud ----------------------------------------------

    async def _switch_to(self, modo: DataSource) -> None:
        if modo is self._mode:
            return

        anterior = self._mode
        self._mode = modo
        self._switches += 1
        self._pending_switch = modo

        if modo is DataSource.SIM:
            self._programar_reintento()
        else:
            # Volvimos a XM: el backoff se reinicia desde cero.
            self._retry_attempts = 0
            self._next_retry_at = None

        logger.warning(
            "conmutación de fuente",
            extra={
                "desde": anterior.value,
                "hacia": modo.value,
                "fallos": self._consecutive_failures,
                "proximo_reintento": self._iso(self._next_retry_at),
            },
        )
        await self._persistir_salud()

    def _programar_reintento(self) -> None:
        """Backoff exponencial acotado: 5 → 10 → 20 → 30 min."""
        espera = min(self._backoff_base * (2**self._retry_attempts), self._backoff_cap)
        self._retry_attempts += 1
        self._next_retry_at = self._clock() + timedelta(seconds=espera)

    async def _on_success(self) -> None:
        self._consecutive_failures = 0
        self._retry_attempts = 0
        self._last_success = self._clock()
        await self._persistir_salud()

    async def _on_failure(self, exc: DataSourceError) -> None:
        self._consecutive_failures += 1
        self._last_failure = self._clock()
        logger.warning(
            "fallo consultando XM",
            extra={
                "fallos_consecutivos": self._consecutive_failures,
                "umbral": self._max_failures,
                "error": str(exc),
            },
        )
        await self._persistir_salud()

    async def _persistir_salud(self) -> None:
        """Vuelca el estado a Redis para `/api/health` y el banner.

        Degrada en silencio: si Redis no está, el publisher debe seguir
        generando datos igual. Perder el banner es molesto; parar el
        pipeline por eso sería peor.
        """
        if self._redis is None:
            return

        valores: dict[str, str] = {
            KEY_HEALTH_MODE: self._mode.value,
            KEY_HEALTH_FAILURES: str(self._consecutive_failures),
            KEY_HEALTH_SOURCE_SWITCHES: str(self._switches),
        }
        if self._last_success is not None:
            valores[KEY_HEALTH_XM_LAST_SUCCESS] = self._iso(self._last_success) or ""
        if self._last_failure is not None:
            valores[KEY_HEALTH_XM_LAST_FAILURE] = self._iso(self._last_failure) or ""
        if self._next_retry_at is not None:
            valores[KEY_HEALTH_NEXT_RETRY_AT] = self._iso(self._next_retry_at) or ""

        try:
            await self._redis.mset(valores)
        except Exception as exc:  # noqa: BLE001 - la salud no puede tumbar el loop
            logger.warning("no se pudo persistir salud en Redis", extra={"error": str(exc)})

    @staticmethod
    def _iso(momento: datetime | None) -> str | None:
        return momento.isoformat() if momento is not None else None

    async def aclose(self) -> None:
        await self._real.aclose()


__all__ = ["MAX_CONSECUTIVE_FAILURES", "SourceSelector"]
