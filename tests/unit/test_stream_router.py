"""Tests for `api.routers.stream` (REQ-API-007 de spec #510).

Convención STANDARD: TDD laxo, tests con código OK.

Cobertura:
- test_001 HAPPY: el cliente recibe un `event: tick` en <1s tras publicar
  al canal pubsub.
- test_002 CLEANUP: cuando el cliente cierra el stream mid-flight, el
  pubsub per-connection es `aclose()`-ado (R1/R5 del risk matrix). El
  test monkey-patchea `redis.asyncio.client.PubSub.aclose` para spiar
  llamadas y verifica que ocurrió al menos una vez con la desconexión.

Estrategia:
- `fakeredis_async_client` fixture de conftest (PR-A): mismo cliente que
  se inyecta al SSE generator vía `dependency_overrides`, así el pubsub
  que el server abre y el `publish` del test comparten state.
- `FastASGITransport` (definido abajo): variante de `httpx.ASGITransport`
  que retorna la response en cuanto se manda `http.response.start` y
  streamea los body chunks por una cola. Sin esto, `ASGITransport`
  awaits `self.app(scope, receive, send)` que se queda colgado porque
  sse-starlette corre un task group infinito (ver `sse_starlette/sse.py`
  `EventSourceResponse.__call__`). El auto-signaling de disconnect en
  `aclose()` permite que el generator limpie el pubsub al cerrar el
  stream — necesario para test_002.

Notas operativas:
- fakeredis 2.38 soporta async pub/sub; verify: una sola `FakeRedis`
  instance entrega mensajes cross-pubsub (connection_pool compartido).
- `sse-starlette` 2.x emite comentarios `:` como `: heartbeat\\n\\n`
  y eventos como `event: X\\ndata: Y\\n\\n`.
- `AppStatus.should_exit_event` es state módulo-nivel en sse-starlette
  bound al event loop del primer test que toca SSE; el autouse fixture
  lo resetea a `None` antes de cada test para que se cree fresco en el
  loop actual (pytest-asyncio mode auto + function-scope loop).
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest
import pytest_asyncio

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Autouse fixture: reset sse-starlette module-level anyio.Event
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_sse_starlette_app_status():
    """Reset `sse_starlette.sse.AppStatus.should_exit_event = None` so it
    gets re-created fresh in the current event loop.

    Why: sse-starlette holds the event as module/class state. Without this,
    tests #2+ that open an SSE stream fail with
    `RuntimeError: <Event> is bound to a different event loop`
    because the cached event was bound to a previous test's loop.
    """
    from sse_starlette.sse import AppStatus

    AppStatus.should_exit_event = None
    yield
    # No teardown: next test resets again.


# ---------------------------------------------------------------------------
# FastASGITransport — local helper, no conftest change needed
# ---------------------------------------------------------------------------


class FastASGITransport(httpx.ASGITransport):
    """`httpx.ASGITransport` modificado para que devuelva la response
    apenas el ASGI app mande `http.response.start`, en lugar de esperar
    a que `__call__` retorne (cosa que sse-starlette nunca hace porque
    su task group corre indefinidamente).

    Body chunks se encolan en un `asyncio.Queue` que un stream async
    consume desde el lado del cliente.

    Cuando el cliente cierra la response (vía `async with` exit o
    `response.aclose()`), el stream llama `aclose()` que dispara
    `_client_disconnected.set()`. Esto hace que la próxima llamada al
    `receive()` del ASGI retorne `http.disconnect`, lo que dispara el
    cancel en `_listen_for_disconnect` de sse-starlette, que cancela
    el task group, lo que cancela el generator, que ejecuta su `finally`
    y cierra el pubsub. Necesario para test_002.
    """

    def __init__(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        super().__init__(*args, **kwargs)
        self._client_disconnected: asyncio.Event = asyncio.Event()

    async def handle_async_request(self, request):  # type: ignore[no-untyped-def]
        assert isinstance(request.stream, httpx.AsyncByteStream)
        # Build scope (same shape as httpx ASGITransport).
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": request.method,
            "headers": [(k.lower(), v) for k, v in request.headers.raw],
            "scheme": request.url.scheme,
            "path": request.url.path,
            "raw_path": request.url.raw_path.split(b"?")[0],
            "query_string": request.url.query,
            "server": (request.url.host, request.url.port),
            "client": self.client,
            "root_path": self.root_path,
        }

        request_body_chunks = request.stream.__aiter__()
        request_complete = False
        client_disconnected = self._client_disconnected

        response_started = asyncio.Event()
        body_queue: asyncio.Queue = asyncio.Queue()
        state: dict = {"status_code": None, "response_headers": None}

        async def receive() -> dict:  # type: ignore[type-arg]
            nonlocal request_complete
            if client_disconnected.is_set():
                return {"type": "http.disconnect"}
            if request_complete:
                # Hold the receive open until the client signals disconnect.
                await client_disconnected.wait()
                return {"type": "http.disconnect"}
            try:
                body = await request_body_chunks.__anext__()
            except StopAsyncIteration:
                request_complete = True
                await client_disconnected.wait()
                return {"type": "http.disconnect"}
            return {"type": "http.request", "body": body, "more_body": True}

        async def send(message):  # type: ignore[no-untyped-def]
            if message["type"] == "http.response.start":
                state["status_code"] = message["status"]
                state["response_headers"] = message.get("headers", [])
                response_started.set()
            elif message["type"] == "http.response.body":
                body = message.get("body", b"")
                more_body = message.get("more_body", False)
                if body:
                    await body_queue.put(body)
                if not more_body:
                    await body_queue.put(None)  # sentinel

        # Spawn the app in a background task — DON'T await it directly.
        app_task = asyncio.create_task(self.app(scope, receive, send))

        # Wait for response.start (with a safety timeout — the SSE generator
        # subscribes before yielding, which takes a few ms in fakeredis).
        try:
            await asyncio.wait_for(response_started.wait(), timeout=3.0)
        except TimeoutError:
            app_task.cancel()
            raise

        class _QueueStream(httpx.AsyncByteStream):
            """Stream que consume `body_queue` y, al cerrarse, señaliza
            disconnect para que el app_task del ASGI pueda cancelarse
            limpio y el generator corra su `finally`."""

            def __init__(self, q, task, disconnect_event):
                self._q = q
                self._task = task
                self._disconnect = disconnect_event

            async def __aiter__(self):  # type: ignore[no-untyped-def]
                while True:
                    chunk = await self._q.get()
                    if chunk is None:
                        break
                    yield chunk

            async def aclose(self) -> None:  # type: ignore[no-untyped-def]
                # Signal disconnect so receive() returns http.disconnect.
                self._disconnect.set()
                # Cancel app task if still running. Cleanup happens in
                # `finally` blocks inside sse-starlette and our generator.
                if not self._task.done():
                    self._task.cancel()
                    try:
                        await self._task
                    except (asyncio.CancelledError, Exception):  # noqa: BLE001
                        pass

        stream = _QueueStream(body_queue, app_task, self._client_disconnected)
        return httpx.Response(
            state["status_code"],
            headers=dict(state["response_headers"] or {}),
            stream=stream,
        )


# ---------------------------------------------------------------------------
# Helpers — wire format del publisher
# ---------------------------------------------------------------------------


def _tick_payload(entity_id: str = "SIN") -> dict[str, object]:
    """Fabrica el dict de un tick en el wire format del publisher.

    Debe matchear `publisher.main._aplanar(evento)` para que el SSE
    generator despache como `event: tick`. Los valores numéricos son
    strings (Redis hash limitation) menos donde el JSON acepta nativos.
    """
    return {
        "type": "tick",
        "entity_id": entity_id,
        "timestamp": "2026-09-21T12:00:00+00:00",
        "latitude": "4.5",
        "longitude": "-74.1",
        "demanda_mw": "10500.0",
        "generacion_mw": "10800.0",
        "generacion_solar_mw": "200.0",
        "generacion_eolica_mw": "50.0",
        "generacion_hidraulica_mw": "6500.0",
        "generacion_termica_mw": "4050.0",
        "fuente": "simulator",
    }


async def _publish_tick_via_fakeredis(fakeredis_async_client, payload: dict) -> None:
    """Simula el `_publicar` del publisher: PUBLISH al canal."""
    from common.redis_keys import PUBSUB_CHANNEL_ENERGY

    await fakeredis_async_client.publish(PUBSUB_CHANNEL_ENERGY, json.dumps(payload))


# ---------------------------------------------------------------------------
# Fixture: app + AsyncClient con FastASGITransport
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def stream_client(fakeredis_async_client):  # noqa: ANN001
    """AsyncClient envuelto en `FastASGITransport` para tests SSE.

    Crea una app fresca (un cliente por test = estado limpio), reemplaza
    `get_redis` por el `fakeredis_async_client` del fixture de conftest,
    y devuelve el `AsyncClient` listo para `client.stream("GET",
    "/api/stream")`.
    """
    from api.dependencies import get_redis
    from api.server import create_app

    app = create_app()
    app.dependency_overrides[get_redis] = lambda: fakeredis_async_client

    transport = FastASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    # Tear down overrides explicitly (FastASGITransport's app_task is
    # already cancelled by the stream's aclose on `async with` exit).
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# test_001 — HAPPY: tick publicado al pubsub llega al cliente SSE en <1s
# ---------------------------------------------------------------------------


async def test_001_stream_publishes_tick_received_by_client_under_1s(
    stream_client, fakeredis_async_client
):
    """El cliente conectado a `/api/stream` recibe un `event: tick` con
    el JSON del payload publicado en `energy-events`, dentro de 1s.

    Estrategia: abrimos el stream, esperamos a que el generator se
    suscriba (~200ms), publicamos un tick, y leemos los body chunks
    hasta tener un evento SSE completo (termina con `\\n\\n`).
    """
    payload = _tick_payload(entity_id="SIN")
    expected_data = json.dumps(payload, ensure_ascii=False)

    start = asyncio.get_event_loop().time()

    async with stream_client.stream("GET", "/api/stream") as response:
        assert response.status_code == 200, (
            f"stream status was {response.status_code}, expected 200"
        )
        assert response.headers["content-type"].startswith("text/event-stream"), (
            f"content-type was {response.headers['content-type']!r}"
        )

        # Esperar a que el generator se suscriba al canal. fakeredis
        # propaga el subscribe ack de inmediato; ~200ms es seguro.
        await asyncio.sleep(0.2)

        # Publicar el tick DESPUÉS de la suscripción (clave para que
        # el mensaje no se pierda).
        await _publish_tick_via_fakeredis(fakeredis_async_client, payload)

        # Leer hasta tener un evento SSE completo (termina con \r\n\r\n,
        # que sse-starlette usa como separador de eventos en lugar de \n\n).
        buffer = b""
        async with asyncio.timeout(2.0):
            async for chunk in response.aiter_bytes():
                buffer += chunk
                if b"\r\n\r\n" in buffer:
                    break
    elapsed = asyncio.get_event_loop().time() - start

    text = buffer.decode("utf-8", errors="replace")
    # Verificar formato SSE: `event: tick` + `data: <json>`.
    assert "event: tick" in text, f"evento 'tick' no presente en: {text!r}"
    assert expected_data in text, (
        f"data recibido no matchea el payload publicado.\n"
        f"esperado: {expected_data!r}\n"
        f"recibido: {text!r}"
    )
    # SLA del spec REQ-API-007 scenario 1: cliente recibe en <1s.
    # Damos margen al setup de la conexión (200ms subscribe + ~100ms publish).
    assert elapsed < 1.5, f"elapsed {elapsed:.2f}s exceeds 1.5s budget"


# ---------------------------------------------------------------------------
# test_002 — CLEANUP: disconnect mid-flight cierra el pubsub (R1/R5)
# ---------------------------------------------------------------------------


async def test_002_stream_disconnect_mid_flight_closes_pubsub_via_aclose(
    stream_client, fakeredis_async_client
):
    """Cuando el cliente cierra el stream antes de consumir todos los
    eventos, el pubsub per-connection es cerrado (R1/R5: sin leak de
    conexiones Redis hacia suscriptores lentos o que desconectan).

    Estrategia: monkey-patch `redis.asyncio.client.PubSub.aclose` con
    un spy que registra llamadas, abrimos el stream, esperamos a que el
    generator se suscriba, salimos del `async with` (lo que dispara el
    aclose del stream que señala disconnect), esperamos al cleanup, y
    verificamos que el spy recibió al menos una llamada.
    """
    from redis.asyncio.client import PubSub

    # --- Spy setup --------------------------------------------------------
    aclose_calls: list[PubSub] = []
    original_aclose = PubSub.aclose

    async def spy_aclose(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        aclose_calls.append(self)
        return await original_aclose(self, *args, **kwargs)

    PubSub.aclose = spy_aclose  # type: ignore[method-assign]
    try:
        # --- Open stream -------------------------------------------------
        async with stream_client.stream("GET", "/api/stream") as response:
            assert response.status_code == 200

            # Esperar a que el generator haya creado su pubsub y se
            # haya suscrito al canal. ~200ms es suficiente en fakeredis.
            await asyncio.sleep(0.25)
            assert len(aclose_calls) == 0, (
                f"aclose llamado prematuramente: {len(aclose_calls)} veces"
            )

            # --- Disconnect mid-flight -----------------------------------
            # Salir del `async with` cierra el stream → FastASGITransport
            # señala disconnect → sse-starlette cancela tasks → el
            # generator corre su `finally` → pubsub.aclose().
        # A este punto, el `async with` salió → stream cerrado.

        # --- Wait for cleanup --------------------------------------------
        # El generator corre en una task de sse-starlette. El finally
        # debería ejecutarse prácticamente inmediato tras el cancel, pero
        # le damos margen para evitar flake en CI.
        for _ in range(30):  # hasta 3s total
            if len(aclose_calls) >= 1:
                break
            await asyncio.sleep(0.1)

        assert len(aclose_calls) >= 1, (
            f"PubSub.aclose no fue llamado tras disconnect mid-flight. "
            f"spy={len(aclose_calls)} llamadas. "
            f"R1/R5: conexión Redis leakada."
        )
    finally:
        # --- Restore -----------------------------------------------------
        PubSub.aclose = original_aclose  # type: ignore[method-assign]
