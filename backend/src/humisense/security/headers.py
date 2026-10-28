"""Secure response headers and trusted-proxy handling.

``client_ip`` is only trusted when the deployment declares how many proxies sit in front
of the application. An untrusted ``X-Forwarded-For`` header is a rate-limit bypass, so it
is ignored unless the topology is declared.
"""

from __future__ import annotations

from starlette.datastructures import Headers
from starlette.requests import Request

CONTENT_SECURITY_POLICY = (
    "default-src 'none'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "font-src 'self'; "
    "connect-src 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'none'; "
    "object-src 'none'"
)

SECURITY_HEADERS: dict[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=(), payment=()",
    "Cache-Control": "no-store",
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Permitted-Cross-Domain-Policies": "none",
}

HSTS_HEADER = ("Strict-Transport-Security", "max-age=31536000; includeSubDomains; preload")


def security_headers(*, production: bool) -> dict[str, str]:
    """Return the response headers applied to every API response."""
    headers = dict(SECURITY_HEADERS)
    if production:
        headers[HSTS_HEADER[0]] = HSTS_HEADER[1]
    return headers


def client_ip(request: Request, *, trusted_proxy_count: int) -> str:
    """Return the client IP, honoring forwarding headers only from trusted proxies.

    Args:
        request: The incoming request.
        trusted_proxy_count: Number of proxies the deployment declares. Zero means the
            socket address is authoritative and forwarding headers are ignored.
    """
    peer = request.client.host if request.client else "unknown"
    if trusted_proxy_count <= 0:
        return peer

    headers = Headers(scope=request.scope)
    forwarded = headers.get("x-forwarded-for")
    if not forwarded:
        return peer

    chain = [part.strip() for part in forwarded.split(",") if part.strip()]
    if not chain:
        return peer
    # The rightmost entries were appended by our own proxies; the client is the entry
    # immediately before them.
    index = max(len(chain) - trusted_proxy_count, 0)
    candidate = chain[index]
    return candidate if _looks_like_ip(candidate) else peer


def _looks_like_ip(value: str) -> bool:
    import ipaddress

    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def is_secure_request(request: Request, *, trusted_proxy_count: int) -> bool:
    """Return True when the request arrived over TLS."""
    if request.url.scheme == "https":
        return True
    if trusted_proxy_count > 0:
        proto = Headers(scope=request.scope).get("x-forwarded-proto")
        if proto and proto.split(",")[0].strip().lower() == "https":
            return True
    return False
