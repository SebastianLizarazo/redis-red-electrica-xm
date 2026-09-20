"""
Tests del normalizador (T-PUB-006).

Los casos de `extract_latest_demand` están calcados de la respuesta real del
endpoint de XM, incluida la trampa del último punto con `"Valor": null`.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from common.data_source import InvalidXMResponseError
from common.models import DataSource
from publisher.normalizer import (
    COLOMBIA_TZ,
    ZONE_PROFILES,
    build_events,
    extract_latest_demand,
)


def _payload(datos: list[dict], *, unidad: str = "MW") -> dict:
    """Respuesta XM mínima con la misma forma que la real."""
    return {
        "Nombre": "Demanda en tiempo real",
        "Resolucion": "Horaria",
        "Variables": [
            {"Nombre": "Tiempo real", "UnidadMedida": unidad, "Datos": datos},
            {
                "Nombre": "Pronóstico",
                "UnidadMedida": "MW",
                "Datos": [{"Fecha": "2026-09-19T23:55:00-05:00", "Valor": 9716.5}],
            },
        ],
    }


class TestExtractLatestDemand:
    def test_toma_la_ultima_lectura_valida(self):
        payload = _payload(
            [
                {"Fecha": "2026-09-19T17:00:00", "Valor": 10100.0},
                {"Fecha": "2026-09-19T17:05:00", "Valor": 10185.6},
            ]
        )
        lectura = extract_latest_demand(payload)
        assert lectura.demanda_mw == pytest.approx(10185.6)

    def test_ignora_el_punto_final_nulo(self):
        """XM deja la muestra en curso con `Valor: null`.

        Tomar `Datos[-1]` a ciegas devolvería None y rompería aguas abajo.
        """
        payload = _payload(
            [
                {"Fecha": "2026-09-19T17:05:00", "Valor": 10185.6},
                {"Fecha": "2026-09-19T17:10:00", "Valor": None},
            ]
        )
        assert extract_latest_demand(payload).demanda_mw == pytest.approx(10185.6)

    def test_ignora_varios_nulos_al_final(self):
        payload = _payload(
            [
                {"Fecha": "2026-09-19T17:00:00", "Valor": 9900.0},
                {"Fecha": "2026-09-19T17:05:00", "Valor": None},
                {"Fecha": "2026-09-19T17:10:00", "Valor": None},
            ]
        )
        assert extract_latest_demand(payload).demanda_mw == pytest.approx(9900.0)

    def test_no_usa_la_serie_de_pronostico(self):
        """El pronóstico no es una medición: publicarlo sería mentir."""
        payload = {
            "Variables": [
                {"Nombre": "Tiempo real", "UnidadMedida": "MW", "Datos": [
                    {"Fecha": "2026-09-19T17:00:00", "Valor": 10100.0}
                ]},
                {"Nombre": "Pronóstico", "UnidadMedida": "MW", "Datos": [
                    {"Fecha": "2026-09-19T18:00:00", "Valor": 4321.0}
                ]},
            ]
        }
        assert extract_latest_demand(payload).demanda_mw == pytest.approx(10100.0)

    def test_fecha_naive_se_interpreta_como_hora_colombia(self):
        """17:05 en Bogotá son las 22:05 UTC."""
        payload = _payload([{"Fecha": "2026-09-19T17:05:00", "Valor": 10185.6}])
        ts = extract_latest_demand(payload).timestamp

        assert ts.tzinfo is not None
        assert ts == datetime(2026, 9, 19, 22, 5, tzinfo=UTC)
        assert ts.astimezone(COLOMBIA_TZ).hour == 17

    def test_fecha_con_offset_explicito(self):
        payload = _payload([{"Fecha": "2026-09-19T17:05:00-05:00", "Valor": 10185.6}])
        ts = extract_latest_demand(payload).timestamp
        assert ts == datetime(2026, 9, 19, 22, 5, tzinfo=UTC)

    def test_unidad_distinta_de_mw_es_rechazada(self):
        """Si XM pasa a kWh, los umbrales de alerta dejan de tener sentido."""
        payload = _payload([{"Fecha": "2026-09-19T17:05:00", "Valor": 10185.6}], unidad="kWh")
        with pytest.raises(InvalidXMResponseError, match="unidad inesperada"):
            extract_latest_demand(payload)

    def test_demanda_fuera_de_rango_es_rechazada(self):
        payload = _payload([{"Fecha": "2026-09-19T17:05:00", "Valor": 999_999.0}])
        with pytest.raises(InvalidXMResponseError, match="fuera de rango"):
            extract_latest_demand(payload)

    def test_serie_sin_lecturas_validas(self):
        payload = _payload([{"Fecha": "2026-09-19T17:05:00", "Valor": None}])
        with pytest.raises(InvalidXMResponseError, match="ninguna lectura"):
            extract_latest_demand(payload)

    def test_falta_la_serie_tiempo_real(self):
        payload = {"Variables": [{"Nombre": "Pronóstico", "UnidadMedida": "MW", "Datos": []}]}
        with pytest.raises(InvalidXMResponseError, match="Tiempo real"):
            extract_latest_demand(payload)

    def test_payload_sin_variables(self):
        with pytest.raises(InvalidXMResponseError, match="Variables"):
            extract_latest_demand({"Nombre": "Demanda"})

    def test_payload_que_no_es_objeto(self):
        with pytest.raises(InvalidXMResponseError):
            extract_latest_demand([1, 2, 3])


class TestBuildEvents:
    TS = datetime(2026, 9, 19, 17, 0, tzinfo=UTC)  # 12:00 en Colombia

    def test_genera_cinco_zonas_mas_el_sin(self):
        eventos = build_events(
            demanda_nacional_mw=10_000.0, fuente=DataSource.SIM, timestamp=self.TS
        )
        ids = [e.entity_id for e in eventos]

        assert len(eventos) == 6
        assert set(ids[:-1]) == set(ZONE_PROFILES)
        assert ids[-1] == "SIN"

    def test_las_participaciones_suman_uno(self):
        assert sum(p.share for p in ZONE_PROFILES.values()) == pytest.approx(1.0)

    def test_el_sin_es_la_suma_exacta_de_las_zonas(self):
        eventos = build_events(
            demanda_nacional_mw=10_000.0, fuente=DataSource.SIM, timestamp=self.TS
        )
        zonas, sin = eventos[:-1], eventos[-1]

        assert sin.data.demanda_mw == pytest.approx(
            sum(e.data.demanda_mw for e in zonas), abs=0.5
        )
        assert sin.data.generacion_mw == pytest.approx(
            sum(e.data.generacion_mw for e in zonas), abs=0.5
        )

    def test_la_demanda_del_sin_coincide_con_la_nacional(self):
        eventos = build_events(
            demanda_nacional_mw=10_185.6, fuente=DataSource.REAL, timestamp=self.TS
        )
        assert eventos[-1].data.demanda_mw == pytest.approx(10_185.6, abs=0.5)

    def test_generacion_total_es_la_suma_del_desglose(self):
        for evento in build_events(
            demanda_nacional_mw=10_000.0, fuente=DataSource.SIM, timestamp=self.TS
        ):
            d = evento.data
            desglose = (
                d.generacion_solar_mw
                + d.generacion_eolica_mw
                + d.generacion_hidraulica_mw
                + d.generacion_termica_mw
            )
            assert d.generacion_mw == pytest.approx(desglose, abs=0.5)

    def test_la_fuente_viaja_en_cada_evento(self):
        eventos = build_events(
            demanda_nacional_mw=10_000.0, fuente=DataSource.REAL, timestamp=self.TS
        )
        assert all(e.data.fuente is DataSource.REAL for e in eventos)

    def test_sin_sol_de_madrugada(self):
        medianoche = datetime(2026, 9, 19, 5, 0, tzinfo=UTC)  # 00:00 Colombia
        eventos = build_events(
            demanda_nacional_mw=10_000.0, fuente=DataSource.SIM, timestamp=medianoche
        )
        assert eventos[-1].data.generacion_solar_mw == 0.0

    def test_hay_sol_al_mediodia(self):
        eventos = build_events(
            demanda_nacional_mw=10_000.0, fuente=DataSource.SIM, timestamp=self.TS
        )
        assert eventos[-1].data.generacion_solar_mw > 0.0

    def test_sequia_desplaza_hidraulica_hacia_termica(self):
        """Con embalses bajos la térmica tiene que cubrir el hueco."""
        normal = build_events(
            demanda_nacional_mw=10_000.0, fuente=DataSource.SIM, timestamp=self.TS
        )[-1].data
        sequia = build_events(
            demanda_nacional_mw=10_000.0,
            fuente=DataSource.SIM,
            timestamp=self.TS,
            hydro_factor=0.45,
        )[-1].data

        assert sequia.generacion_hidraulica_mw < normal.generacion_hidraulica_mw
        assert sequia.generacion_termica_mw > normal.generacion_termica_mw

    def test_generation_factor_produce_deficit(self):
        sin = build_events(
            demanda_nacional_mw=10_000.0,
            fuente=DataSource.SIM,
            timestamp=self.TS,
            generation_factor=0.80,
        )[-1].data
        deficit = sin.demanda_mw - sin.generacion_mw

        # El umbral de la alerta A1 del subscriber son 800 MW.
        assert deficit > 800.0

    def test_operacion_normal_cubre_la_demanda(self):
        sin = build_events(
            demanda_nacional_mw=10_000.0, fuente=DataSource.SIM, timestamp=self.TS
        )[-1].data
        assert sin.generacion_mw >= sin.demanda_mw

    def test_timestamp_naive_es_rechazado(self):
        with pytest.raises(ValueError, match="aware"):
            build_events(
                demanda_nacional_mw=10_000.0,
                fuente=DataSource.SIM,
                timestamp=datetime(2026, 9, 19, 12, 0),
            )

    @pytest.mark.parametrize("demanda", [0.0, 1_000.0, 50_000.0])
    def test_demanda_fuera_de_rango_es_rechazada(self, demanda):
        with pytest.raises(ValueError, match="fuera de rango"):
            build_events(
                demanda_nacional_mw=demanda, fuente=DataSource.SIM, timestamp=self.TS
            )
