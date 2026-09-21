"""Tests for `api.routers.alerts` (REQ-API-004 de spec #510).

Convención STANDARD (obs #458): TDD laxo, tests con código OK.

Cobertura:
- test_001 HAPPY: recent (list[Alert]) + active (dict[str,int]) poblados vía
  `subscriber.alerts.publish_alert` (la única vía canónica de escribir las 5
  estructuras Redis: stream + recent list + counters + pubsub + total).
- test_002 EDGE: sin alertas → `recent: []`, `active: {}`.

Nota sobre la discrepancia del contrato (ver obs apply-progress + módulo
`api/routers/alerts.py`): el subscriber LPUSHea solo `alert.id` en
`alerts:recent` (no el payload). El router cruza con XREVRANGE sobre
`alerts:stream` para reconstruir el `Alert` completo. Los tests verifican
este comportamiento end-to-end sembrando vía `publish_alert` y leyendo vía
`GET /api/alerts`.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from common.models import Alert, AlertSeverity
from common.redis_keys import alerts_active_key

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers — factories canónicas para sembrar vía publish_alert
# ---------------------------------------------------------------------------


def _make_active_alert(code: str, zone: str = "ANT") -> Alert:
    """Alerta activa de ejemplo, suficiente para pasar el publish_alert."""
    return Alert(
        id=f"alert-{code.lower()}-{zone}-001",
        rule=code,
        code=code,
        severity=AlertSeverity.HIGH if code == "DEMAND_GENERATION_GAP" else AlertSeverity.MEDIUM,
        zone_id=zone,  # type: ignore[arg-type]
        value=1000.0,
        threshold=800.0,
        state="active",
        consecutive_cycles=2,
        timestamp=datetime(2026, 9, 21, 12, 0, 0, tzinfo=UTC),
        message=f"{code} active for {zone}: 1000.00 vs threshold 800.00",
    )


# ---------------------------------------------------------------------------
# test_001 — HAPPY: recent + active ambos poblados (REQ-API-004 scenario 1)
# ---------------------------------------------------------------------------


async def test_001_alerts_recent_and_active_both_populated(
    app_client, fakeredis_async_client
):
    """Tras publicar 2 alertas (una por código), el GET devuelve:
    - `recent`: lista con 2 Alerts parseadas (orden LPUSH = newest-first).
    - `active`: dict con los 2 counters en su valor correcto.

    Sembramos con `subscriber.alerts.publish_alert` porque es la única vía
    canónica de escribir las 5 estructuras Redis sincronizadas. El test
    valida end-to-end que el API puede reconstruir el `Alert` desde el
    stream cuando el list solo tiene IDs."""
    from subscriber.alerts import publish_alert

    # Publicamos 2 alertas: una de cada rule code conocido.
    await publish_alert(fakeredis_async_client, _make_active_alert("DEMAND_GENERATION_GAP"))
    await publish_alert(fakeredis_async_client, _make_active_alert("LOW_RENEWABLE", zone="VAL"))

    resp = await app_client.get("/api/alerts")
    assert resp.status_code == 200
    body = resp.json()

    # --- recent ---
    assert isinstance(body["recent"], list)
    assert len(body["recent"]) == 2
    # Orden LPUSH: newest-first → la segunda (LOW_RENEWABLE) aparece primero.
    assert body["recent"][0]["code"] == "LOW_RENEWABLE"
    assert body["recent"][1]["code"] == "DEMAND_GENERATION_GAP"
    # Cada item tiene los campos del Alert.
    first = body["recent"][0]
    assert first["rule"] == "LOW_RENEWABLE"
    assert first["zone_id"] == "VAL"
    assert first["severity"] == "medium"
    assert first["state"] == "active"
    assert first["consecutive_cycles"] == 2
    assert first["value"] == 1000.0
    assert first["threshold"] == 800.0
    # Timestamps parseados como ISO 8601 con offset UTC.
    assert first["timestamp"].startswith("2026-09-21T12:00:00")
    assert "T" in first["timestamp"]

    # --- active ---
    assert isinstance(body["active"], dict)
    assert body["active"] == {
        "DEMAND_GENERATION_GAP": 1,
        "LOW_RENEWABLE": 1,
    }
    # Verificamos también que el counter Redis coincide con lo reportado.
    assert await fakeredis_async_client.get(alerts_active_key("DEMAND_GENERATION_GAP")) == "1"
    assert await fakeredis_async_client.get(alerts_active_key("LOW_RENEWABLE")) == "1"


# ---------------------------------------------------------------------------
# test_002 — EDGE: sin alertas → contenedores vacíos (REQ-API-004 scenario 2)
# ---------------------------------------------------------------------------


async def test_002_alerts_empty_recent_and_active_returns_empty_containers(
    app_client, fakeredis_async_client
):
    """Sin sembrar nada (publisher nunca publicó alertas, subscriber en cold start):
    el GET devuelve `recent: []` y `active: {}` sin errores."""
    # Sanity check: Redis está vacío (la fixture fakeredis es por-test).
    assert await fakeredis_async_client.exists("alerts:recent") == 0
    assert await fakeredis_async_client.exists("alerts:stream") == 0

    resp = await app_client.get("/api/alerts")
    assert resp.status_code == 200
    body = resp.json()

    assert body == {"recent": [], "active": {}}
    # Doble check de tipos (no strings vacíos ni nulls).
    assert isinstance(body["recent"], list)
    assert isinstance(body["active"], dict)
    assert len(body["recent"]) == 0
    assert len(body["active"]) == 0
