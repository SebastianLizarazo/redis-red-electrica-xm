"""
Tests del cliente XM (T-PUB-007), con `respx` interceptando httpx.

Lo que se verifica aquí no es que httpx funcione, sino la política: qué se
reintenta, qué no, y en qué excepción del dominio acaba cada fallo.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from common.data_source import (
    DataSourceError,
    DataSourceTimeoutError,
    InvalidXMResponseError,
)
from common.models import DataSource
from publisher.xm_client import XMRealSource

URL = "https://xm.example.test/DemandaTiempoReal"

PAYLOAD_OK = {
    "Nombre": "Demanda en tiempo real",
    "Variables": [
        {
            "Nombre": "Tiempo real",
            "UnidadMedida": "MW",
            "Datos": [
                {"Fecha": "2026-09-19T17:00:00", "Valor": 10_100.0},
                {"Fecha": "2026-09-19T17:05:00", "Valor": 10_185.6},
                {"Fecha": "2026-09-19T17:10:00", "Valor": None},
            ],
        }
    ],
}


def _source(**kwargs) -> XMRealSource:
    # backoff 0 para que los tests de reintento no duren segundos.
    kwargs.setdefault("backoff_base_seconds", 0.0)
    return XMRealSource(url=URL, **kwargs)


class TestFetchEvents:
    @respx.mock
    async def test_devuelve_seis_eventos_marcados_como_reales(self):
        respx.get(URL).mock(return_value=httpx.Response(200, json=PAYLOAD_OK))
        source = _source()

        eventos = await source.fetch_events()
        await source.aclose()

        assert len(eventos) == 6
        assert [e.entity_id for e in eventos][-1] == "SIN"
        assert all(e.data.fuente is DataSource.REAL for e in eventos)
        assert eventos[-1].data.demanda_mw == pytest.approx(10_185.6, abs=0.5)

    @respx.mock
    async def test_fetch_event_devuelve_solo_el_sin(self):
        respx.get(URL).mock(return_value=httpx.Response(200, json=PAYLOAD_OK))
        source = _source()

        evento = await source.fetch_event()
        await source.aclose()

        assert evento is not None
        assert evento.entity_id == "SIN"


class TestPoliticaDeReintentos:
    @respx.mock
    async def test_timeout_agota_reintentos_y_lanza_timeout_error(self):
        ruta = respx.get(URL).mock(side_effect=httpx.ConnectTimeout("timeout"))
        source = _source(max_retries=3)

        with pytest.raises(DataSourceTimeoutError):
            await source.fetch_events()
        await source.aclose()

        assert ruta.call_count == 3

    @respx.mock
    async def test_error_500_se_reintenta(self):
        ruta = respx.get(URL).mock(return_value=httpx.Response(503))
        source = _source(max_retries=3)

        with pytest.raises(DataSourceError):
            await source.fetch_events()
        await source.aclose()

        assert ruta.call_count == 3

    @respx.mock
    async def test_error_404_no_se_reintenta(self):
        """Un 4xx es determinista: reintentar solo retrasa el fallback."""
        ruta = respx.get(URL).mock(return_value=httpx.Response(404))
        source = _source(max_retries=3)

        with pytest.raises(DataSourceError, match="no se reintenta"):
            await source.fetch_events()
        await source.aclose()

        assert ruta.call_count == 1

    @respx.mock
    async def test_se_recupera_si_un_intento_intermedio_funciona(self):
        ruta = respx.get(URL).mock(
            side_effect=[
                httpx.Response(503),
                httpx.Response(200, json=PAYLOAD_OK),
            ]
        )
        source = _source(max_retries=3)

        eventos = await source.fetch_events()
        await source.aclose()

        assert len(eventos) == 6
        assert ruta.call_count == 2

    @respx.mock
    async def test_error_de_red_se_convierte_en_datasource_error(self):
        respx.get(URL).mock(side_effect=httpx.ConnectError("sin ruta al host"))
        source = _source(max_retries=1)

        with pytest.raises(DataSourceError) as exc_info:
            await source.fetch_events()
        await source.aclose()

        assert exc_info.value.source == "xm"


class TestRespuestasMalformadas:
    @respx.mock
    async def test_cuerpo_no_json(self):
        respx.get(URL).mock(return_value=httpx.Response(200, text="<html>mantenimiento</html>"))
        source = _source()

        with pytest.raises(InvalidXMResponseError, match="no es JSON"):
            await source.fetch_events()
        await source.aclose()

    @respx.mock
    async def test_json_sin_la_estructura_esperada_no_se_reintenta(self):
        ruta = respx.get(URL).mock(return_value=httpx.Response(200, json={"otra": "cosa"}))
        source = _source(max_retries=3)

        with pytest.raises(InvalidXMResponseError):
            await source.fetch_events()
        await source.aclose()

        assert ruta.call_count == 1


class TestHealthCheck:
    @respx.mock
    async def test_ok_cuando_xm_responde(self):
        respx.head(URL).mock(return_value=httpx.Response(200))
        source = _source()

        assert await source.health_check() is True
        await source.aclose()

    @respx.mock
    async def test_falso_cuando_xm_esta_caida(self):
        respx.head(URL).mock(side_effect=httpx.ConnectError("caída"))
        source = _source()

        assert await source.health_check() is False
        await source.aclose()

    @respx.mock
    async def test_falso_con_error_de_servidor(self):
        respx.head(URL).mock(return_value=httpx.Response(500))
        source = _source()

        assert await source.health_check() is False
        await source.aclose()
