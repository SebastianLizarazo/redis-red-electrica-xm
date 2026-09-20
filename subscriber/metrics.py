"""Subscriber metrics layer.

Funciones puras sobre `Event` (sin I/O excepto `persist_metrics`). El
`compute_metrics` es stateless por diseño: el caller pasa `previous_demand_mw`
explícitamente. Esto facilita `pytest.mark.parametrize` con entradas
independientes y permite paralelizar el engine si el volumen lo exige.

Capas:
- `compute_metrics(event, previous_demand_mw)` → dict con 3 métricas derivadas
  (M1 = % renewable, M2 = balance demanda-generación, M3 = variación % demanda).
- `persist_metrics(redis, metrics)` → escribe los 3 hashes `metrics:*` con
  HSET. La actualización del ZSet `metrics:demand:history` queda en manos del
  processor (PR-B) porque necesita el `unix_ts` actual, no el del `event`.
"""
from __future__ import annotations

from typing import Any

from redis.asyncio import Redis

from common.models import Event
from common.redis_keys import (
    KEY_DEMAND_HISTORY,
    KEY_METRICS_BALANCE,
    KEY_METRICS_DEMAND_VARIATION,
    KEY_METRICS_RENEWABLE,
)


# Units (símbolos canónicos para que la API y el dashboard rendericen igual).
UNIT_PERCENT = "%"
UNIT_MW = "MW"


# Mapping nombre lógico → key Redis. Inmutable y compartido entre
# `compute_metrics` (que etiqueta el dict) y `persist_metrics` (que resuelve
# la key para el HSET).
_METRIC_KEYS: dict[str, str] = {
    "renewable_pct": KEY_METRICS_RENEWABLE,
    "balance_mw": KEY_METRICS_BALANCE,
    "demand_variation_pct": KEY_METRICS_DEMAND_VARIATION,
}


def _ts(event: Event) -> str:
    """Serializa `event.timestamp` a ISO 8601 canónico.

    Pydantic v2 emite ISO con `+00:00`; el dashboard ya acepta ese formato.
    Se mantiene el offset explícito para que el round-trip sea estable.
    """
    return event.timestamp.isoformat()


def compute_metrics(
    event: Event, *, previous_demand_mw: float | None
) -> dict[str, dict[str, Any]]:
    """Calcula M1, M2 y M3 para un tick.

    - **M1 (% renewable)** = `(solar + eolica + hidro) / generacion * 100`.
      Si `generacion_mw == 0` → M1 = 0.0 (sin division-by-zero).
    - **M2 (balance)** = `demanda_mw - generacion_mw` (negativo = superávit).
    - **M3 (% variación demanda)** = `(actual - previa) / previa * 100`.
      Si `previous_demand_mw is None` o `0` → M3 = `None` (persiste como
      literal `"NaN"` en el hash; ver `persist_metrics`).

    Retorna `dict[str, dict[str, Any]]` con tres claves (`renewable_pct`,
    `balance_mw`, `demand_variation_pct`). Cada valor contiene los campos
    `value`, `unit`, `timestamp` (ISO), `zone_id` y `fuente` (string del enum).
    """
    d = event.data
    renewable_mw = (
        d.generacion_solar_mw + d.generacion_eolica_mw + d.generacion_hidraulica_mw
    )
    m1: float = 0.0 if d.generacion_mw == 0.0 else renewable_mw / d.generacion_mw * 100.0
    m2: float = d.demanda_mw - d.generacion_mw
    if previous_demand_mw is None or previous_demand_mw == 0.0:
        m3: float | None = None
    else:
        m3 = (d.demanda_mw - previous_demand_mw) / previous_demand_mw * 100.0

    base = {
        "timestamp": _ts(event),
        "zone_id": event.entity_id,
        "fuente": d.fuente.value,
    }
    return {
        "renewable_pct": {"value": m1, "unit": UNIT_PERCENT, **base},
        "balance_mw": {"value": m2, "unit": UNIT_MW, **base},
        "demand_variation_pct": {"value": m3, "unit": UNIT_PERCENT, **base},
    }


async def persist_metrics(redis: Redis, metrics: dict[str, dict[str, Any]]) -> None:
    """Persiste las métricas en sus hashes Redis.

    Para cada métrica: `HSET <key> value <v> unit <u> timestamp <iso>
    zone_id <id> fuente <src>`. Si `value` es `None` (M3 sin histórico), se
    escribe el literal `"NaN"` porque los hashes de Redis almacenan strings;
    el dashboard distingue `"NaN"` de un valor numérico válido.

    El ZSet `metrics:demand:history` se actualiza en el processor (PR-B)
    tras este HSET, una vez conocida la `demanda_mw` del tick actual.
    Aquí sólo se escribe estado de métricas.
    """
    for metric_name, metric in metrics.items():
        redis_key = _METRIC_KEYS[metric_name]
        raw_value = metric["value"]
        # None → "NaN" string (fakeredis y redis-py escriben todo como texto).
        hash_value = "NaN" if raw_value is None else float(raw_value)
        await redis.hset(
            redis_key,
            mapping={
                "value": hash_value,
                "unit": metric["unit"],
                "timestamp": metric["timestamp"],
                "zone_id": metric["zone_id"],
                "fuente": metric["fuente"],
            },
        )


__all__ = [
    "KEY_DEMAND_HISTORY",
    "UNIT_MW",
    "UNIT_PERCENT",
    "compute_metrics",
    "persist_metrics",
]
