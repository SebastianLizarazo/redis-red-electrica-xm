"""
Normalizador del publisher (T-PUB-003).

Traduce datos crudos al contrato `Event` de `common/models.py`. Es el único
módulo del publisher que sabe cómo se ve un `Event`, de modo que XM y el
simulador producen exactamente la misma forma: el resto del sistema no puede
distinguir el origen salvo por `EventData.fuente`.

Dos responsabilidades:

1. `extract_latest_demand()` — lee la respuesta cruda de XM y devuelve la
   última lectura válida de demanda nacional.
2. `build_events()` — reparte una demanda nacional (venga de XM o del
   simulador) entre las 5 zonas y arma el desglose de generación por recurso.

Por qué el paso (2) existe
--------------------------
El endpoint `DemandaTiempoReal` publica un agregado nacional: no trae
desglose por zona ni por recurso de generación. El taller exige ambos. La
solución es un modelo de reparto explícito y documentado:

  - Cada zona consume una fracción fija de la demanda nacional (`share`).
  - Dentro de la zona, la generación se despacha por orden de mérito real:
    primero lo renovable no gestionable (solar y eólica, costo marginal
    cero), luego la hidráulica (limitada por embalses) y por último la
    térmica, que cubre el faltante.

Es un modelo, no un dato medido, y está marcado como tal: la magnitud del
sistema sí es real cuando la fuente es XM. La alternativa (pydataxm/SINERGOX)
queda tras el flag `XM_USE_PYDATAXM` y es trabajo de Fase 5.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

from common.data_source import InvalidXMResponseError
from common.models import DataSource, Event, EventData, Location, ZoneId

# --- Constantes de dominio ---------------------------------------------------

# Colombia no aplica horario de verano: el offset es fijo todo el año.
COLOMBIA_TZ = timezone(timedelta(hours=-5))

# Rango de sanidad para la demanda nacional del SIN. El pico histórico ronda
# los 11 GW; el valle de madrugada no baja de ~7 GW. Un valor fuera de esta
# banda casi siempre significa que XM cambió la unidad (kWh) o que estamos
# leyendo otra serie, no que el país cambió de tamaño.
DEMANDA_NACIONAL_MIN_MW = 3_000.0
DEMANDA_NACIONAL_MAX_MW = 20_000.0

# Margen de reserva: el despacho siempre programa algo por encima de la
# demanda esperada para absorber desviaciones.
RESERVE_MARGIN = 1.04

# Nombre de la serie que nos interesa dentro de `Variables`. La respuesta
# también trae "Pronóstico", que es proyección y NO debe publicarse como
# lectura real.
XM_SERIE_TIEMPO_REAL = "tiempo real"


@dataclass(frozen=True, slots=True)
class ZoneProfile:
    """Perfil estático de una zona: dónde está, cuánto pesa y con qué genera.

    `share` es la fracción de la demanda nacional que consume la zona.
    `mix` son las fracciones de capacidad por recurso dentro de la zona y
    suman 1.0.
    """

    zone_id: ZoneId
    nombre: str
    location: Location
    share: float
    hidraulica: float
    termica: float
    solar: float
    eolica: float


# Las 5 zonas fijadas en el design (OPEN-1). Las participaciones suman 1.0 y
# el mix de cada una refleja su parque generador real: Antioquia y Santander
# son hidráulicas (Ituango, Sogamoso), el Atlántico es térmico con la eólica
# de La Guajira, y Bogotá mezcla hidro con respaldo térmico.
ZONE_PROFILES: dict[ZoneId, ZoneProfile] = {
    "BOG": ZoneProfile(
        zone_id="BOG",
        nombre="Bogotá - Cundinamarca",
        location=Location(latitude=4.7110, longitude=-74.0721),
        share=0.30,
        hidraulica=0.60,
        termica=0.34,
        solar=0.05,
        eolica=0.01,
    ),
    "ANT": ZoneProfile(
        zone_id="ANT",
        nombre="Antioquia",
        location=Location(latitude=6.2442, longitude=-75.5812),
        share=0.22,
        hidraulica=0.88,
        termica=0.09,
        solar=0.03,
        eolica=0.00,
    ),
    "ATL": ZoneProfile(
        zone_id="ATL",
        nombre="Atlántico - Caribe",
        location=Location(latitude=10.9639, longitude=-74.7964),
        share=0.20,
        hidraulica=0.10,
        termica=0.70,
        solar=0.12,
        eolica=0.08,
    ),
    "VAL": ZoneProfile(
        zone_id="VAL",
        nombre="Valle del Cauca",
        location=Location(latitude=3.4516, longitude=-76.5320),
        share=0.18,
        hidraulica=0.76,
        termica=0.18,
        solar=0.06,
        eolica=0.00,
    ),
    "SAN": ZoneProfile(
        zone_id="SAN",
        nombre="Santander",
        location=Location(latitude=7.1193, longitude=-73.1227),
        share=0.10,
        hidraulica=0.84,
        termica=0.08,
        solar=0.08,
        eolica=0.00,
    ),
}

# Centro geográfico aproximado del país, para el tick consolidado del SIN.
SIN_LOCATION = Location(latitude=4.5709, longitude=-74.2973)

# Disponibilidad solar por hora local: cero de noche, máximo al mediodía.
CURVA_SOLAR: tuple[float, ...] = (
    0.00, 0.00, 0.00, 0.00, 0.00, 0.00,
    0.08, 0.28, 0.52, 0.74, 0.90, 0.98,
    1.00, 0.96, 0.86, 0.70, 0.48, 0.22,
    0.04, 0.00, 0.00, 0.00, 0.00, 0.00,
)

# Disponibilidad eólica por hora local. En La Guajira el viento arrecia de
# noche y cae al mediodía — es el patrón inverso al solar, y por eso las dos
# fuentes se complementan bien en el Caribe.
CURVA_EOLICA: tuple[float, ...] = (
    0.90, 0.92, 0.94, 0.95, 0.93, 0.88,
    0.78, 0.66, 0.55, 0.48, 0.45, 0.44,
    0.46, 0.50, 0.56, 0.63, 0.72, 0.80,
    0.86, 0.90, 0.92, 0.92, 0.91, 0.90,
)


@dataclass(frozen=True, slots=True)
class XMReading:
    """Una lectura puntual de demanda nacional extraída de la respuesta XM."""

    demanda_mw: float
    timestamp: datetime


# --- Extracción desde la respuesta cruda de XM -------------------------------


def extract_latest_demand(payload: Any) -> XMReading:
    """Devuelve la última lectura NO nula de la serie "Tiempo real".

    La respuesta de XM tiene esta forma::

        {"Nombre": "Demanda en tiempo real",
         "Variables": [{"Nombre": "Tiempo real", "UnidadMedida": "MW",
                        "Datos": [{"Fecha": "...", "Valor": 10533.38}, ...]},
                       {"Nombre": "Pronóstico", ...}]}

    Tres trampas verificadas contra el endpoint real:

    1. Hay dos series y la segunda es un **pronóstico**; publicarla como
       lectura real sería mentir en el dashboard.
    2. El último punto de la serie real suele traer ``"Valor": null`` (XM
       deja el slot de la muestra en curso reservado). Tomar ``Datos[-1]``
       a ciegas devuelve ``None`` y revienta aguas abajo.
    3. Las fechas de la serie real son *naive* y están en hora Colombia; el
       modelo `Event` exige datetime aware en UTC.

    Raises:
        InvalidXMResponseError: si la estructura no es la esperada, si la
            unidad no es MW o si no hay ninguna lectura válida.
    """
    if not isinstance(payload, dict):
        raise InvalidXMResponseError(
            f"se esperaba un objeto JSON, llegó {type(payload).__name__}", source="xm"
        )

    variables = payload.get("Variables")
    if not isinstance(variables, list) or not variables:
        raise InvalidXMResponseError("la respuesta no trae 'Variables'", source="xm")

    serie = _find_serie_tiempo_real(variables)

    unidad = str(serie.get("UnidadMedida", "")).strip().upper()
    if unidad != "MW":
        # Si XM cambia a kWh, los números entran 1000x y todos los umbrales
        # de alerta del subscriber dejan de tener sentido en silencio.
        raise InvalidXMResponseError(
            f"unidad inesperada {unidad!r}, se esperaba 'MW'", source="xm"
        )

    datos = serie.get("Datos")
    if not isinstance(datos, list) or not datos:
        raise InvalidXMResponseError("la serie 'Tiempo real' no trae 'Datos'", source="xm")

    for punto in reversed(datos):
        if not isinstance(punto, dict):
            continue
        valor = punto.get("Valor")
        if valor is None:
            continue
        try:
            demanda = float(valor)
        except (TypeError, ValueError):
            continue
        if not _is_finite(demanda):
            continue
        if not DEMANDA_NACIONAL_MIN_MW <= demanda <= DEMANDA_NACIONAL_MAX_MW:
            raise InvalidXMResponseError(
                f"demanda nacional fuera de rango: {demanda} MW "
                f"(esperado {DEMANDA_NACIONAL_MIN_MW}-{DEMANDA_NACIONAL_MAX_MW})",
                source="xm",
            )
        return XMReading(demanda_mw=demanda, timestamp=_parse_fecha(punto.get("Fecha")))

    raise InvalidXMResponseError(
        "la serie 'Tiempo real' no tiene ninguna lectura con valor", source="xm"
    )


def _find_serie_tiempo_real(variables: list[Any]) -> dict[str, Any]:
    """Localiza la serie de medición real, descartando el pronóstico."""
    for variable in variables:
        if not isinstance(variable, dict):
            continue
        if str(variable.get("Nombre", "")).strip().lower() == XM_SERIE_TIEMPO_REAL:
            return variable
    raise InvalidXMResponseError(
        "no se encontró la serie 'Tiempo real' en la respuesta", source="xm"
    )


def _parse_fecha(fecha: Any) -> datetime:
    """Convierte la fecha de XM a datetime aware en UTC.

    Acepta tanto el formato naive de la serie real (`2026-09-19T17:18:13.016`)
    como el que trae offset explícito en el pronóstico (`...-05:00`). Si la
    fecha falta o es ilegible cae a "ahora", porque perder el dato de demanda
    por un timestamp mal formado sería peor que estampar la hora de llegada.
    """
    if isinstance(fecha, str) and fecha.strip():
        try:
            parsed = datetime.fromisoformat(fecha.strip())
        except ValueError:
            return datetime.now(UTC)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=COLOMBIA_TZ)
        return parsed.astimezone(UTC)
    return datetime.now(UTC)


def _is_finite(valor: float) -> bool:
    return valor == valor and valor not in (float("inf"), float("-inf"))


# --- Construcción de eventos -------------------------------------------------


def build_events(
    *,
    demanda_nacional_mw: float,
    fuente: DataSource,
    timestamp: datetime,
    hydro_factor: float = 1.0,
    generation_factor: float = 1.0,
) -> list[Event]:
    """Reparte la demanda nacional en 6 eventos: las 5 zonas + el SIN.

    Args:
        demanda_nacional_mw: demanda total del sistema, real o simulada.
        fuente: marca de trazabilidad (`REAL` o `SIM`) que viaja en cada evento.
        timestamp: instante de la lectura, aware. La hora local derivada de
            él decide la disponibilidad solar y eólica.
        hydro_factor: multiplicador de la generación hidráulica. Menor a 1
            modela sequía o embalses bajos (stress `hydro_drop`).
        generation_factor: multiplicador sobre toda la generación ya
            despachada. Menor a 1 fuerza déficit (stress `critical_deficit`).

    Returns:
        Los 5 eventos de zona seguidos del consolidado `SIN`, que es la suma
        exacta de los anteriores.

    Raises:
        ValueError: si la demanda nacional cae fuera del rango de sanidad.
    """
    if not _is_finite(demanda_nacional_mw):
        raise ValueError("la demanda nacional debe ser un número finito")
    if not DEMANDA_NACIONAL_MIN_MW <= demanda_nacional_mw <= DEMANDA_NACIONAL_MAX_MW:
        raise ValueError(
            f"demanda nacional fuera de rango: {demanda_nacional_mw} MW "
            f"(esperado {DEMANDA_NACIONAL_MIN_MW}-{DEMANDA_NACIONAL_MAX_MW})"
        )

    if timestamp.tzinfo is None:
        raise ValueError("el timestamp debe ser aware (UTC)")

    hora_local = hora_colombia_decimal(timestamp)
    disp_solar = interpolar_curva(CURVA_SOLAR, hora_local)
    disp_eolica = interpolar_curva(CURVA_EOLICA, hora_local)

    eventos: list[Event] = []
    acumulado = {
        "demanda_mw": 0.0,
        "generacion_solar_mw": 0.0,
        "generacion_eolica_mw": 0.0,
        "generacion_hidraulica_mw": 0.0,
        "generacion_termica_mw": 0.0,
    }

    for profile in ZONE_PROFILES.values():
        demanda_zona = demanda_nacional_mw * profile.share
        despacho = _despachar(
            profile,
            demanda_zona,
            disp_solar=disp_solar,
            disp_eolica=disp_eolica,
            hydro_factor=hydro_factor,
            generation_factor=generation_factor,
        )

        acumulado["demanda_mw"] += demanda_zona
        for recurso, mw in despacho.items():
            acumulado[recurso] += mw

        eventos.append(
            Event(
                entity_id=profile.zone_id,
                timestamp=timestamp,
                location=profile.location,
                data=_build_data(demanda_zona, despacho, fuente),
            )
        )

    despacho_sin = {k: v for k, v in acumulado.items() if k != "demanda_mw"}
    eventos.append(
        Event(
            entity_id="SIN",
            timestamp=timestamp,
            location=SIN_LOCATION,
            data=_build_data(acumulado["demanda_mw"], despacho_sin, fuente),
        )
    )
    return eventos


def _build_data(demanda_mw: float, despacho: dict[str, float], fuente: DataSource) -> EventData:
    """Arma el `EventData`, con la generación total siempre igual a la suma."""
    total = sum(despacho.values())
    return EventData(
        demanda_mw=_redondear(demanda_mw),
        generacion_mw=_redondear(total),
        generacion_solar_mw=_redondear(despacho["generacion_solar_mw"]),
        generacion_eolica_mw=_redondear(despacho["generacion_eolica_mw"]),
        generacion_hidraulica_mw=_redondear(despacho["generacion_hidraulica_mw"]),
        generacion_termica_mw=_redondear(despacho["generacion_termica_mw"]),
        fuente=fuente,
    )


def _despachar(
    profile: ZoneProfile,
    demanda_zona: float,
    *,
    disp_solar: float,
    disp_eolica: float,
    hydro_factor: float,
    generation_factor: float,
) -> dict[str, float]:
    """Despacha la generación de una zona por orden de mérito.

    Entra primero lo renovable no gestionable (solar y eólica), que se
    aprovecha íntegro porque su costo marginal es cero; luego la hidráulica,
    acotada por `hydro_factor`; y la térmica cubre el faltante hasta su
    capacidad instalada. Ese orden es el que hace que una sequía (hidráulica
    restringida) se traduzca sola en más térmica y peor huella de carbono,
    sin números mágicos.
    """
    objetivo = demanda_zona * RESERVE_MARGIN

    solar = objetivo * profile.solar * disp_solar
    eolica = objetivo * profile.eolica * disp_eolica
    hidraulica = objetivo * profile.hidraulica * hydro_factor

    # La térmica solo cubre el hueco restante, y no puede pasar de su
    # capacidad instalada (aprox. 2.2x su cuota nominal en la zona).
    faltante = max(0.0, objetivo - (solar + eolica + hidraulica))
    termica = min(faltante, objetivo * profile.termica * 2.2)

    return {
        "generacion_solar_mw": max(0.0, solar * generation_factor),
        "generacion_eolica_mw": max(0.0, eolica * generation_factor),
        "generacion_hidraulica_mw": max(0.0, hidraulica * generation_factor),
        "generacion_termica_mw": max(0.0, termica * generation_factor),
    }


def hora_colombia_decimal(timestamp: datetime) -> float:
    """Hora local de Colombia como decimal (13.5 = 13:30), para interpolar."""
    local = timestamp.astimezone(COLOMBIA_TZ)
    return local.hour + local.minute / 60.0


def interpolar_curva(curva: tuple[float, ...], hora: float) -> float:
    """Interpola linealmente en una curva horaria de 24 posiciones.

    Sin interpolación la disponibilidad solar saltaría en escalones cada hora
    y las gráficas del dashboard mostrarían dientes de sierra.
    """
    hora = hora % 24
    base = int(hora)
    fraccion = hora - base
    actual = curva[base]
    siguiente = curva[(base + 1) % 24]
    return actual + (siguiente - actual) * fraccion


def _redondear(valor: float, decimales: int = 1) -> float:
    return round(max(0.0, valor), decimales)


__all__ = [
    "COLOMBIA_TZ",
    "CURVA_EOLICA",
    "CURVA_SOLAR",
    "DEMANDA_NACIONAL_MAX_MW",
    "DEMANDA_NACIONAL_MIN_MW",
    "RESERVE_MARGIN",
    "SIN_LOCATION",
    "ZONE_PROFILES",
    "XMReading",
    "ZoneProfile",
    "build_events",
    "extract_latest_demand",
    "hora_colombia_decimal",
    "interpolar_curva",
]
