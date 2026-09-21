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
    """Métrica derivada univariada. `zone_id=None` = global (SIN).

    `value` admite `None` para representar el caso M3 sin histórico:
    el subscriber (subscriber/metrics.py:persist_metrics) escribe el literal
    `"NaN"` en el hash cuando `compute_metrics.m3` es `None`. La API
    coacciona esa cadena a `None` antes de pasar a Pydantic para que el
    JSON serialice como `null` (REQ-API-003 scenario 2, design #511 §6).

    No es `Optional[float]` con default `None`: el campo sigue siendo
    obligatorio (los 3 metrics siempre se publican), solo que su valor
    puede ser `None` cuando no hay histórico. `Alert.value` y otros
    campos numéricos siguen siendo `float` estricto (no aplica este caso).
    """

    model_config = ConfigDict(frozen=True)

    name: str = Field(..., min_length=1, max_length=64)
    value: float | None  # None → JSON `null` (M3 sin histórico).
    unit: str = Field(..., min_length=1, max_length=16)
    timestamp: AwareDatetime
    zone_id: ZoneId | None = None


class Alert(BaseModel):
    """Alerta operacional, generada por el subscriber.

    Campos nuevos (PR-A de `subscriber-core-2026-09`):
    - `state`: `Literal["active", "cleared"]` para distinguir el ciclo de vida.
    - `consecutive_cycles`: contador de ciclos consecutivos en breach (debounce).
    - `code`: discriminator que coincide con el `code` del wire format del Pub/Sub
      (REQ-SUB-ALERTS-001). Por back-compat, `code == rule` cuando ambos
      están presentes; los consumidores existentes que leían `rule` siguen
      funcionando mientras `code` queda vacío por default.

    Los nuevos campos tienen default, por lo que `frozen=True` se preserva:
    instancias ya creadas no pueden mutar, pero constructores sin los nuevos
    campos siguen funcionando (test fixtures existentes siguen verdes).
    """

    model_config = ConfigDict(frozen=True)

    id: str = Field(..., min_length=1, max_length=64)
    rule: str = Field(..., min_length=1, max_length=64)
    severity: AlertSeverity
    zone_id: ZoneId
    value: float
    threshold: float
    timestamp: AwareDatetime
    message: str = Field(..., min_length=1, max_length=512)
    # --- Nuevos campos con default (back-compat, frozen=True preservado) ---
    code: str = Field(default="", max_length=64)
    state: Literal["active", "cleared"] = "active"
    consecutive_cycles: int = Field(default=0, ge=0)


class HealthStatus(BaseModel):
    """
    Estado agregado del pipeline. El dashboard lo pinta como banner + KPIs
    de freshness; el operador lo lee para saber si XM está respondiendo.

    Campos (alineados con REQ-API-005 del spec #510):
    - `mode`: fuente comprometida (REAL/SIM).
    - `failures`: contador de fallos consecutivos de XM (Redis: `health:failures`).
    - `source_switches`: nº de conmutaciones REAL↔SIM (Redis: `health:source_switches`).
    - `last_xm_success`, `last_failure`, `next_retry_at`: opcionales (None si ausentes).
    - `uptime_seconds`: derivado de `health:subscriber:started_at` (0 si ausente).
    - `redis_ok`: resultado del inline `await redis.ping()` del handler.

    Los nuevos campos tienen default, así que `frozen=True` se preserva y
    ningún call site existente se rompe (HealthStatus no se construye fuera
    de los routers del API todavía).
    """

    model_config = ConfigDict(frozen=True)

    mode: DataSource
    last_xm_success: datetime | None
    # `datetime | None` en vez de `AwareDatetime | None` porque cuando XM
    # nunca respondió, `last_xm_success` es None; no necesitamos validar
    # zona horaria sobre None. Cuando llegue un valor real, sí exigimos
    # aware en el validador de abajo.
    failures: int = Field(..., ge=0)
    source_switches: int = Field(..., ge=0)
    last_failure: datetime | None = None
    next_retry_at: datetime | None = None
    redis_ok: bool
    uptime_seconds: int = Field(..., ge=0)

    @field_validator("last_xm_success", "last_failure", "next_retry_at", mode="after")
    @classmethod
    def _ensure_aware_when_present(cls, v: datetime | None) -> datetime | None:
        if v is None:
            return v
        if v.tzinfo is None or v.tzinfo.utcoffset(v) is None:
            raise ValueError("datetime fields must be timezone-aware (UTC) when present")
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
