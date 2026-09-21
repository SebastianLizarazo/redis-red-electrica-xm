"""Tests for `api.routers.state` (REQ-API-002 de spec #510).

Convención STANDARD: TDD laxo, tests con código OK. Pero
los tests son obligatorios para routers y deben cubrir happy + edge
cases del spec.

Estrategia: usamos `app_client` (fixture de `tests/conftest.py`) que
crea una FastAPI app con `dependency_overrides[get_redis] = fakeredis`.
Cada test siembra el fakeredis manualmente y luego hace el GET.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from common.redis_keys import state_sin_key, state_zone_key

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hash_for(zone: str, *, ts: datetime | None = None) -> dict[str, str]:
    """Fabrica un hash `state:zone:*` con el wire format del publisher
    (`publisher/main.py:_aplanar`)."""
    ts = ts or datetime(2026, 9, 21, 12, 0, 0, tzinfo=UTC)
    return {
        "entity_id": zone,
        "timestamp": ts.isoformat(),
        "latitude": "4.5" if zone == "SIN" else "6.25",
        "longitude": "-74.1" if zone == "SIN" else "-75.56",
        "demanda_mw": "1000.0",
        "generacion_mw": "1100.0",
        "generacion_solar_mw": "200.0",
        "generacion_eolica_mw": "50.0",
        "generacion_hidraulica_mw": "600.0",
        "generacion_termica_mw": "250.0",
        "fuente": "simulator",
    }


async def _seed_full(fakeredis_async_client) -> None:
    """Sembrar las 5 zonas + SIN como lo haría el publisher tras un ciclo completo."""
    await fakeredis_async_client.hset(state_sin_key(), mapping=_hash_for("SIN"))
    for zid in ("ANT", "VAL", "ATL", "BOG", "SAN"):
        await fakeredis_async_client.hset(state_zone_key(zid), mapping=_hash_for(zid))


# ---------------------------------------------------------------------------
# test_001 — HAPPY: 5 zones + SIN presentes
# ---------------------------------------------------------------------------


async def test_001_state_happy_path_returns_5_zones_and_sin(
    app_client, fakeredis_async_client
):
    """Las 5 zonas + SIN pobladas → `zones` length 5, `sin.zone_id == "SIN"`."""
    await _seed_full(fakeredis_async_client)

    resp = await app_client.get("/api/state")
    assert resp.status_code == 200
    body = resp.json()

    assert body["sin"] is not None
    assert body["sin"]["zone_id"] == "SIN"
    assert body["sin"]["demanda_mw"] == 1000.0
    assert body["sin"]["generacion_mw"] == 1100.0
    assert body["sin"]["fuente"] == "simulator"

    assert isinstance(body["zones"], list)
    assert len(body["zones"]) == 5
    zone_ids = [z["zone_id"] for z in body["zones"]]
    assert zone_ids == ["ANT", "VAL", "ATL", "BOG", "SAN"]


# ---------------------------------------------------------------------------
# test_002 — EDGE: zonas vacías → `zones: []`, sin 500
# ---------------------------------------------------------------------------


async def test_002_state_empty_zones_returns_empty_list_not_500(
    app_client, fakeredis_async_client
):
    """Publisher aún no escribió `state:zone:*` → `zones: []`, `sin: null`.
    El handler no debe lanzar 500."""
    # Sembramos SOLO el SIN para que `sin` no sea None; las 5 zonas quedan vacías.
    # (Variante más dura del edge case: ni siquiera SIN.)
    # Probamos la variante más dura primero: todo vacío.
    resp = await app_client.get("/api/state")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"sin": None, "zones": []}


async def test_002b_state_partial_zones_skips_missing(
    app_client, fakeredis_async_client
):
    """Solo 2 zonas escritas → `zones` length 2, las que están."""
    await fakeredis_async_client.hset(state_zone_key("ANT"), mapping=_hash_for("ANT"))
    await fakeredis_async_client.hset(state_zone_key("BOG"), mapping=_hash_for("BOG"))

    resp = await app_client.get("/api/state")
    assert resp.status_code == 200
    body = resp.json()

    assert body["sin"] is None
    zone_ids = sorted(z["zone_id"] for z in body["zones"])
    assert zone_ids == ["ANT", "BOG"]


# ---------------------------------------------------------------------------
# test_004 — EDGE: CORS preflight desde origin allow-listed (REQ-API-001)
# ---------------------------------------------------------------------------


async def test_004_cors_preflight_from_allow_listed_origin_succeeds(
    app_client, fakeredis_async_client
):
    """`OPTIONS /api/state` con `Origin: http://localhost:5173` debe
    responder 200 con `Access-Control-Allow-Origin` eco del origin.

    CORSMiddleware procesa preflight antes de llegar al router; no hace
    falta sembrar Redis para esta ruta. La fixture `app_client` basta."""
    # El origin debe estar en `settings.cors_origins` (default cubre :5173
    # + GH Pages URL). El header `Origin` activa el camino preflight.
    origin = "http://localhost:5173"
    resp = await app_client.options(
        "/api/state",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
        },
    )

    # Starlette/FastAPI devuelve 200 para preflight exitoso.
    assert resp.status_code in (200, 204)
    # El middleware eco el origin (no usa "*" cuando la lista es allow-list).
    allow_origin = resp.headers.get("Access-Control-Allow-Origin")
    assert allow_origin == origin
    # Los métodos permitidos incluyen GET (lo que el preflight pidió).
    allow_methods = resp.headers.get("Access-Control-Allow-Methods", "")
    assert "GET" in allow_methods.upper()
