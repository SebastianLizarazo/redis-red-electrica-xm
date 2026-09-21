"""
Contrato `DataSource`: cualquier fuente de datos (XM real o simulador)
debe implementar `async fetch()` y `async health_check()`.

Por qué Protocol y no ABC:
- duck typing estático (mypy) sin acoplamiento en runtime.
- Una clase ya existente que cumpla la forma vale sin heredar de nada.
- Tests y fakes fluyen sin trampas de instanciación.

Las implementaciones concretas viven en:
- `publisher/xm_client.py`     → XMRealSource (HTTP a XM + pydataxm opcional)
- `publisher/simulator.py`     → SimulatorSource (4 modos de stress test)

El selector que decide cuál usar vive en `publisher/source_selector.py`.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from common.models import Event

# Custom exceptions: viven aquí para que publisher Y tests las puedan
# capturar sin acoplar al módulo que las lanza originalmente.


class DataSourceError(Exception):
    """Error base para cualquier fuente de datos."""

    def __init__(self, message: str, *, source: str = "", cause: Exception | None = None) -> None:
        super().__init__(message)
        self.source = source
        self.cause = cause


class DataSourceTimeoutError(DataSourceError):
    """La fuente no respondió dentro del timeout configurado."""


class InvalidXMResponseError(DataSourceError):
    """La fuente respondió pero el payload no tiene la estructura esperada."""


@runtime_checkable
class DataSource(Protocol):
    """
    Contrato para cualquier fuente que publique eventos en el bus.

    Implementar como `@runtime_checkable` permite que tests hagan
    `isinstance(fake, DataSource)` sin herencia (útil con mocks simples
    o con dataclasses que cumplen la forma por accidente).
    """

    # Atributo de clase: nombre humano legible (para logs y métricas).
    # Lo implementa la subclase como `name: ClassVar[str] = "xm"`.
    name: str

    async def fetch_event(self) -> Event | None:
        """
        Devuelve el siguiente evento o `None` si no hay datos disponibles
        en este ciclo (ej. rate limit, sin respuesta fresca, error recuperable).

        El subscriber distingue `None` de "error fatal": `None` significa
        "no publicar este ciclo"; una excepción propagada significa "fallo
        que el selector debería contar".
        """
        ...

    async def health_check(self) -> bool:
        """
        Liveness probe rápido (sin transferir datos). Usado por el endpoint
        /api/health y por el selector de fuente para distinguir "caída total"
        de "responde pero devuelve basura".
        """
        ...


# Alias útil para importadores que quieran el "tipo canónico".
DataSourceT = DataSource


__all__ = [
    "DataSource",
    "DataSourceT",
    "DataSourceError",
    "DataSourceTimeoutError",
    "InvalidXMResponseError",
]
