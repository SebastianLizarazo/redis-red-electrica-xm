"""
Configuración centralizada de logging.

Decisiones:
- JSON estructurado cuando `LOG_JSON=1` (fácil de parsear con `jq`,
  ingestable por Loki/Datadog/etc.).
- Formato humano (con timestamps y color-friendly) cuando `LOG_JSON=0`.
- Logger names canónicos: `publisher`, `subscriber`, `api`, `dashboard`.
  Usar estos nombres exactos ayuda a filtrar en runtime: `LOG_LEVEL=DEBUG`.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any, Mapping


# Colores ANSI (solo texto plano; los logs JSON NO los emiten).
_RESET = "\x1b[0m"
_LEVEL_COLORS = {
    logging.DEBUG: "\x1b[37m",  # gris claro
    logging.INFO: "\x1b[36m",  # cian
    logging.WARNING: "\x1b[33m",  # amarillo
    logging.ERROR: "\x1b[31m",  # rojo
    logging.CRITICAL: "\x1b[1;31m",  # rojo brillante
}


class _JsonFormatter(logging.Formatter):
    """Emite cada log line como JSON compacto con timestamp ISO en UTC."""

    # Campos estándar que NO van en el bloque `fields`.
    _STANDARD_FIELDS = frozenset(
        ("name", "msg", "args", "levelname", "levelno", "pathname", "lineno",
         "funcName", "created", "msecs", "relativeCreated", "thread", "threadName",
         "processName", "process", "exc_info", "exc_text", "stack_info", "message",
         "asctime", "taskName")
    )

    def format(self, record: logging.LogRecord) -> str:  # type: ignore[override]
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # Excepciones como string (truncable) y fields extra del `extra={}`.
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        for key, value in record.__dict__.items():
            if key in self._STANDARD_FIELDS or key.startswith("_"):
                continue
            payload[key] = _safe(value)
        return json.dumps(payload, ensure_ascii=False, default=str)


class _HumanFormatter(logging.Formatter):
    """Formato legible con timestamp ISO, nombre, nivel y mensaje."""

    def __init__(self) -> None:
        super().__init__(
            fmt="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )

    def formatTime(  # type: ignore[override]
        self,
        record: logging.LogRecord,
        datefmt: str | None = None,
    ) -> str:
        # Forzar UTC + sufijo Z.
        ts = datetime.fromtimestamp(record.created, tz=timezone.utc)
        return ts.strftime(datefmt or "%Y-%m-%dT%H:%M:%S") + "Z"

    def format(self, record: logging.LogRecord) -> str:  # type: ignore[override]
        # Color por nivel para terminals; si stdout no es TTY, se ignora porque
        # los códigos siguen siendo caracteres válidos (cosmético).
        msg = super().format(record)
        color = _LEVEL_COLORS.get(record.levelno)
        if color and sys.stderr.isatty():
            msg = f"{color}{msg}{_RESET}"
        # Si hay extras (vía `extra={}`), los añade en una segunda línea.
        extras: list[str] = []
        for key, value in record.__dict__.items():
            if key.startswith("_") or key in (
                "name", "msg", "args", "levelname", "levelno", "pathname",
                "lineno", "funcName", "created", "msecs", "relativeCreated",
                "thread", "threadName", "processName", "process",
                "exc_info", "exc_text", "stack_info", "message", "asctime",
                "taskName",
            ):
                continue
            extras.append(f"{key}={value!r}")
        if extras:
            msg = f"{msg}\n    {', '.join(extras)}"
        return msg


def _safe(value: Any) -> Any:
    """Serializa valores no-JSON-native a strings. Para evitar crashes en
    `json.dumps` con excepciones, sets u objetos raros."""
    try:
        json.dumps(value, default=str)
        return value
    except (TypeError, ValueError):
        return repr(value)


def setup_logging(level: str = "INFO", json_format: bool = False) -> None:
    """
    Configura el logger raíz y los logger names canónicos del proyecto.

    Llamar una sola vez al inicio del proceso (idempotente: reconfigurar
    es seguro). Si ya hay handlers, los reemplaza.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter() if json_format else _HumanFormatter())
    handler.setLevel(level.upper())

    root = logging.getLogger()
    # Idempotencia: borrar handlers previos en el root para que reconfigurar
    # no duplique salida (importar el módulo dos veces, reload, etc.).
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())

    # Forzar niveles de loggers ruidosos a WARNING salvo en DEBUG explícito.
    for noisy in ("httpx", "httpcore", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Convenience: alias a `logging.getLogger(name)`.

    Logger names canónicos: `publisher`, `subscriber`, `api`, `dashboard`.
    El módulo del caller debe prefijar uno de estos para que `LOG_LEVEL`
    funcione como filtro uniforme.
    """
    return logging.getLogger(name)


__all__ = ["setup_logging", "get_logger"]
