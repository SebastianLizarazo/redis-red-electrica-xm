"""
Tests unitarios de `publisher/main.py` (T-PHB-007, R3-001).

Cubre el contrato de las 5 REQs del spec obs #478:
- REQ-PHB-001 : redacción de URL con userinfo embebido (test_001).
- REQ-PHB-002 : shape de `_aplanar`, branch SIN-key, payload Pub/Sub
                (test_002, test_003, test_004).
- REQ-PHB-003 : aislamiento por evento (test_006).
- REQ-PHB-004 : atomicidad del pipeline `transaction=True` (test_007).
- REQ-PHB-005 : source-switch publica sin tocar streams ni hashes (test_005).

Fixtures reutilizados de `tests/conftest.py` (pin RISK-N2; los números
de línea reflejan el estado del conftest al cierre de PR-A, ya merged):
    fakeredis_client         -> conftest.py:41
    fakeredis_async_client   -> conftest.py:65
    event_sample             -> conftest.py:155
    zone_event_sample        -> conftest.py:174

Orden de tests en el archivo (RISK-N3, lockdown PR-A apply-progress):
shape ANTES que behavior. `test_002_aplanar_devuelve_todo_string` corre
primero: si PR-A hubiera mutado la semántica de `_aplanar`, este test
falla primero y expone el cambio antes de que los behavior tests
pasen contra suposiciones equivocadas.

Total: 7 funciones (lockdown RISK-4: ni una más, ni una menos). Las
variantes de `test_001_redacta_url_con_password` se generan vía
`pytest.mark.parametrize`; pytest cuenta cada caso como item separado.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from common.models import DataSource, Event
from common.redis_keys import (
    KEY_STATE_SIN,
    PUBSUB_CHANNEL_ENERGY,
    STREAM_ENERGY,
    state_zone_key,
)
from publisher.main import Publisher, _aplanar, _safe_redis_url


# ----------------------------------------------------------------------
# Helpers locales (no se exportan)
# ----------------------------------------------------------------------


class _StubSelector:
    """Selector de fuente mínimo para instanciar `Publisher` en los tests.

    Solo necesitamos que `_ciclo` reciba una lista determinista de eventos
    y `take_switch_notice` devuelva `None` (excepto donde se quiera
    forzar el path de source-switch, cubierto en test_005 con un stub
    dedicado más abajo).
    """

    def __init__(self, eventos: list[Event], *, switch_notice: DataSource | None = None) -> None:
        self._eventos = list(eventos)
        self.mode = DataSource.REAL
        self.interval_seconds = 5
        self.consecutive_failures = 0
        self._switch_notice = switch_notice

    async def fetch_events(self):  # noqa: D401 - match SourceSelector protocol
        return list(self._eventos)

    def take_switch_notice(self):
        notice = self._switch_notice
        self._switch_notice = None
        return notice

    async def aclose(self) -> None:  # noqa: D401
        return None


# ======================================================================
# Test 002 — FIRST (RISK-N3). Shape de `_aplanar` antes que behavior.
# ======================================================================


def test_002_aplanar_devuelve_todo_string(event_sample: Event) -> None:
    """`_aplanar` debe emitir SIEMPRE valores `str` parseables.

    Redis Streams y Hashes no admiten valores anidados (ni dict, ni
    list, ni float). Si `_aplanar` se rompe y empieza a emitir floats
    o dicts, los behavior tests (test_003..007) podrían pasar contra
    suposiciones equivocadas; este test corre PRIMERO para forzar el
    fail-fast.
    """
    plano = _aplanar(event_sample)

    # 1) Todo valor es str (constraint duro de Redis Streams/Hashes).
    bad = [(k, type(v).__name__) for k, v in plano.items() if not isinstance(v, str)]
    assert not bad, f"_aplanar emitió valores no-str: {bad}"

    # 2) Las 11 claves canónicas están presentes y nada más.
    expected_keys = {
        "entity_id",
        "timestamp",
        "latitude",
        "longitude",
        "demanda_mw",
        "generacion_mw",
        "generacion_solar_mw",
        "generacion_eolica_mw",
        "generacion_hidraulica_mw",
        "generacion_termica_mw",
        "fuente",
    }
    assert set(plano.keys()) == expected_keys

    # 3) Round-trip del Event original via `Event.model_validate_json`.
    #    Esto blinda el contrato upstream que consume el subscriber.
    parsed = Event.model_validate_json(event_sample.model_dump_json())
    assert parsed == event_sample

    # 4) Cada valor del flat dict debe sobrevivir un parse JSON sin
    #    que se altere (los floats serializados como "10500.0" vuelven
    #    a su forma canónica tras json.loads).
    for value in plano.values():
        json_round_trip = json.loads(json.dumps(value))
        assert json_round_trip == value


# ======================================================================
# Test 001 — Redacción de URL con userinfo embebido (REQ-PHB-001).
# ======================================================================


@pytest.mark.parametrize(
    "raw, expected",
    [
        pytest.param(
            "redis://:hunter2@host:6379/0",
            "redis://host:6379/0",
            id="password-after-colon",
        ),
        pytest.param(
            "redis://user:secret@redis:6379/0",
            "redis://redis:6379/0",
            id="user-and-password",
        ),
        pytest.param(
            "redis://[::1]:6379/0",
            "redis://[::1]:6379/0",
            id="ipv6-with-port",
        ),
        pytest.param(
            "redis://localhost",
            "redis://localhost",
            id="missing-port",
        ),
        pytest.param(
            "redis://redis:6379/0",
            "redis://redis:6379/0",
            id="no-credentials-passthrough",
        ),
    ],
)
def test_001_redacta_url_con_password(raw: str, expected: str) -> None:
    """REQ-PHB-001: `_safe_redis_url` quita userinfo, preserva host/port/path."""
    redacted = _safe_redis_url(raw)

    assert redacted == expected
    assert "@" not in redacted


# ======================================================================
# Test 003 — Branch SIN: `state:sin` y NO `state:zone:SIN`.
# ======================================================================


async def test_003_sin_evento_usa_state_sin(
    fakeredis_async_client,
    event_sample: Event,
) -> None:
    """REQ-PHB-002: un Event SIN debe escribir a `KEY_STATE_SIN`, no a
    `state_zone_key("SIN")` (que devolvería `state:zone:SIN` — incorrecto).
    """
    publisher = Publisher(
        redis_client=fakeredis_async_client,
        selector=_StubSelector([event_sample]),
    )

    await publisher._publicar(event_sample)

    assert await fakeredis_async_client.exists(KEY_STATE_SIN) == 1
    assert await fakeredis_async_client.exists(state_zone_key("SIN")) == 0


# ======================================================================
# Test 004 — Pub/Sub payload shape (`type == "tick"`).
# ======================================================================


async def test_004_pubsub_lleva_type_tick(
    monkeypatch,
    fakeredis_async_client,
    event_sample: Event,
) -> None:
    """REQ-PHB-002: el mensaje Pub/Sub lleva `type == "tick"` y los
    campos del Event (entity_id, timestamp, EventData).
    """
    publisher = Publisher(
        redis_client=fakeredis_async_client,
        selector=_StubSelector([event_sample]),
    )

    # Capturamos el `publish` que el pipe encola. Stub pipeline con
    # execute() no-op: solo nos importa el payload del publish.
    published_payloads: list[tuple[str, str]] = []

    class _CapturingPipeline:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def publish(self, channel: str, message: str):
            published_payloads.append((channel, message))
            return self

        def xadd(self, *args: Any, **kwargs: Any):
            return self

        def hset(self, *args: Any, **kwargs: Any):
            return self

        def expire(self, *args: Any, **kwargs: Any):
            return self

        async def execute(self):
            return [None, None, None, None]

    monkeypatch.setattr(
        fakeredis_async_client, "pipeline", lambda *a, **kw: _CapturingPipeline()
    )

    await publisher._publicar(event_sample)

    assert len(published_payloads) == 1
    channel, message = published_payloads[0]

    # El canal canónico.
    assert channel == PUBSUB_CHANNEL_ENERGY

    # El payload es JSON válido y arranca con `type == "tick"`.
    parsed = json.loads(message)
    assert parsed["type"] == "tick"

    # Contiene los campos del Event (entity_id, timestamp, location, data).
    assert parsed["entity_id"] == event_sample.entity_id
    # Pydantic v2 emite UTC como sufijo `Z` mientras `datetime.isoformat()`
    # lo emite como `+00:00`. Comparamos contra la forma canónica del
    # model_dump JSON para no atar el test al detalle del serializer.
    assert parsed["timestamp"] == event_sample.model_dump(mode="json")["timestamp"]
    assert parsed["data"]["demanda_mw"] == event_sample.data.demanda_mw
    assert parsed["data"]["generacion_mw"] == event_sample.data.generacion_mw
    assert parsed["data"]["fuente"] == event_sample.data.fuente.value


# ======================================================================
# Test 005 — Source-switch: PUBLISH only (no xadd, no hset).
# ======================================================================


async def test_005_source_switch_no_xaddea(
    monkeypatch,
    fakeredis_async_client,
) -> None:
    """REQ-PHB-005: `_publicar_source_switch` debe emitir SOLO un
    `publish` (Pub/Sub). NUNCA debe tocar `xadd` ni `hset`.
    """
    publisher = Publisher(
        redis_client=fakeredis_async_client,
        selector=_StubSelector([], switch_notice=DataSource.SIM),
    )

    # Capturamos publish, xadd y hset sobre el cliente Redis.
    published: list[tuple[str, str]] = []
    xadd_calls: list[tuple[Any, ...]] = []
    hset_calls: list[tuple[Any, ...]] = []

    async def stub_publish(channel: str, message: str) -> int:
        published.append((channel, message))
        return 1

    async def stub_xadd(*args: Any, **kwargs: Any):
        xadd_calls.append((args, kwargs))
        return b"0-0"

    async def stub_hset(*args: Any, **kwargs: Any):
        hset_calls.append((args, kwargs))
        return 1

    monkeypatch.setattr(fakeredis_async_client, "publish", stub_publish)
    monkeypatch.setattr(fakeredis_async_client, "xadd", stub_xadd)
    monkeypatch.setattr(fakeredis_async_client, "hset", stub_hset)

    await publisher._publicar_source_switch(DataSource.SIM)

    # Hubo exactamente un publish al canal canónico, con type=source_switch.
    assert len(published) == 1
    channel, message = published[0]
    assert channel == PUBSUB_CHANNEL_ENERGY
    parsed = json.loads(message)
    assert parsed["type"] == "source_switch"
    assert parsed["mode"] == DataSource.SIM.value

    # NUNCA se llamó a xadd ni hset en el path de source-switch.
    assert xadd_calls == []
    assert hset_calls == []


# ======================================================================
# Test 006 — Aislamiento por evento (REQ-PHB-003).
# ======================================================================


async def test_006_per_event_isolation(
    monkeypatch,
    fakeredis_async_client,
    event_sample: Event,
) -> None:
    """REQ-PHB-003: si `_publicar` levanta en el evento 3 de 6, los
    eventos 1, 2, 4, 5, 6 deben seguir publicándose y el contador
    `_event_failures` debe terminar en 1.
    """
    publisher = Publisher(
        redis_client=fakeredis_async_client,
        selector=_StubSelector([event_sample] * 6),
    )

    call_log: list[int] = []
    success_log: list[int] = []

    async def stub_publicar(evento: Event) -> None:
        call_log.append(len(success_log) + len(call_log) + 1)
        if len(call_log) == 3:
            raise ConnectionError("simulated failure on event 3")
        success_log.append(len(call_log))

    monkeypatch.setattr(publisher, "_publicar", stub_publicar)

    # El ciclo NO debe levantar: el try/except por evento lo absorbe.
    await publisher._ciclo()

    # Los 6 eventos fueron intentados.
    assert len(call_log) == 6

    # 5 publicaron con éxito, 1 levantó.
    assert len(success_log) == 5

    # El contador de fallos es exactamente 1.
    assert publisher._event_failures == 1


# ======================================================================
# Test 007 — Atomicidad del pipeline `transaction=True`.
# ======================================================================


async def test_007_pipeline_atomico_o_nada(
    monkeypatch,
    fakeredis_async_client,
    event_sample: Event,
) -> None:
    """REQ-PHB-004: `_publicar` debe abrir el pipeline con
    `transaction=True` y, ante un fallo en `execute()`, ninguna key
    debe quedar escrita (atomicidad all-or-nothing).
    """
    publisher = Publisher(
        redis_client=fakeredis_async_client,
        selector=_StubSelector([event_sample]),
    )

    pipeline_kwargs: list[dict[str, Any]] = []
    queued_commands: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    class _FailingPipeline:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pipeline_kwargs.append(kwargs)

        def publish(self, channel: str, message: str):
            queued_commands.append(("publish", (channel, message), {}))
            return self

        def xadd(self, *args: Any, **kwargs: Any):
            queued_commands.append(("xadd", args, kwargs))
            return self

        def hset(self, *args: Any, **kwargs: Any):
            queued_commands.append(("hset", args, kwargs))
            return self

        def expire(self, *args: Any, **kwargs: Any):
            queued_commands.append(("expire", args, kwargs))
            return self

        async def execute(self):
            # Simulamos la caída entre la cola y el commit server-side.
            # En `transaction=True` el server haría rollback; en
            # fakeredis la stub ni siquiera toca el store.
            raise ConnectionError("simulated mid-execute failure")

    monkeypatch.setattr(
        fakeredis_async_client, "pipeline", lambda *a, **kw: _FailingPipeline(*a, **kw)
    )

    # Pre-condición: nada escrito todavía.
    assert await fakeredis_async_client.exists(KEY_STATE_SIN) == 0
    assert await fakeredis_async_client.xlen(STREAM_ENERGY) == 0

    # La llamada a `_publicar` levanta el ConnectionError.
    with pytest.raises(ConnectionError, match="simulated"):
        await publisher._publicar(event_sample)

    # 1) El pipeline se abrió con `transaction=True` (literal kwarg).
    assert len(pipeline_kwargs) == 1
    assert pipeline_kwargs[0].get("transaction") is True

    # 2) Se encolaron los 4 comandos esperados, en orden.
    cmd_names = [c[0] for c in queued_commands]
    assert cmd_names == ["publish", "xadd", "hset", "expire"]

    # 3) El comando `hset` apuntó a `KEY_STATE_SIN` (branch SIN), no a
    #    `state_zone_key("SIN")` — refuerza la cobertura de test_003.
    hset_cmd = next(c for c in queued_commands if c[0] == "hset")
    hset_key = hset_cmd[1][0]
    assert hset_key == KEY_STATE_SIN

    # 4) Atomicidad: tras el raise, NADA quedó escrito en Redis.
    #    - state:sin no existe.
    #    - el stream energy:stream está vacío.
    assert await fakeredis_async_client.exists(KEY_STATE_SIN) == 0
    assert await fakeredis_async_client.xlen(STREAM_ENERGY) == 0
