"""
Tests del selector de fuente (T-PUB-008).

El caso que da nombre a la tarea es `test_tres_fallos_conmutan_a_simulador`:
es la garantía de que una caída de XM no deja el dashboard en blanco durante
la demo.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from common.data_source import DataSourceError
from common.models import DataSource
from common.redis_keys import (
    KEY_HEALTH_FAILURES,
    KEY_HEALTH_MODE,
    KEY_HEALTH_SOURCE_SWITCHES,
)
from publisher.normalizer import build_events
from publisher.simulator import SimulatorSource
from publisher.source_selector import SourceSelector

TS = datetime(2026, 9, 19, 17, 0, tzinfo=UTC)


class FakeXM:
    """Doble de `XMRealSource` con fallo conmutable."""

    name = "xm"

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0
        self.closed = False

    async def fetch_events(self):
        self.calls += 1
        if self.fail:
            raise DataSourceError("caída simulada", source="xm")
        return build_events(
            demanda_nacional_mw=10_000.0, fuente=DataSource.REAL, timestamp=TS
        )

    async def fetch_event(self):
        return (await self.fetch_events())[-1]

    async def health_check(self) -> bool:
        return not self.fail

    async def aclose(self) -> None:
        self.closed = True


class Reloj:
    """Reloj controlable: evita esperar 5 minutos reales por el backoff."""

    def __init__(self, ahora: datetime) -> None:
        self.ahora = ahora

    def __call__(self) -> datetime:
        return self.ahora

    def avanzar(self, segundos: float) -> None:
        self.ahora += timedelta(seconds=segundos)


def _selector(xm: FakeXM, *, redis=None, clock=None, force: str = "") -> SourceSelector:
    return SourceSelector(
        real=xm,
        simulator=SimulatorSource(),
        redis=redis,
        force_source=force,
        clock=clock,
    )


class TestFallback:
    async def test_arranca_en_modo_real(self):
        selector = _selector(FakeXM())
        assert selector.mode is DataSource.REAL

    async def test_tres_fallos_conmutan_a_simulador(self):
        xm = FakeXM(fail=True)
        selector = _selector(xm)

        for _ in range(3):
            await selector.fetch_events()

        assert selector.mode is DataSource.SIM
        assert selector.consecutive_failures == 3

    async def test_dos_fallos_no_conmutan(self):
        """El umbral es 3: un par de timeouts sueltos no cambian el modo."""
        selector = _selector(FakeXM(fail=True))

        for _ in range(2):
            await selector.fetch_events()

        assert selector.mode is DataSource.REAL
        assert selector.consecutive_failures == 2

    async def test_un_exito_reinicia_el_contador(self):
        xm = FakeXM(fail=True)
        selector = _selector(xm)

        await selector.fetch_events()
        await selector.fetch_events()
        xm.fail = False
        await selector.fetch_events()

        assert selector.consecutive_failures == 0
        assert selector.mode is DataSource.REAL

    async def test_el_ciclo_siempre_entrega_datos(self):
        """Aunque XM falle, el pipeline no puede quedarse seco."""
        selector = _selector(FakeXM(fail=True))

        eventos = await selector.fetch_events()

        assert len(eventos) == 6
        assert all(e.data.fuente is DataSource.SIM for e in eventos)

    async def test_los_datos_reales_se_marcan_como_reales(self):
        selector = _selector(FakeXM())
        eventos = await selector.fetch_events()
        assert all(e.data.fuente is DataSource.REAL for e in eventos)


class TestBackoff:
    async def test_no_reintenta_xm_antes_del_backoff(self):
        xm = FakeXM(fail=True)
        reloj = Reloj(TS)
        selector = _selector(xm, clock=reloj)

        for _ in range(3):
            await selector.fetch_events()
        llamadas_tras_conmutar = xm.calls

        await selector.fetch_events()

        assert xm.calls == llamadas_tras_conmutar

    async def test_reintenta_cuando_vence_el_backoff(self):
        xm = FakeXM(fail=True)
        reloj = Reloj(TS)
        selector = _selector(xm, clock=reloj)

        for _ in range(3):
            await selector.fetch_events()
        llamadas = xm.calls

        reloj.avanzar(301)
        await selector.fetch_events()

        assert xm.calls == llamadas + 1

    async def test_vuelve_a_real_cuando_xm_se_recupera(self):
        xm = FakeXM(fail=True)
        reloj = Reloj(TS)
        selector = _selector(xm, clock=reloj)

        for _ in range(3):
            await selector.fetch_events()
        assert selector.mode is DataSource.SIM

        xm.fail = False
        reloj.avanzar(301)
        eventos = await selector.fetch_events()

        assert selector.mode is DataSource.REAL
        assert all(e.data.fuente is DataSource.REAL for e in eventos)

    async def test_el_backoff_crece_5_10_20_30_minutos(self):
        xm = FakeXM(fail=True)
        reloj = Reloj(TS)
        selector = _selector(xm, clock=reloj)

        for _ in range(3):
            await selector.fetch_events()

        esperas = []
        for _ in range(4):
            esperas.append((selector._next_retry_at - reloj.ahora).total_seconds())
            reloj.avanzar(esperas[-1] + 1)
            await selector.fetch_events()

        assert esperas == [300, 600, 1200, 1800]

    async def test_el_backoff_no_pasa_del_tope(self):
        xm = FakeXM(fail=True)
        reloj = Reloj(TS)
        selector = _selector(xm, clock=reloj)

        for _ in range(3):
            await selector.fetch_events()
        for _ in range(8):
            reloj.avanzar(2000)
            await selector.fetch_events()

        espera = (selector._next_retry_at - reloj.ahora).total_seconds()
        assert espera <= 1800


class TestForceSource:
    async def test_force_simulator_nunca_consulta_xm(self):
        xm = FakeXM()
        selector = _selector(xm, force="simulator")

        eventos = await selector.fetch_events()

        assert xm.calls == 0
        assert all(e.data.fuente is DataSource.SIM for e in eventos)

    async def test_force_real_propaga_el_error(self):
        """En CI queremos que una API rota se vea, no que se enmascare."""
        selector = _selector(FakeXM(fail=True), force="real")

        with pytest.raises(DataSourceError):
            await selector.fetch_events()


class TestSalud:
    async def test_persiste_el_estado_en_redis(self, fakeredis_async_client):
        xm = FakeXM(fail=True)
        selector = _selector(xm, redis=fakeredis_async_client)

        for _ in range(3):
            await selector.fetch_events()

        assert await fakeredis_async_client.get(KEY_HEALTH_MODE) == "simulator"
        assert await fakeredis_async_client.get(KEY_HEALTH_FAILURES) == "3"
        assert await fakeredis_async_client.get(KEY_HEALTH_SOURCE_SWITCHES) == "1"

    async def test_modo_real_se_refleja_en_redis(self, fakeredis_async_client):
        selector = _selector(FakeXM(), redis=fakeredis_async_client)

        await selector.fetch_events()

        assert await fakeredis_async_client.get(KEY_HEALTH_MODE) == "real"
        assert await fakeredis_async_client.get(KEY_HEALTH_FAILURES) == "0"

    async def test_force_simulator_tambien_persiste_el_modo(self, fakeredis_async_client):
        """El banner del dashboard lee `health:mode` incluso en demo forzada.

        Sin esto la clave se queda vacía justo en el modo que se usa para
        presentar sin internet.
        """
        selector = _selector(FakeXM(), redis=fakeredis_async_client, force="simulator")

        await selector.fetch_events()

        assert await fakeredis_async_client.get(KEY_HEALTH_MODE) == "simulator"

    async def test_funciona_sin_redis(self):
        """Sin Redis el selector degrada, no explota."""
        selector = _selector(FakeXM(fail=True), redis=None)
        for _ in range(3):
            await selector.fetch_events()
        assert selector.mode is DataSource.SIM


class TestAvisoDeConmutacion:
    async def test_take_switch_notice_avisa_una_sola_vez(self):
        selector = _selector(FakeXM(fail=True))

        for _ in range(3):
            await selector.fetch_events()

        assert selector.take_switch_notice() is DataSource.SIM
        assert selector.take_switch_notice() is None

    async def test_sin_conmutacion_no_hay_aviso(self):
        selector = _selector(FakeXM())
        await selector.fetch_events()
        assert selector.take_switch_notice() is None


class TestCadencia:
    async def test_el_intervalo_depende_del_modo(self):
        selector = _selector(FakeXM(fail=True))
        assert selector.interval_seconds == 300  # modo real: XM publica cada 5 min

        for _ in range(3):
            await selector.fetch_events()

        assert selector.interval_seconds == 5  # simulador: datos nuevos cada tick
