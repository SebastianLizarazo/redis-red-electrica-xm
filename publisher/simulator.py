"""
Simulador de respaldo (T-PUB-002).

Implementa el Protocol `DataSource` generando demanda nacional sintética
cuando XM no está disponible. El reparto por zonas y el despacho de
generación NO viven aquí: los hace `normalizer.build_events()`, el mismo que
usa la fuente real. Así el simulador solo responde una pregunta —"¿cuántos MW
consume el país en este instante?"— y el resto del evento se construye igual
que con datos reales.

Por qué no es `random()`
------------------------
El taller pide explícitamente series coherentes: si una zona consume 1800 MW,
el siguiente valor no puede ser 400 MW. La técnica es un **paseo aleatorio
acotado**:

1. Una curva circadiana fija el objetivo de demanda para la hora local
   (valle entre 3 y 5 am, pico a las 19 h, cuando la iluminación residencial
   se suma al consumo comercial que aún no cierra).
2. El valor actual se mueve solo una fracción hacia ese objetivo.
3. Se añade un ruido pequeño.

El resultado son curvas continuas con forma de día real, no ruido blanco.

Eventos de stress
-----------------
Los 4 escenarios del roadmap se inyectan desde la API (`POST /api/stress/...`)
escribiendo flags con TTL corto en Redis. El simulador los lee en cada ciclo;
nunca los escribe. `recovery` es especial: limpia los otros tres.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime
from typing import Any, ClassVar

from common.logging_config import get_logger
from common.models import DataSource, Event
from common.redis_keys import stress_key
from publisher.normalizer import (
    DEMANDA_NACIONAL_MAX_MW,
    DEMANDA_NACIONAL_MIN_MW,
    build_events,
    hora_colombia_decimal,
    interpolar_curva,
)

logger = get_logger("publisher.simulator")

# Demanda nacional de referencia en el pico (MW). El SIN colombiano ronda
# los 10.5 GW en el pico de la noche.
DEMANDA_PICO_NACIONAL_MW = 10_500.0

# Curva circadiana: factor sobre el pico, por hora local (0-23).
# Valle en 3-5 am (0.72) y pico a las 19 h (1.00), según T-PUB-002.
CURVA_DEMANDA: tuple[float, ...] = (
    0.82, 0.78, 0.75, 0.73, 0.72, 0.72,
    0.78, 0.86, 0.90, 0.93, 0.94, 0.95,
    0.94, 0.93, 0.93, 0.93, 0.93, 0.95,
    0.97, 1.00, 0.99, 0.95, 0.90, 0.86,
)

# Cuánto se acerca el valor actual al objetivo en cada tick. Bajo = suave.
SUAVIZADO = 0.25

# Ruido máximo por tick, como fracción del valor.
RUIDO = 0.015

# Los 4 escenarios inyectables del roadmap.
STRESS_DEMAND_SURGE = "demand_surge"
STRESS_HYDRO_DROP = "hydro_drop"
STRESS_CRITICAL_DEFICIT = "critical_deficit"
STRESS_RECOVERY = "recovery"

STRESS_EVENTS: tuple[str, ...] = (
    STRESS_DEMAND_SURGE,
    STRESS_HYDRO_DROP,
    STRESS_CRITICAL_DEFICIT,
    STRESS_RECOVERY,
)

# Intensidad de cada escenario.
FACTOR_DEMAND_SURGE = 1.25  # +25% de demanda: sobrecarga el sistema
FACTOR_HYDRO_DROP = 0.45  # embalses bajos: la térmica tiene que cubrir
FACTOR_CRITICAL_DEFICIT = 0.80  # generación deprimida: déficit > 800 MW


class SimulatorSource:
    """Fuente sintética con patrones circadianos y escenarios de stress."""

    name: ClassVar[str] = "simulator"

    def __init__(
        self,
        *,
        redis: Any | None = None,
        rng: random.Random | None = None,
        demanda_pico_mw: float = DEMANDA_PICO_NACIONAL_MW,
    ) -> None:
        self._redis = redis
        self._rng = rng or random.Random()
        self._pico = demanda_pico_mw
        # Estado del paseo aleatorio. Arranca en el objetivo de la hora actual
        # para que el primer tick ya sea plausible y no un escalón.
        self._demanda_actual: float | None = None
        # Stress inyectado en proceso (tests y demo sin API levantada).
        self._stress_local: set[str] = set()

    # --- Protocol DataSource ------------------------------------------------

    async def fetch_event(self) -> Event | None:
        eventos = await self.fetch_events()
        return next((e for e in eventos if e.entity_id == "SIN"), None)

    async def fetch_events(self) -> list[Event]:
        """Genera los 6 eventos del ciclo (5 zonas + SIN)."""
        ahora = datetime.now(UTC)
        activos = await self._leer_stress()

        demanda = self._siguiente_demanda(ahora)
        hydro_factor = 1.0
        generation_factor = 1.0

        if STRESS_DEMAND_SURGE in activos:
            demanda *= FACTOR_DEMAND_SURGE
        if STRESS_HYDRO_DROP in activos:
            hydro_factor = FACTOR_HYDRO_DROP
        if STRESS_CRITICAL_DEFICIT in activos:
            generation_factor = FACTOR_CRITICAL_DEFICIT

        # El clamp evita que un surge encadenado saque la demanda del rango
        # que `build_events` considera físicamente posible.
        demanda = min(max(demanda, DEMANDA_NACIONAL_MIN_MW), DEMANDA_NACIONAL_MAX_MW)

        if activos:
            logger.info("simulando con stress activo", extra={"stress": sorted(activos)})

        return build_events(
            demanda_nacional_mw=demanda,
            fuente=DataSource.SIM,
            timestamp=ahora,
            hydro_factor=hydro_factor,
            generation_factor=generation_factor,
        )

    async def health_check(self) -> bool:
        """El simulador siempre está sano: es el último recurso del sistema."""
        return True

    # --- Inyección local de stress (sin API) --------------------------------

    def inject_stress(self, evento: str) -> None:
        """Activa un escenario en memoria. Para tests y demo sin API.

        En producción los flags llegan por Redis desde `POST /api/stress/...`.
        """
        if evento not in STRESS_EVENTS:
            raise ValueError(f"escenario desconocido: {evento!r}; use uno de {STRESS_EVENTS}")
        if evento == STRESS_RECOVERY:
            self._stress_local.clear()
            return
        self._stress_local.add(evento)

    def clear_stress(self) -> None:
        self._stress_local.clear()

    # --- Internos -----------------------------------------------------------

    def _siguiente_demanda(self, ahora: datetime) -> float:
        """Paseo aleatorio acotado hacia el objetivo circadiano."""
        hora = hora_colombia_decimal(ahora)
        objetivo = self._pico * interpolar_curva(CURVA_DEMANDA, hora)

        if self._demanda_actual is None:
            self._demanda_actual = objetivo

        acercamiento = self._demanda_actual + (objetivo - self._demanda_actual) * SUAVIZADO
        ruido = 1.0 + (self._rng.random() * 2 - 1) * RUIDO
        self._demanda_actual = max(0.0, acercamiento * ruido)
        return self._demanda_actual

    async def _leer_stress(self) -> set[str]:
        """Lee los flags de stress: primero Redis, luego los locales.

        Nunca propaga errores: el simulador es el respaldo del sistema y un
        fallo leyendo flags de demo no puede dejar al publisher sin datos.
        """
        activos: set[str] = set(self._stress_local)

        if self._redis is not None:
            try:
                claves = [stress_key(e) for e in STRESS_EVENTS]
                valores = await self._redis.mget(claves)
                for evento, valor in zip(STRESS_EVENTS, valores, strict=True):
                    if valor is not None:
                        activos.add(evento)
            except Exception as exc:  # noqa: BLE001 - degradar, nunca tumbar
                logger.warning("no se pudieron leer flags de stress", extra={"error": str(exc)})

        if STRESS_RECOVERY in activos:
            # `recovery` cancela todo lo demás y limpia el estado local, para
            # que el operador tenga un botón de "volver a la normalidad".
            self._stress_local.clear()
            await self._borrar_stress_remoto()
            return set()

        return activos

    async def _borrar_stress_remoto(self) -> None:
        if self._redis is None:
            return
        try:
            claves = [stress_key(e) for e in STRESS_EVENTS]
            await self._redis.delete(*claves)
        except Exception as exc:  # noqa: BLE001 - mismo criterio que arriba
            logger.warning("no se pudieron limpiar flags de stress", extra={"error": str(exc)})


__all__ = [
    "CURVA_DEMANDA",
    "DEMANDA_PICO_NACIONAL_MW",
    "STRESS_CRITICAL_DEFICIT",
    "STRESS_DEMAND_SURGE",
    "STRESS_EVENTS",
    "STRESS_HYDRO_DROP",
    "STRESS_RECOVERY",
    "SimulatorSource",
]
