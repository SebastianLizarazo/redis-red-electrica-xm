"""`GET /api/alerts` — alertas operacionales recientes + counters activos.

Devuelve `{recent: list[Alert], active: dict[str, int]}`:
- `recent`: hasta 20 alertas parseadas (orden = orden del LPUSH, newest-first).
- `active`: counters por código (`DEMAND_GENERATION_GAP`, `LOW_RENEWABLE`).

Discrepancia del contrato entre el spec y el subscriber (ya archivado):
- Spec #510 REQ-API-004 dice: `recent: list[Alert] (max 20 via LRANGE 0 19
  de `alerts:recent`)`.
- El subscriber (subscriber/alerts.py:251) LPUSHea SOLO `alert.id` (UUID)
  en la lista, no el payload completo. El payload completo vive en el
  Stream `alerts:stream` (XADD con `{"payload": <json>}`).
- Resolución: LRANGE para el orden canónico de IDs + XREVRANGE sobre
  `alerts:stream` para reconstruir el `Alert` con todos los campos.
  Si un ID del list no aparece en el stream (MAXLEN ~ 100 trimming
  barrió la alerta), se omite del `recent` con un DEBUG log.

Casos cubiertos:
- Stream + recent + counters poblados → `recent` es list[Alert], `active` poblado.
- Sin alertas → `recent: []`, `active: {}`.
- Redis caído → 503 via handler global de `api/server.py`.

Decisión: importamos RULE_A1/RULE_A2 de subscriber.alerts para mantener
una sola fuente de verdad del wire format (cualquier nuevo rule debe
añadirse tanto al AlertEngine como a `_ACTIVE_CODES`).
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends
from redis.asyncio import Redis

from api.dependencies import get_redis
from common.logging_config import get_logger
from common.models import Alert
from common.redis_keys import (
    KEY_RECENT_ALERTS,
    STREAM_ALERTS,
    alerts_active_key,
)
from subscriber.alerts import RULE_A1, RULE_A2

logger = get_logger("api.routers.alerts")

router = APIRouter(prefix="/api", tags=["alerts"])


# Códigos que el subscriber mantiene como counter activo (alineado con
# `subscriber.alerts:AlertEngine.__init__`). Si añadís un nuevo rule
# (A3, A4, ...), actualizá AMBOS lugares: el engine Y esta tupla.
_ACTIVE_CODES: tuple[str, ...] = (RULE_A1, RULE_A2)


def _parse_alerts_payloads(
    stream_entries: list, payload_by_id: dict[str, str]
) -> None:
    """Indexa los payloads del stream por `alert.id` para lookup O(1)."""
    for _entry_id, fields in stream_entries:
        raw_payload = fields.get("payload")
        if not raw_payload:
            continue
        try:
            data = json.loads(raw_payload)
            alert_id = data.get("id")
            if alert_id:
                payload_by_id[alert_id] = raw_payload
        except (json.JSONDecodeError, KeyError, TypeError):
            # Payload malformado: lo saltamos silenciosamente. El
            # dashboard solo necesita alertas bien formadas para mostrar.
            continue


@router.get("/alerts")
async def get_alerts(redis: Redis = Depends(get_redis)) -> dict:
    """Alertas recientes (hasta 20) + counters activos por código."""
    # Pipeline para reducir 3 round-trips (LRANGE + XREVRANGE + MGET) a 1.
    pipe = redis.pipeline(transaction=False)
    pipe.lrange(KEY_RECENT_ALERTS, 0, 19)
    pipe.xrevrange(STREAM_ALERTS, "+", "-", count=20)
    pipe.mget([alerts_active_key(code) for code in _ACTIVE_CODES])
    recent_ids_raw, stream_entries, active_values_raw = await pipe.execute()

    # recent_ids_raw puede ser list[str] o list[bytes] según el backend
    # (fakeredis devuelve str por `decode_responses=True`). Lo aceptamos
    # como iterable y normalizamos abajo.
    recent_ids: list[str] = [
        rid.decode() if isinstance(rid, bytes) else rid for rid in recent_ids_raw
    ]

    # Indexamos payloads por ID.
    payload_by_id: dict[str, str] = {}
    _parse_alerts_payloads(stream_entries or [], payload_by_id)

    # Reconstruimos list[Alert] en el orden del LPUSH (newest-first).
    recent: list[Alert] = []
    for alert_id in recent_ids:
        raw = payload_by_id.get(alert_id)
        if raw is None:
            # Stream MAXLEN (~100) recortó esta alerta. Es vieja: la
            # omitimos. NO es un error operacional — el cap es por diseño.
            logger.debug(
                "alerta reciente sin payload en stream, omitiendo",
                extra={"alert_id": alert_id},
            )
            continue
        try:
            recent.append(Alert.model_validate_json(raw))
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "alert payload corrupto, omitiendo",
                extra={"alert_id": alert_id, "error": str(exc)},
            )

    # Counters activos: solo aparecen códigos con valor > 0 en Redis.
    # Un counter inexistente en Redis significa "sin alertas activas",
    # no "0 alertas" — por eso filtramos `None` en lugar de poner 0.
    active: dict[str, int] = {}
    for code, raw in zip(_ACTIVE_CODES, active_values_raw, strict=True):
        if raw is None:
            continue
        # fakeredis con decode_responses=True devuelve str; redis real
        # podría devolver bytes según el cliente.
        raw_str = raw.decode() if isinstance(raw, bytes) else raw
        try:
            active[code] = int(raw_str)
        except ValueError:
            logger.warning(
                "active counter corrupto, ignorando",
                extra={"code": code, "raw": raw_str},
            )

    return {"recent": recent, "active": active}


__all__ = ["router"]
