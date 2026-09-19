"""
Modelos de dominio Pydantic v2 compartidos por publisher, subscriber, api
y dashboard.

Convenciones:
- Todos los modelos son inmutables (`frozen=True`) salvo donde el caller
  necesite mutar (poco común gracias al patrón funcional).
- Las fechas son siempre `AwareDatetime` (UTC) — la API XM y Redis
  no negocian zonas naive.
- Las validaciones físicas viven en field_validators (no en constructores
  ad-hoc) para que fallen en parseo, no en uso.
- NO usar `from __future__ import annotations` en Pydantic v2: pierde
  información de tipos que el validador necesita.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
)


# --- Enums y discriminadores -------------------------------------------------


# Las 5 zonas colombianas representativas (design §3, OPEN-1) más
# el agregador global "SIN" para el tick consolidado.
# Hardcoded porque la API XM no devuelve códigos al nivel requerido
# y el docente las asumió fijas.
# OJO: el factory `state_zone_key(zid)` SOLO aplica a las 5 zonas;
# para "SIN" usar `state_sin_key()` (pendiente de Fase 1 si se necesita).
ZoneId = Literal["ANT", "VAL", "ATL", "BOG", "SAN", "SIN"]
# Conjunto reducido (sin "SIN") para cuando necesitemos solo las
# 5 zonas geográficas de las claves `state:zone:*`.
ZONES_GEO = ("ANT", "VAL", "ATL", "BOG", "SAN")


class DataSource(str, Enum):
    """Origen del dato entrante. Se serializa como string corto."""

    REAL = "real"
    SIM = "simulator"


class AlertSeverity(str, Enum):
    """Severidad operativa de una alerta."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


# --- Modelos simples ---------------------------------------------------------


class Location(BaseModel):
    """Coordenadas geográficas en grados decimales (WGS84)."""

    model_config = ConfigDict(frozen=True)

    latitude: float = Field(..., ge=-90.0, le=90.0)
    longitude: float = Field(..., ge=-180.0, le=180.0)


class EventData(BaseModel):
    """
    Carga útil de un tick (Punto del SIN) o de una zona individual.

    Todos los MW son `>= 0` físicamente. La suma de generaciones por recurso
    no se valida aquí (puede haber fuentes no desagregadas); el processor
    se encarga de la coherencia.
    """

    model_config = ConfigDict(frozen=True)

    demanda_mw: float = Field(..., ge=0.0, le=50_000.0)
    generacion_mw: float = Field(..., ge=0.0, le=50_000.0)
    generacion_solar_mw: float = Field(..., ge=0.0, le=50_000.0)
    generacion_eolica_mw: float = Field(..., ge=0.0, le=50_000.0)
    generacion_hidraulica_mw: float = Field(..., ge=0.0, le=50_000.0)
    generacion_termica_mw: float = Field(..., ge=0.0, le=50_000.0)
    fuente: DataSource

    @field_validator("*", mode="after")
    @classmethod
    def _no_nan(cls, v: float) -> float:
        # NaN e Inf rompen JSON, Redis, Chart.js... bloquearlos aquí.
        if isinstance(v, float) and not (v == v and v not in (float("inf"), float("-inf"))):
            raise ValueError("must be a finite number")
        return v


class Event(BaseModel):
    """
    Evento normalizado publicado en el canal Pub/Sub `energy-events`.

    Es el contrato compartido entre publisher (produce) y subscriber
    (consume). Un cambio aquí rompe TODOS los consumidores — versionar
    cualquier cambio en `data` con un campo `schema_version`.
    """

    model_config = ConfigDict(frozen=True)

    entity_id: ZoneId
    timestamp: AwareDatetime
    location: Location
    data: EventData


class ZoneState(BaseModel):
    """Snapshot por zona expuesto por /api/state."""

    model_config = ConfigDict(frozen=True)

    zone_id: ZoneId
    demanda_mw: float = Field(..., ge=0.0)
    generacion_mw: float = Field(..., ge=0.0)
    timestamp: AwareDatetime
    fuente: DataSource


class Metric(BaseModel):
    """Métrica derivada univariada. `zone_id=None` = global (SIN)."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(..., min_length=1, max_length=64)
    value: float
    unit: str = Field(..., min_length=1, max_length=16)
    timestamp: AwareDatetime
    zone_id: ZoneId | None = None


class Alert(BaseModel):
    """Alerta operacional, generada por el subscriber."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(..., min_length=1, max_length=64)
    rule: str = Field(..., min_length=1, max_length=64)
    severity: AlertSeverity
    zone_id: ZoneId
    value: float
    threshold: float
    timestamp: AwareDatetime
    message: str = Field(..., min_length=1, max_length=512)


class HealthStatus(BaseModel):
    """
    Estado agregado del pipeline. El dashboard lo pinta como banner + KPIs
    de freshness; el operador lo lee para saber si XM está respondiendo.
    """

    model_config = ConfigDict(frozen=True)

    mode: DataSource
    last_xm_success: datetime | None
    # `datetime | None` en vez de `AwareDatetime | None` porque cuando XM
    # nunca respondió, `last_xm_success` es None; no necesitamos validar
    # zona horaria sobre None. Cuando llegue un valor real, sí exigimos
    # aware en el validador de abajo.
    consecutive_failures: int = Field(..., ge=0)
    redis_ok: bool
    uptime_seconds: int = Field(..., ge=0)

    @field_validator("last_xm_success", mode="after")
    @classmethod
    def _ensure_aware_when_present(cls, v: datetime | None) -> datetime | None:
        if v is None:
            return v
        if v.tzinfo is None or v.tzinfo.utcoffset(v) is None:
            raise ValueError("last_xm_success must be timezone-aware (UTC)")
        return v


__all__ = [
    "Alert",
    "AlertSeverity",
    "AwareDatetime",
    "DataSource",
    "Event",
    "EventData",
    "HealthStatus",
    "Location",
    "Metric",
    "ZoneId",
    "ZONES_GEO",
    "ZoneState",
]
