"""
Configuración del proyecto vía variables de entorno.

Usa Pydantic Settings v2 para que la validación falle rápido si el .env
tiene basura (ej. `XM_TIMEOUT_SECONDS=abc`), en vez de petar en runtime
durante el primer fetch.

Reglas:
- El módulo expone un singleton `settings` que carga una sola vez al
  importarse. Los módulos consumidores importan `settings` y listo.
- Los defaults son *operacionales* (puedes arrancar sin `.env`), no seguros
  para producción.
- Los secretos NO viven en este repo. Variables como tokens XM live en
  `.env` (local) o en el secret manager del deploy.

Loading order (pydantic-settings lo hace por ti):
  1. Init args del constructor (no se usan en este módulo)
  2. Variables de entorno del proceso (incluye lo cargado por python-dotenv)
  3. Fichero `.env` desde CWD o desde los padres, según config
  4. Defaults declarados en el modelo
"""

from __future__ import annotations

import logging

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from common.models import ZoneId

# Origins permitidos por defecto para el backend FastAPI (CORS).
# Override por env `CORS_ORIGINS` (CSV) — pydantic-settings parsea el string
# gracias al field_validator(mode="before") abajo.
_DEFAULT_CORS_ORIGINS: list[str] = [
    "http://localhost:5173",
    "https://sebastianlizarazo.github.io/redis-red-electrica-xm",
]


class Settings(BaseSettings):
    """Modelo de configuración inmutable (asignable solo en el __init__)."""

    model_config = SettingsConfigDict(
        # Nombre del fichero que pydantic-settings busca automáticamente.
        env_file=".env",
        env_file_encoding="utf-8",
        # Por defecto, pydantic-settings es case-insensitive en variables
        # de entorno. Lo dejamos así porque los nombres vienen en MAYÚSCULAS
        # del sistema y minúsculas solo en el código.
        case_sensitive=False,
        # Si una variable tiene nombre `xm_demanda_url` y el entorno la
        # expone como `XM_DEMANDA_URL`, lo puentea automáticamente.
        extra="ignore",  # ignora variables desconocidas (forward-compat)
    )

    # --- Redis --------------------------------------------------------------
    redis_url: str = Field(
        default="redis://localhost:6379/0",
        description="URL completa de Redis incluyendo DB.",
    )

    # --- API XM (datos reales) ---------------------------------------------
    xm_demanda_url: str = Field(
        default=(
            "https://serviciosfacturacion.xm.com.co/"
            "XM.Portal.Indicadores/api/Operacion/DemandaTiempoReal"
        ),
        description="Endpoint principal de Demanda en Tiempo Real.",
    )
    xm_timeout_seconds: float = Field(
        default=10.0,
        ge=1.0,
        le=60.0,
        description="Timeout HTTP estricto. Más de esto = fallo.",
    )
    xm_use_pydataxm: bool = Field(
        default=False,
        description="Activa enriquecimiento con pydataxm/SINERGOX.",
    )
    force_source: str = Field(
        default="",
        description=(
            "Forzar fuente: 'real', 'simulator' o '' (auto). Útil para "
            "demos sin internet y CI."
        ),
    )

    # --- Publisher (cadencia) -----------------------------------------------
    publisher_interval_seconds: int = Field(
        default=5,
        ge=1,
        le=3600,
        description="Cadencia del simulador.",
    )
    publisher_interval_seconds_real: int = Field(
        default=300,
        ge=60,
        le=86400,
        description="Cadencia del modo real (5 min por defecto).",
    )

    # --- Backend FastAPI ----------------------------------------------------
    api_host: str = Field(default="0.0.0.0")
    api_port: int = Field(default=8000, ge=1, le=65535)
    # Lista de origins permitidos por CORS. Pydantic-settings acepta una
    # lista JSON en `CORS_ORIGINS=["https://a","https://b"]` o un CSV
    # gracias al `field_validator(mode="before")` de abajo. El default
    # cubre dev local (Vite en :5173) y el deploy de GH Pages del taller.
    cors_origins: str | list[str] = Field(
        default_factory=lambda: list(_DEFAULT_CORS_ORIGINS),
        description=(
            "Lista de origins CORS permitidos. Acepta lista JSON o CSV. "
            "Override por env CORS_ORIGINS."
        ),
    )

    # --- Dashboard ----------------------------------------------------------
    vite_api_url: str = Field(
        default="http://localhost:8000",
        description="URL del backend que el frontend consume.",
    )

    # --- Logging ------------------------------------------------------------
    log_level: str = Field(
        default="INFO",
        description="Nivel de log raíz (DEBUG/INFO/WARNING/ERROR/CRITICAL).",
    )
    log_json: bool = Field(
        default=False,
        description="Si True, logs en JSON; si False, formato humano.",
    )

    # --- Alertas ------------------------------------------------------------
    demand_generation_gap_threshold: float = Field(default=800.0, ge=0.0)
    renewable_threshold: float = Field(default=30.0, ge=0.0, le=100.0)
    alert_debounce_cycles: int = Field(default=2, ge=1, le=10)

    # --- Zonas --------------------------------------------------------------
    # Tipamos como `str | list[ZoneId]` para que pydantic-settings
    # acepte un string desde .env (ej. "ANT,VAL,ATL,BOG,SAN") sin quejarse;
    # el field_validator(mode="before") lo convierte a list[ZoneId].
    default_zones: str | list[ZoneId] = Field(
        default_factory=lambda: ["ANT", "VAL", "ATL", "BOG", "SAN"],
        description="Zonas por defecto (OPEN-1 del design). Acepta lista o CSV.",
    )

    # --- Misc ---------------------------------------------------------------
    stale_threshold_seconds: int = Field(
        default=900,
        ge=60,
        description="Umbral para marcar una zona como `stale=true`.",
    )

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalize_log_level(cls, v: str) -> str:
        # Acepta "info" / "Info" / "INFO" y devuelve el nombre canónico.
        if not isinstance(v, str):
            v = str(v)
        v = v.strip().upper()
        if v not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(
                f"log_level must be one of DEBUG/INFO/WARNING/ERROR/CRITICAL, got {v!r}"
            )
        return v

    @field_validator("force_source", mode="before")
    @classmethod
    def _validate_force_source(cls, v: str) -> str:
        if v is None:
            return ""
        v = v.strip().lower()
        if v not in {"", "real", "simulator"}:
            raise ValueError(f"force_source must be '', 'real', or 'simulator'; got {v!r}")
        return v

    @field_validator("default_zones", mode="before")
    @classmethod
    def _parse_default_zones(cls, v):  # noqa: ANN001 - pydantic passes raw
        # Acepta "ANT,VAL,ATL,BOG,SAN" desde env var, o ya-lista en tests.
        # NOTA: pydantic-settings aplica este validador, pero su `parse`
        # interno a veces se ejecuta antes. Por eso también blindamos con
        # `model_validator(mode="before")` abajo.
        if isinstance(v, str):
            return [z.strip().upper() for z in v.split(",") if z.strip()]
        return v

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _parse_cors_origins(cls, v):  # noqa: ANN001 - pydantic passes raw
        # Acepta "http://a,https://b" desde env var (CSV) o ya-lista.
        # Mismo patrón que `default_zones`.
        if isinstance(v, str):
            return [o.strip() for o in v.split(",") if o.strip()]
        return v

    @model_validator(mode="before")
    @classmethod
    def _pre_parse_strings(cls, data):  # noqa: ANN001 - pydantic passes raw
        """Blindaje extra: si el input llega como dict (lo normal en
        pydantic-settings), también parseamos strings separados por coma."""
        if isinstance(data, dict):
            zones = data.get("default_zones")
            if isinstance(zones, str):
                data = {**data, "default_zones": [z.strip().upper() for z in zones.split(",") if z.strip()]}
            origins = data.get("cors_origins")
            if isinstance(origins, str):
                data = {
                    **data,
                    "cors_origins": [o.strip() for o in origins.split(",") if o.strip()],
                }
        return data


# Singleton: se carga una sola vez al importar el módulo.
# Para tests, importar `from common.config import Settings; Settings()` crea
# una instancia fresca con overrides.
settings = Settings()  # type: ignore[call-arg]


def get_logger(name: str) -> logging.Logger:
    """Convenience: alias a `logging.getLogger(name)`."""
    return logging.getLogger(name)


__all__ = ["Settings", "settings", "get_logger"]
