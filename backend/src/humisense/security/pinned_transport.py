"""Address-pinned outbound transport (SSRF / DNS-rebinding defence).

:class:`~humisense.security.egress.EgressPolicy` validates *resolved addresses*, but
validating a hostname and then handing that hostname to an ordinary HTTP client leaves a
time-of-check/time-of-use gap: the client resolves the name a second time, and an
attacker who controls DNS can answer the second lookup differently. This module closes
that gap.

The mechanism is an ``httpcore`` network backend that, for every TCP connection:

1. re-resolves the destination and validates **every** returned address against the
   egress policy (fail closed on a mixed or blocked answer);
2. connects to one of the **validated IP literals** rather than the hostname, so no
   second DNS lookup can occur;
3. verifies the connected socket's peer address is the address it validated.

Because the connection is made to the literal, the original hostname is still what
``httpcore`` uses for the TLS ``server_hostname`` (SNI and certificate verification) and
what ``httpx`` uses for the ``Host`` header. Pinning is therefore transparent to the HTTP
layer.

The backend is installed through :class:`PinnedAsyncHTTPTransport`, which reuses
``httpx.AsyncHTTPTransport``'s request handling and exception mapping and only replaces
the connection pool's network backend. Nothing in ``httpx`` or ``httpcore`` is
monkey-patched, and only public API is used.
"""

from __future__ import annotations

import ipaddress
from typing import Any

import httpcore
import httpx

from humisense.security.egress import EgressPolicy


def _peer_matches_address(peer: Any, expected: str) -> bool:
    """Return True when a socket peer address is the address that was validated.

    ``peer`` is the value httpcore exposes as ``server_addr``: an address tuple whose
    first element is the IP, or ``None`` when the backend cannot report it. Anything that
    cannot be read as an address is treated as a mismatch, so identity is never assumed.
    """
    if not peer:
        return False
    try:
        peer_ip = peer[0]
    except (TypeError, IndexError):
        return False
    try:
        return ipaddress.ip_address(peer_ip) == ipaddress.ip_address(expected)
    except ValueError:
        return False


class PinnedAsyncNetworkBackend(httpcore.AsyncNetworkBackend):
    """A network backend that connects only to egress-validated addresses.

    Args:
        policy: The egress policy used to re-resolve and validate every destination.
        backend: The backend used for the actual socket work. Defaults to
            ``httpcore.AnyIOBackend``. A test may inject a recording backend to observe
            the exact connect target.
    """

    def __init__(
        self,
        policy: EgressPolicy,
        *,
        backend: httpcore.AsyncNetworkBackend | None = None,
    ) -> None:
        self._policy = policy
        self._backend = backend if backend is not None else httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any | None = None,
    ) -> httpcore.AsyncNetworkStream:
        """Validate ``host`` and connect to a validated address literal.

        Raises:
            EgressBlockedError: when the host or any resolved address is not permitted.
                This is a policy decision, not a transport failure, and callers treat it
                as permanent.
            httpcore.ConnectError: when no permitted address could be connected, or the
                connected peer could not be confirmed as the validated address.
        """
        permitted = self._policy.resolve_for_connection(host, port)
        last_error: Exception | None = None
        for address in permitted:
            try:
                stream = await self._backend.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except Exception as exc:  # noqa: BLE001 - try the next validated address
                last_error = exc
                continue
            if not _peer_matches_address(stream.get_extra_info("server_addr"), address):
                await stream.aclose()
                last_error = httpcore.ConnectError(
                    "the connected peer was not the validated address"
                )
                continue
            return stream
        raise httpcore.ConnectError(
            f"no permitted address could be connected for {host}"
        ) from last_error

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Any | None = None,
    ) -> httpcore.AsyncNetworkStream:
        """Refuse unix sockets: they are outside the egress policy's reach."""
        raise httpcore.ConnectError("unix domain sockets are not permitted by the egress policy")

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


class PinnedAsyncHTTPTransport(httpx.AsyncHTTPTransport):
    """An ``httpx`` async transport whose connections are address-pinned.

    Only the connection pool is replaced; request handling, response streaming, and
    httpcore-to-httpx exception mapping are inherited from ``httpx.AsyncHTTPTransport``.
    """

    def __init__(
        self,
        *,
        policy: EgressPolicy,
        verify: bool = True,
        limits: httpx.Limits | None = None,
        keepalive_expiry: float | None = None,
        network_backend: httpcore.AsyncNetworkBackend | None = None,
    ) -> None:
        resolved_limits = limits if limits is not None else httpx.Limits()
        # ``trust_env=False`` so a proxy or CA bundle configured through the environment
        # cannot redirect or intercept connector traffic. Certificate verification is
        # always on; there is no configuration that disables it.
        ssl_context = httpx.create_ssl_context(verify=verify, cert=None, trust_env=False)
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl_context,
            max_connections=resolved_limits.max_connections,
            max_keepalive_connections=resolved_limits.max_keepalive_connections,
            keepalive_expiry=keepalive_expiry,
            http1=True,
            http2=False,
            network_backend=network_backend
            if network_backend is not None
            else PinnedAsyncNetworkBackend(policy),
        )
