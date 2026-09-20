"""
Tests de redacción del helper `_safe_redis_url` (T-PHB-003, R1-001).

REQ-PHB-001: la URL con credenciales embebidas NO debe aparecer en los logs
de diagnóstico. El helper conserva esquema, host (incluido IPv6 con sus
corchetes) y puerto, y descarta el userinfo ``user:password@``.

Estos tests son ortogonales a PR-B: prueban el helper de forma aislada, sin
necesidad del resto del publisher. Mantienen el PR-A testeable sin esperar
a la suite completa de `test_publisher_main.py` (que llegará en PR-B con
T-PHB-007).
"""

from __future__ import annotations

import pytest

from publisher.main import _safe_redis_url


@pytest.mark.parametrize(
    "raw, expected",
    [
        pytest.param(
            "redis://user:secret@redis:6379/0",
            "redis://redis:6379/0",
            id="password-embedded",
        ),
        pytest.param(
            "redis://[::1]:6379/0",
            "redis://[::1]:6379/0",
            id="ipv6-with-port",
        ),
        pytest.param(
            "redis://localhost",
            "redis://localhost",
            id="missing-port",
        ),
        pytest.param(
            "redis://redis:6379/0",
            "redis://redis:6379/0",
            id="no-credentials-passthrough",
        ),
    ],
)
def test_safe_redis_url_strips_userinfo(raw, expected):
    """`raw` con credenciales → `expected` sin userinfo; sin userinfo → byte-identical."""
    redacted = _safe_redis_url(raw)

    assert redacted == expected
    assert "user" not in redacted
    assert "secret" not in redacted
    assert "@" not in redacted


def test_safe_redis_url_preserves_tls_scheme():
    """`rediss://` (TLS) mantiene el esquema y descarta el userinfo."""
    assert _safe_redis_url("rediss://u:p@host:6380/0") == "rediss://host:6380/0"


def test_safe_redis_url_ipv6_without_port():
    """IPv6 sin puerto → conserva corchetes, sin dos puntos colgantes."""
    assert _safe_redis_url("redis://[::1]") == "redis://[::1]"