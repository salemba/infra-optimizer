"""
Auth and network-security helpers shared across all routers.

  _require_key  — FastAPI Security dependency; enforces X-API-Key when API_KEY is set.
  _is_private   — SSRF guard; blocks webhook fan-out to private/loopback addresses.
  limiter       — slowapi rate-limiter instance; attached to app.state in api.py.
"""
from __future__ import annotations

import hmac
import ipaddress
import os
import socket

from fastapi import HTTPException, Security
from fastapi.security.api_key import APIKeyHeader
from slowapi import Limiter
from slowapi.util import get_remote_address

# ── Rate limiter ───────────────────────────────────────────────────────────

limiter = Limiter(key_func=get_remote_address)

# ── API key auth ───────────────────────────────────────────────────────────

_API_KEY    = os.getenv("API_KEY", "")
_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def require_key(key: str | None = Security(_key_header)) -> None:
    """Raise 401 when API_KEY is configured and the header is missing or wrong."""
    if _API_KEY and not hmac.compare_digest(key or "", _API_KEY):
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key.")


# ── SSRF guard ─────────────────────────────────────────────────────────────

def _resolve_is_private(host: str) -> bool:
    """Resolve hostname to IPs and check all of them; fail-safe returns True on error."""
    try:
        infos = socket.getaddrinfo(host, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        return any(
            ipaddress.ip_address(i[4][0]).is_private
            or ipaddress.ip_address(i[4][0]).is_loopback
            or ipaddress.ip_address(i[4][0]).is_link_local
            for i in infos
        )
    except (socket.gaierror, ValueError):
        return True


def is_private(url: str) -> bool:
    """Return True if the URL resolves to a private/loopback address (SSRF protection)."""
    try:
        host = url.split("/")[2].split(":")[0]
        try:
            addr = ipaddress.ip_address(host)
            return addr.is_private or addr.is_loopback or addr.is_link_local
        except ValueError:
            return _resolve_is_private(host)
    except IndexError:
        return True
