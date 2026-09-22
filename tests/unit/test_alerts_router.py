"""Tests for `api.routers.alerts` (REQ-API-004 de spec #510).

Convención STANDARD: TDD laxo, tests con código OK.

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


# ---------------------------------------------------------------------------
# test_003/004 — REGRESIÓN: la alerta global A1 llega al cliente
# ---------------------------------------------------------------------------
#
# A1 (DEMAND_GENERATION_GAP) evalúa el SIN completo, no una zona, y el
# subscriber marca ese alcance con el centinela `_GLOBAL_ZONE = ""`.
#
# `Alert.zone_id` estaba tipado como `ZoneId`, que no admite cadena vacía. El
# subscriber esquivaba la validación al escribir (`model_construct`), pero
# `api/routers/alerts.py` valida al leer con `model_validate_json`: todas las
# A1 se descartaban en silencio y nunca llegaban al dashboard. Medido contra
# el stack real: 13 alertas en Redis, 1 expuesta por `/api/alerts`.
#
# Si alguien vuelve a estrechar el tipo, estos dos tests fallan.


def _make_global_a1_alert(state: str = "active") -> Alert:
    """Alerta A1 con el centinela de zona global.

    Se construye con el constructor normal (no `model_construct`) a
    propósito: si el modelo dejara de aceptar `""`, el test falla aquí
    mismo, antes de llegar al router.
    """
    return Alert(
        id=f"alert-global-a1-{state}",
        rule="DEMAND_GENERATION_GAP",
        code="DEMAND_GENERATION_GAP",
        severity=AlertSeverity.HIGH,
        zone_id="",
        value=1472.0,
        threshold=800.0,
        state=state,  # type: ignore[arg-type]
        consecutive_cycles=2,
        timestamp=datetime(2026, 9, 21, 12, 0, 0, tzinfo=UTC),
        message="DEMAND_GENERATION_GAP active for zone : 1472.00 vs threshold 800.00",
    )


async def test_003_global_a1_alert_reaches_the_client(app_client, fakeredis_async_client):
    """Una A1 con `zone_id=""` publicada por el subscriber llega al GET."""
    from subscriber.alerts import publish_alert

    await publish_alert(fakeredis_async_client, _make_global_a1_alert())

    resp = await app_client.get("/api/alerts")
    assert resp.status_code == 200
    body = resp.json()

    # Lo que fallaba: `recent` venía vacío porque el Alert no validaba.
    assert len(body["recent"]) == 1, "la alerta global se descartó al leerla"
    alerta = body["recent"][0]
    assert alerta["rule"] == "DEMAND_GENERATION_GAP"
    assert alerta["zone_id"] == "", "el centinela de zona global debe sobrevivir el round-trip"
    assert alerta["value"] == 1472.0
    assert body["active"] == {"DEMAND_GENERATION_GAP": 1}


async def test_004_global_and_per_zone_alerts_coexist(app_client, fakeredis_async_client):
    """Conviven una alerta global y una por zona.

    Este es el caso que enmascaraba el bug: la A2 por zona sí aparecía, así
    que `/api/alerts` devolvía datos y parecía funcionar. Solo faltaba la
    global, y el panel del dashboard mostraba de menos sin ningún error.
    """
    from subscriber.alerts import publish_alert

    await publish_alert(fakeredis_async_client, _make_global_a1_alert())
    await publish_alert(fakeredis_async_client, _make_active_alert("LOW_RENEWABLE", zone="ATL"))

    body = (await app_client.get("/api/alerts")).json()

    zonas = sorted(a["zone_id"] for a in body["recent"])
    assert zonas == ["", "ATL"], f"se perdió alguna alerta: {zonas}"
