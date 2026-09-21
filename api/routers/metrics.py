"""`GET /api/metrics` — 3 métricas derivadas (REQ-API-003).

Lee los 3 hashes `metrics:renewable|balance|demand:variation` vía HGETALL
(en pipeline, 1 round-trip) y devuelve `list[Metric]` de length 3.

Manejo de M3 NaN (design #511 §6):
- El subscriber escribe el literal `"NaN"` cuando no hay histórico para M3
  (subscriber/metrics.py:persist_metrics — `hash_value = "NaN" if raw_value is None`).
- `Metric.value: float | None` (extendido en PR-B) acepta None y Pydantic
  lo serializa como JSON `null` (REQ-API-003 scenario 2).
- Solución: pre-coerción en este router — `None if raw == "NaN" else float(raw)`.

Orden canónico de las métricas (alineado con subscriber/metrics.py:_METRIC_KEYS):
1. renewable_pct (% renewable, M1)
2. balance_mw (déficit MW, M2)
3. demand_variation_pct (% variación demanda, M3)

Casos cubiertos:
- 3 hashes con valores numéricos → 3 items, todos con `value` numérico.
- M3 con `value == "NaN"` → su item tiene `value: null` en JSON.
- Hash ausente o corrupto → fallback con `value: null` (preserva length 3).
- Redis caído → 503 via handler global de `api/server.py`.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from redis.asyncio import Redis

from api.dependencies import get_redis
from common.logging_config import get_logger
from common.models import Metric
from common.redis_keys import (
    KEY_METRICS_BALANCE,
    KEY_METRICS_DEMAND_VARIATION,
    KEY_METRICS_RENEWABLE,
)

logger = get_logger("api.routers.metrics")

router = APIRouter(prefix="/api", tags=["metrics"])


# (name, redis_key) en el orden canónico que verá el cliente.
# Idéntico al orden de `_METRIC_KEYS` en subscriber/metrics.py.
_METRIC_DEFS: tuple[tuple[str, str], ...] = (
    ("renewable_pct", KEY_METRICS_RENEWABLE),
    ("balance_mw", KEY_METRICS_BALANCE),
    ("demand_variation_pct", KEY_METRICS_DEMAND_VARIATION),
)

# Sentinel para hashes ausentes/corruptos: timestamp epoch UTC (siempre
# parseable por Pydantic como AwareDatetime) + unit "?" (pasa min_length=1).
# El dashboard distingue `value: null` de un valor numérico real.
_EMPTY_TIMESTAMP = "1970-01-01T00:00:00+00:00"
_EMPTY_UNIT = "?"


def _metric_from_hash(name: str, raw: dict[str, str]) -> Metric:
    """Coacciona un hash `metrics:*` a `Metric` validado.

    Lanza si el hash está ausente o corrupto — el handler del router
    captura y devuelve el fallback `_empty_metric` para preservar el
    contrato `length 3` del spec REQ-API-003.
    """
    if not raw:
        raise ValueError(f"hash ausente para métrica {name!r}")

    raw_value = raw.get("value", "")
    # Coerción del literal "NaN" del subscriber. NO usamos math.nan porque
    # Pydantic v2 lo rechaza en el field validator; usamos None directamente.
    if raw_value == "NaN":
        value: float | None = None
    else:
        value = float(raw_value)

    return Metric(
        name=name,
        value=value,
        unit=raw["unit"],
        timestamp=raw["timestamp"],
        zone_id=raw.get("zone_id"),  # type: ignore[arg-type]
    )


def _empty_metric(name: str) -> Metric:
    """Fallback cuando el hash no existe o es corrupto.

    `value=None` → JSON `null`. El dashboard lo renderiza como "cargando"
    sin distinguir de "valor realmente ausente" (caso idéntico al M3 sin
    histórico — el spec los unifica).
    """
    return Metric(
        name=name,
        value=None,
        unit=_EMPTY_UNIT,
        timestamp=_EMPTY_TIMESTAMP,
        zone_id=None,
    )


@router.get("/metrics")
async def get_metrics(redis: Redis = Depends(get_redis)) -> list[Metric]:
    """Snapshot de las 3 métricas derivadas (M1, M2, M3) en orden canónico."""
    # Pipeline para reducir 3 round-trips HGETALL a 1.
    pipe = redis.pipeline(transaction=False)
    for _name, key in _METRIC_DEFS:
        pipe.hgetall(key)
    hashes = await pipe.execute()

    metrics: list[Metric] = []
    for (name, _key), raw in zip(_METRIC_DEFS, hashes, strict=True):
        try:
            metrics.append(_metric_from_hash(name, raw))
        except Exception as exc:  # noqa: BLE001 - un hash corrupto no rompe la lista
            logger.warning(
                "metric hash corrupto o ausente, devolviendo null",
                extra={"metric": name, "error": str(exc)},
            )
            # Fallback para preservar `length 3` del contrato REQ-API-003.
            metrics.append(_empty_metric(name))
    return metrics


__all__ = ["router"]
