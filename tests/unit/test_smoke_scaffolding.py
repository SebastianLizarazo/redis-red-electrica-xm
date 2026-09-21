"""Smoke test del scaffolding Fase 0.

Ejecuta las verificaciones del checklist de T-INFRA-001 a T-INT-005:
- import de todos los módulos comunes
- instanciación de Settings
- constantes y factories de redis_keys
- Protocol DataSource y jerarquía de errores
- modelos Pydantic construibles y validables
- setup_logging no crashea
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime

# --- imports básicos ---

def test_imports() -> None:
    from common import config, data_source, logging_config, models, redis_keys
    assert models
    assert redis_keys
    assert data_source
    assert config
    assert logging_config


def test_models_zoneid_literal() -> None:
    # ZoneId es Literal — 5 zonas geográficas + "SIN" global.
    import typing

    from common.models import ZoneId
    args = typing.get_args(ZoneId)
    assert set(args) == {"ANT", "VAL", "ATL", "BOG", "SAN", "SIN"}


def test_models_event_constructible() -> None:
    from common.models import DataSource, Event, EventData, Location
    e = Event(
        entity_id="SIN",
        timestamp=datetime(2026, 9, 19, 12, 0, 0, tzinfo=UTC),
        location=Location(latitude=4.5, longitude=-74.1),
        data=EventData(
            demanda_mw=10_500.0,
            generacion_mw=10_800.0,
            generacion_solar_mw=200.0,
            generacion_eolica_mw=50.0,
            generacion_hidraulica_mw=6_500.0,
            generacion_termica_mw=4_050.0,
            fuente=DataSource.SIM,
        ),
    )
    assert e.entity_id == "SIN"
    assert e.data.fuente.value == "simulator"
    # Inmutabilidad por `frozen=True`.
    try:
        e.entity_id = "ATL"  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("Event debe ser inmutable (frozen=True)")


def test_models_event_validates_negative() -> None:
    from pydantic import ValidationError

    from common.models import DataSource, Event, EventData, Location
    try:
        Event(
            entity_id="SIN",
            timestamp=datetime(2026, 9, 19, 12, 0, 0, tzinfo=UTC),
            location=Location(latitude=4.5, longitude=-74.1),
            data=EventData(
                demanda_mw=-1.0,
                generacion_mw=10_800.0,
                generacion_solar_mw=200.0,
                generacion_eolica_mw=50.0,
                generacion_hidraulica_mw=6_500.0,
                generacion_termica_mw=4_050.0,
                fuente=DataSource.SIM,
            ),
        )
    except ValidationError:
        return
    raise AssertionError("demanda_mw=-1 debió ser rechazado")


def test_redis_keys_factories() -> None:
    from common.models import AlertSeverity as AS
    from common.redis_keys import (
        DEMAND_HISTORY_WINDOW_SECONDS,
        PUBSUB_CHANNEL_ENERGY,
        STATE_ZONE_TTL_SECONDS,
        STREAM_ENERGY,
        STREAM_ENERGY_MAXLEN,
        alert_severity_in_channel,
        alerts_active_key,
        state_zone_key,
        stress_key,
    )
    assert PUBSUB_CHANNEL_ENERGY == "energy-events"
    assert STREAM_ENERGY == "energy:stream"
    assert STREAM_ENERGY_MAXLEN == 1000
    assert state_zone_key("ANT") == "state:zone:ANT"
    assert state_zone_key("BOG") == "state:zone:BOG"
    assert alerts_active_key("DEMAND_GENERATION_GAP") == "alerts:active:DEMAND_GENERATION_GAP"
    assert stress_key("demand_surge") == "stress:demand_surge"
    assert alert_severity_in_channel(AS.HIGH) == "high"
    assert STATE_ZONE_TTL_SECONDS == 86400
    assert DEMAND_HISTORY_WINDOW_SECONDS == 3600


def test_data_source_protocol() -> None:
    from common.data_source import (
        DataSource,
        DataSourceError,
        DataSourceTimeoutError,
        InvalidXMResponseError,
    )
    # DataSource es Protocol — debe ser runtime_checkable (isinstance).
    class Fake:
        name = "fake"
        async def fetch_event(self): return None
        async def health_check(self): return True
    f = Fake()
    assert isinstance(f, DataSource)
    # Jerarquía de errores.
    err = DataSourceTimeoutError("timeout", source="xm")
    assert isinstance(err, DataSourceError)
    assert err.source == "xm"
    inv = InvalidXMResponseError("bad json", source="xm")
    assert isinstance(inv, DataSourceError)


def test_config_singleton() -> None:
    # Evita contaminar settings globales: instanciar en tmpdir.
    import os
    os.environ["REDIS_URL"] = "redis://example.com:1234/2"
    from common.config import Settings
    s = Settings()
    assert s.redis_url == "redis://example.com:1234/2"
    # El singleton del módulo es independiente de esta llamada (carga al import).
    from common.config import settings
    assert settings.redis_url.startswith("redis://")


def test_config_loads_dotenv(tmp_path) -> None:
    """Verifica que Settings pilla el .env sin contaminar el singleton."""
    import os
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "REDIS_URL=redis://dotenv-host:9999/5\n"
        "LOG_LEVEL=DEBUG\n"
        "XM_TIMEOUT_SECONDS=12.5\n"
        "DEFAULT_ZONES=ANT,BOG\n",
        encoding="utf-8",
    )
    # Limpiar env vars del test anterior que contaminarían este.
    for key in ("REDIS_URL", "LOG_LEVEL", "XM_TIMEOUT_SECONDS", "DEFAULT_ZONES"):
        os.environ.pop(key, None)
    # Cambiar de cwd temporalmente.
    old = os.getcwd()
    try:
        os.chdir(tmp_path)
        from common.config import Settings
        s = Settings()
        assert s.redis_url == "redis://dotenv-host:9999/5"
        assert s.log_level == "DEBUG"
        assert s.xm_timeout_seconds == 12.5
        assert s.default_zones == ["ANT", "BOG"]
    finally:
        os.chdir(old)


def test_logging_setup() -> None:
    import logging

    from common.logging_config import setup_logging
    setup_logging("INFO", json_format=False)
    setup_logging("DEBUG", json_format=True)  # idempotente
    log = logging.getLogger("publisher")
    assert log is not None
    # Capturar una salida para confirmar que no crashea.
    log.info("smoke test")


if __name__ == "__main__":
    # Run pytest on this file when invoked directly: `python smoke_test.py`.
    import pytest
    sys.exit(pytest.main([__file__, "-v", "--tb=short"]))
