"""
Constantes y factories de keys Redis centralizadas.

Por qué centralizar:
- Una sola fuente de verdad para que publisher/subscriber/api no divergan.
- Si renombramos una key (ej. `state:zone:*` → `zone:state:*`), un solo
  cambio en este archivo actualiza TODO el sistema.
- Los factories (`state_zone_key(zid)`) blindan contra typos como
  `state:zone:ANT ` (con espacio) que romperían el lookup.

Las keys siguen la convención:
  - `<dominio>:<entidad>:<id?>` en kebab-ish (subdominio/subdominio/hijo)
  - Pub/Sub: sin prefijo (regla histórica de Redis)
  - Streams: `<dominio>:stream`
  - Sorted sets: `<dominio>:by:<criterio>` para rankings
"""

from __future__ import annotations

from common.models import AlertSeverity, ZoneId


# --- Pub/Sub ----------------------------------------------------------------

# Canal único con discriminador `type` en el payload
# (ver spec §REQ-PUBSUB-001 + design §5).
PUBSUB_CHANNEL_ENERGY = "energy-events"


# --- Streams ----------------------------------------------------------------

# Stream principal con MAXLEN aproximado (~1000).
# A 5s son ~83 min de histórico; a 5 min son ~3.5 días.
STREAM_ENERGY = "energy:stream"
STREAM_ENERGY_MAXLEN = 1000

# Stream de alertas (MAXLEN más bajo porque son menos frecuentes).
STREAM_ALERTS = "alerts:stream"
STREAM_ALERTS_MAXLEN = 100


# --- Hashes -----------------------------------------------------------------

# Hashes por zona (5 + global). `state:zone:ANT`, `state:zone:VAL`, etc.
KEY_STATE_ZONE = "state:zone:{zone_id}"
KEY_STATE_SIN = "state:sin"

# Hashes de métricas (3 derivados del subscriber).
KEY_METRICS_RENEWABLE = "metrics:renewable"
KEY_METRICS_BALANCE = "metrics:balance"
KEY_METRICS_DEMAND_VARIATION = "metrics:demand:variation"


# --- Sorted Sets -----------------------------------------------------------

# Histórico de demanda (últimos 60 min). Score = unix timestamp,
# Member = demand_mw serializado como string.
KEY_DEMAND_HISTORY = "metrics:demand:history"

# Ranking de zonas por balance (positivo = déficit). ZREVRANGE para el top.
KEY_ZONES_BY_BALANCE = "zones:by:balance"


# --- Lists -----------------------------------------------------------------

# Lista de alertas recientes (cap 20 con LTRIM).
KEY_RECENT_ALERTS = "alerts:recent"
KEY_RECENT_ALERTS_MAXLEN = 20


# --- Strings / counters / INCR --------------------------------------------

# Total acumulado de alertas emitidas (incluye clears, según spec).
KEY_ALERTS_TOTAL = "alerts:total"

# Conteo de alertas activas por código (se resetean al cleared).
KEY_ALERTS_ACTIVE_PREFIX = "alerts:active:"

# Salud del sistema (ver design §9).
KEY_HEALTH_MODE = "health:mode"
KEY_HEALTH_XM_LAST_SUCCESS = "health:xm:last_success"
KEY_HEALTH_XM_LAST_FAILURE = "health:xm:last_failure"
KEY_HEALTH_FAILURES = "health:failures"
KEY_HEALTH_UPTIME = "health:uptime"
KEY_HEALTH_SOURCE_SWITCHES = "health:source_switches"
KEY_HEALTH_NEXT_RETRY_AT = "health:next_retry_at"

# Banderas de stress test inyectables por la API (TTL corto).
KEY_STRESS_PREFIX = "stress:"
KEY_STRESS_TTL_SECONDS = 30


# --- TTLs y ventanas -------------------------------------------------------

# 24h sin update = zona inactiva. Suficiente para demo y aborta
# zonas zombie si XM cambia discretización.
STATE_ZONE_TTL_SECONDS = 86400

# Ventana del histórico de demanda (60 min en el Sorted Set).
DEMAND_HISTORY_WINDOW_SECONDS = 3600

# Cooldown entre reintentos de vuelta a XM (5 min, ver design §10).
DEFAULT_BACKOFF_BASE_SECONDS = 300

# Máximo absoluto del backoff (30 min). Evita loops de "5, 10, 20, 30, 30, 30...".
DEFAULT_BACKOFF_CAP_SECONDS = 1800

# List también capada (es una List, no Stream, por simplicidad).
# El LPUSH + LTRIM 0 19 deja siempre 20 entradas exactas.
RECENT_ALERTS_LENGTH = 20


# --- Factories --------------------------------------------------------------


def state_zone_key(zone_id: ZoneId) -> str:
    """Hash por zona. `state_zone_key('ANT') -> 'state:zone:ANT'`.

    El `format(...)` con `str.format_map` blindaría mejor pero el splat
    simple `format(...)` es suficiente: las llaves explícitas son
    explícitamente alineadas con la plantilla.
    """
    return KEY_STATE_ZONE.format(zone_id=zone_id)


def demand_history_member(zone_id: ZoneId) -> str:
    """Miembro del Sorted Set de histórico cuando se quiere discriminar
    por zona (no usado en la implementación base porque el ranking es
    global, pero queda como hook para Fase 5 si quieres histórico por zona)."""
    return f"zone:{zone_id}"


def recent_alerts_key() -> str:
    """La lista de alertas recientes: clave simple, pero la hacemos factory
    por simetría con el resto y para futuro-namespacing."""
    return KEY_RECENT_ALERTS


def alerts_active_key(code: str) -> str:
    """Contador atómico de alertas activas de un código dado
    (ej. `alerts:active:DEMAND_GENERATION_GAP`)."""
    return f"{KEY_ALERTS_ACTIVE_PREFIX}{code}"


def stress_key(event: str) -> str:
    """Flag de stress test, con TTL corto (KEY_STRESS_TTL_SECONDS).
    SET stress:demand_surge 1 EX 30."""
    return f"{KEY_STRESS_PREFIX}{event}"


def alert_severity_in_channel(severity: AlertSeverity) -> str:
    """Discriminador por severidad dentro del payload Pub/Sub.
    Hoy vale `severity.value` directamente, pero esta factory deja la puerta
    abierta a prefijos o reformateos sin tocar al publisher."""
    return severity.value


__all__ = [
    # Pub/Sub
    "PUBSUB_CHANNEL_ENERGY",
    # Streams
    "STREAM_ENERGY",
    "STREAM_ENERGY_MAXLEN",
    "STREAM_ALERTS",
    "STREAM_ALERTS_MAXLEN",
    # Hashes
    "KEY_STATE_ZONE",
    "KEY_STATE_SIN",
    "KEY_METRICS_RENEWABLE",
    "KEY_METRICS_BALANCE",
    "KEY_METRICS_DEMAND_VARIATION",
    # Sorted Sets
    "KEY_DEMAND_HISTORY",
    "KEY_ZONES_BY_BALANCE",
    # Lists
    "KEY_RECENT_ALERTS",
    "KEY_RECENT_ALERTS_MAXLEN",
    "RECENT_ALERTS_LENGTH",
    # Strings / counters
    "KEY_ALERTS_TOTAL",
    "KEY_ALERTS_ACTIVE_PREFIX",
    "KEY_HEALTH_MODE",
    "KEY_HEALTH_XM_LAST_SUCCESS",
    "KEY_HEALTH_XM_LAST_FAILURE",
    "KEY_HEALTH_FAILURES",
    "KEY_HEALTH_UPTIME",
    "KEY_HEALTH_SOURCE_SWITCHES",
    "KEY_HEALTH_NEXT_RETRY_AT",
    "KEY_STRESS_PREFIX",
    "KEY_STRESS_TTL_SECONDS",
    # TTLs y ventanas
    "STATE_ZONE_TTL_SECONDS",
    "DEMAND_HISTORY_WINDOW_SECONDS",
    "DEFAULT_BACKOFF_BASE_SECONDS",
    "DEFAULT_BACKOFF_CAP_SECONDS",
    # Factories
    "state_zone_key",
    "demand_history_member",
    "recent_alerts_key",
    "alerts_active_key",
    "stress_key",
    "alert_severity_in_channel",
]
