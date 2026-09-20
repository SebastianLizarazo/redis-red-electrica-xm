"""
Cliente de la API pública de XM (T-PUB-001).

Implementa el Protocol `DataSource` contra el endpoint `DemandaTiempoReal`
del operador del SIN. No requiere autenticación.

Política de errores
-------------------
Toda excepción de red se traduce a la jerarquía de `common.data_source`, para
que `source_selector` decida el fallback sin tener que inspeccionar tipos de
httpx:

    httpx.TimeoutException  -> DataSourceTimeoutError
    httpx.HTTPError / 5xx   -> DataSourceError
    payload inesperado      -> InvalidXMResponseError

Política de reintentos
----------------------
Tres intentos con backoff exponencial, pero **solo para fallos transitorios**
(timeout, error de red, 5xx). Un 4xx o un payload malformado no se reintenta:
si XM devuelve basura, devolverá la misma basura 200 ms después y lo único
que se consigue es retrasar el fallback al simulador.
"""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar

import httpx

from common.config import settings
from common.data_source import (
    DataSourceError,
    DataSourceTimeoutError,
    InvalidXMResponseError,
)
from common.logging_config import get_logger
from common.models import DataSource, Event
from publisher.normalizer import build_events, extract_latest_demand

logger = get_logger("publisher.xm")


class XMRealSource:
    """Fuente de datos real: demanda nacional publicada por XM."""

    name: ClassVar[str] = "xm"

    def __init__(
        self,
        *,
        url: str | None = None,
        timeout: float | None = None,
        max_retries: int = 3,
        backoff_base_seconds: float = 0.5,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._url = url or settings.xm_demanda_url
        self._timeout = timeout if timeout is not None else settings.xm_timeout_seconds
        self._max_retries = max(1, max_retries)
        self._backoff_base = backoff_base_seconds
        # Un cliente inyectado (tests) no se cierra aquí: lo maneja quien lo creó.
        self._client = client
        self._owns_client = client is None

    # --- Protocol DataSource ------------------------------------------------

    async def fetch_event(self) -> Event | None:
        """Devuelve solo el tick consolidado del SIN (contrato del Protocol)."""
        eventos = await self.fetch_events()
        return next((e for e in eventos if e.entity_id == "SIN"), None)

    async def fetch_events(self) -> list[Event]:
        """Consulta XM y devuelve los 6 eventos (5 zonas + SIN).

        Raises:
            DataSourceTimeoutError: XM no respondió dentro del timeout.
            DataSourceError: fallo de red o HTTP no recuperable.
            InvalidXMResponseError: la respuesta no tiene la forma esperada.
        """
        payload = await self._get_json()
        lectura = extract_latest_demand(payload)
        logger.debug(
            "lectura XM obtenida",
            extra={"demanda_mw": lectura.demanda_mw, "ts": lectura.timestamp.isoformat()},
        )
        return build_events(
            demanda_nacional_mw=lectura.demanda_mw,
            fuente=DataSource.REAL,
            timestamp=lectura.timestamp,
        )

    async def health_check(self) -> bool:
        """Liveness sin transferir la serie completa.

        Un HEAD basta para saber si XM está en pie; no distingue "responde
        pero devuelve basura", que es justo lo que `fetch_events` sí detecta.
        """
        try:
            client = self._ensure_client()
            respuesta = await client.head(self._url, timeout=self._timeout)
            return respuesta.status_code < 500
        except httpx.HTTPError:
            return False

    # --- Internos -----------------------------------------------------------

    async def _get_json(self) -> Any:
        """GET con reintentos sobre fallos transitorios."""
        client = self._ensure_client()
        ultimo_error: Exception | None = None

        for intento in range(1, self._max_retries + 1):
            try:
                respuesta = await client.get(
                    self._url,
                    timeout=self._timeout,
                    headers={"Accept": "application/json"},
                )
            except httpx.TimeoutException as exc:
                ultimo_error = DataSourceTimeoutError(
                    f"XM no respondió en {self._timeout}s", source=self.name, cause=exc
                )
            except httpx.HTTPError as exc:
                ultimo_error = DataSourceError(
                    f"fallo de red contra XM: {exc}", source=self.name, cause=exc
                )
            else:
                if respuesta.status_code >= 500:
                    ultimo_error = DataSourceError(
                        f"XM devolvió HTTP {respuesta.status_code}", source=self.name
                    )
                elif respuesta.status_code >= 400:
                    # 4xx es determinista: la URL o el contrato cambiaron.
                    raise DataSourceError(
                        f"XM devolvió HTTP {respuesta.status_code} (no se reintenta)",
                        source=self.name,
                    )
                else:
                    return self._decode(respuesta)

            if intento < self._max_retries:
                espera = self._backoff_base * (2 ** (intento - 1))
                logger.warning(
                    "reintentando XM tras fallo transitorio",
                    extra={"intento": intento, "espera_s": espera, "motivo": str(ultimo_error)},
                )
                await asyncio.sleep(espera)

        assert ultimo_error is not None  # noqa: S101 - el loop siempre lo asigna
        raise ultimo_error

    def _decode(self, respuesta: httpx.Response) -> Any:
        try:
            return respuesta.json()
        except ValueError as exc:
            raise InvalidXMResponseError(
                "XM respondió 200 pero el cuerpo no es JSON válido",
                source=self.name,
                cause=exc,
            ) from exc

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
            self._owns_client = True
        return self._client

    async def aclose(self) -> None:
        """Cierra el cliente HTTP si lo creamos nosotros."""
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None


__all__ = ["XMRealSource"]
