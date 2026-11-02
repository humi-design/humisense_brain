"""F5: DNS rebinding / TOCTOU tests for the address-pinned outbound transport.

These tests verify the *actual connection destination*, not the validation function in
isolation. A recording network backend observes the exact ``(host, port)`` the transport
asks to connect to, so a test can prove the connection used a validated IP literal rather
than a hostname that could be re-resolved to an internal address.

A real local HTTP/TLS server is used where the test needs a completed connection; the
egress policy is pointed at it with a narrow allowlist and private networks opened for
that one host, which is the documented way to express an internal integration.
"""

from __future__ import annotations

import contextlib
import datetime
import socket
import ssl
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpcore
import httpx
import pytest

from humisense.config import Settings
from humisense.errors import EgressBlockedError
from humisense.security.egress import EgressPolicy
from humisense.security.pinned_transport import (
    PinnedAsyncHTTPTransport,
    PinnedAsyncNetworkBackend,
    _peer_matches_address,
)

pytestmark = pytest.mark.security

# Captured before any patching so a fake resolver can delegate real lookups.
_REAL_GETADDRINFO = socket.getaddrinfo


# --- Settings and resolver helpers --------------------------------------


def make_settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "app_env": "test",
        "egress_allowed_hosts": (),
        "egress_allow_private_networks": False,
        "egress_allow_insecure_http": False,
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


def resolver_for(mapping: dict[str, list[str]]) -> Any:
    """Patch ``socket.getaddrinfo`` so named hosts resolve to chosen addresses.

    Only the named hosts are intercepted; everything else (including IP literals the
    transport connects to) uses the real resolver, so the patch cannot make a connection
    appear to succeed by rewriting the destination.
    """

    def _getaddrinfo(host: str, port: object, **kwargs: object) -> list[tuple]:
        if host in mapping:
            infos = []
            for address in mapping[host]:
                family = socket.AF_INET6 if ":" in address else socket.AF_INET
                infos.append((family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, 0)))
            return infos
        return _REAL_GETADDRINFO(host, port, **kwargs)  # type: ignore[arg-type]

    return patch("socket.getaddrinfo", side_effect=_getaddrinfo)


# --- Recording network backend ------------------------------------------


class RecordingBackend(httpcore.AsyncNetworkBackend):
    """Records every connect target and delegates the socket work to AnyIOBackend."""

    def __init__(self, *, fail_all: bool = False) -> None:
        self.connected: list[tuple[str, int]] = []
        self._inner = httpcore.AnyIOBackend()
        self._fail_all = fail_all

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any | None = None,
    ) -> httpcore.AsyncNetworkStream:
        self.connected.append((host, port))
        if self._fail_all:
            raise httpcore.ConnectError("injected: no permitted address could be connected")
        return await self._inner.connect_tcp(
            host, port, timeout=timeout, local_address=local_address, socket_options=socket_options
        )

    async def connect_unix_socket(
        self, path: str, timeout: float | None = None, socket_options: Any | None = None
    ) -> httpcore.AsyncNetworkStream:
        raise httpcore.ConnectError("unix sockets are not permitted")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class PeerMismatchBackend(httpcore.AsyncNetworkBackend):
    """Connects for real but reports a peer address that is not the validated one."""

    def __init__(self) -> None:
        self._inner = httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any | None = None,
    ) -> httpcore.AsyncNetworkStream:
        stream = await self._inner.connect_tcp(
            host, port, timeout=timeout, local_address=local_address, socket_options=socket_options
        )
        return _LyingStream(stream)

    async def connect_unix_socket(
        self, path: str, timeout: float | None = None, socket_options: Any | None = None
    ) -> httpcore.AsyncNetworkStream:
        raise httpcore.ConnectError("unix sockets are not permitted")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class _LyingStream(httpcore.AsyncNetworkStream):
    """Wraps a real stream but reports a different server address."""

    def __init__(self, inner: httpcore.AsyncNetworkStream) -> None:
        self._inner = inner

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        return await self._inner.read(max_bytes, timeout=timeout)

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        await self._inner.write(buffer, timeout=timeout)

    async def aclose(self) -> None:
        await self._inner.aclose()

    async def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.AsyncNetworkStream:
        return await self._inner.start_tls(
            ssl_context, server_hostname=server_hostname, timeout=timeout
        )

    def get_extra_info(self, info: str) -> Any:
        if info == "server_addr":
            return ("10.0.0.5", 443)  # not the validated address
        return self._inner.get_extra_info(info)


# --- Real local servers -------------------------------------------------


class _RecordingHandler(BaseHTTPRequestHandler):
    server_version = "TestServer"
    # HTTP/1.1 enables keep-alive, so a pooled connection can be reused across requests.
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        self.server.requests.append(  # type: ignore[attr-defined]
            {"path": self.path, "host": self.headers.get("Host")}
        )
        if self.path.startswith("/redirect"):
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:  # silence the test log
        return


class LocalHttpServer:
    """A real threaded HTTP server bound to loopback."""

    def __init__(self) -> None:
        # Bound to all interfaces so a test can use more than one loopback literal
        # (127.0.0.1 and 127.0.0.2) against the same server. The port is ephemeral.
        self._server = ThreadingHTTPServer(("0.0.0.0", 0), _RecordingHandler)
        self._server.requests = []  # type: ignore[attr-defined]
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def requests(self) -> list[dict[str, Any]]:
        return self._server.requests  # type: ignore[attr-defined]

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def http_server() -> Iterator[LocalHttpServer]:
    server = LocalHttpServer()
    try:
        yield server
    finally:
        server.close()


def _make_self_signed(hostname: str, directory: Path) -> tuple[Path, Path]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path = directory / "cert.pem"
    key_path = directory / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


class LocalTlsServer:
    """A real TLS server bound to loopback that records the SNI it received."""

    def __init__(self, cert: Path, key: Path) -> None:
        self._context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self._context.load_cert_chain(str(cert), str(key))
        self.sni: str | None = None

        def _capture_sni(
            _socket: ssl.SSLSocket, server_name: str | None, _context: ssl.SSLContext
        ) -> None:
            self.sni = server_name

        self._context.sni_callback = _capture_sni
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(5)
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            try:
                tls = self._context.wrap_socket(conn, server_side=True)
                tls.recv(65536)
                tls.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
                tls.close()
            except Exception:  # noqa: BLE001 - a failed handshake is an expected outcome
                with contextlib.suppress(OSError):
                    conn.close()

    def close(self) -> None:
        self._sock.close()


@pytest.fixture
def tls_material(tmp_path: Path) -> tuple[Path, Path]:
    return _make_self_signed("pinned.example.com", tmp_path)


# --- Unit: peer identity check ------------------------------------------


def test_peer_matches_address_accepts_the_validated_address() -> None:
    assert _peer_matches_address(("93.184.216.34", 443), "93.184.216.34") is True
    assert _peer_matches_address(("::1", 443, 0, 0), "::1") is True


def test_peer_matches_address_rejects_a_different_address() -> None:
    assert _peer_matches_address(("10.0.0.5", 443), "93.184.216.34") is False


def test_peer_matches_address_rejects_unreadable_peer() -> None:
    assert _peer_matches_address(None, "93.184.216.34") is False
    assert _peer_matches_address((), "93.184.216.34") is False
    assert _peer_matches_address(("not-an-ip", 1), "93.184.216.34") is False


# --- Connection-time validation -----------------------------------------


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",  # IPv4 loopback
        "::1",  # IPv6 loopback
        "10.0.0.5",  # RFC1918
        "169.254.169.254",  # cloud metadata
        "169.254.1.1",  # link-local
        "fe80::1",  # IPv6 link-local
        "0.0.0.0",  # unspecified
        "224.0.0.1",  # multicast
    ],
)
def test_resolve_for_connection_rejects_internal_addresses(address: str) -> None:
    policy = EgressPolicy(make_settings())
    with resolver_for({"rebind.example.com": [address]}), pytest.raises(EgressBlockedError):
        policy.resolve_for_connection("rebind.example.com", 443)


def test_resolve_for_connection_accepts_a_public_address() -> None:
    policy = EgressPolicy(make_settings())
    with resolver_for({"public.example.com": ["93.184.216.34"]}):
        addresses = policy.resolve_for_connection("public.example.com", 443)
    assert addresses == ("93.184.216.34",)


def test_resolve_for_connection_refuses_a_mixed_answer() -> None:
    """A single private address in the answer set must fail the whole resolution."""
    policy = EgressPolicy(make_settings())
    with (
        resolver_for({"mixed.example.com": ["93.184.216.34", "10.0.0.5"]}),
        pytest.raises(EgressBlockedError),
    ):
        policy.resolve_for_connection("mixed.example.com", 443)


def test_resolve_for_connection_refuses_a_metadata_hostname() -> None:
    policy = EgressPolicy(make_settings())
    with pytest.raises(EgressBlockedError):
        policy.resolve_for_connection("metadata.google.internal", 80)


def test_resolve_for_connection_surfaces_a_dns_failure() -> None:
    policy = EgressPolicy(make_settings())
    with (
        patch("socket.getaddrinfo", side_effect=socket.gaierror("no such host")),
        pytest.raises(EgressBlockedError),
    ):
        policy.resolve_for_connection("gone.example.com", 443)


def test_resolve_for_connection_rejects_control_characters() -> None:
    policy = EgressPolicy(make_settings())
    with pytest.raises(EgressBlockedError):
        policy.resolve_for_connection("evil.example.com\r\nHost: x", 443)


def test_resolve_for_connection_rejects_a_nonstandard_port_for_an_unlisted_host() -> None:
    policy = EgressPolicy(make_settings())
    with (
        resolver_for({"public.example.com": ["93.184.216.34"]}),
        pytest.raises(EgressBlockedError),
    ):
        policy.resolve_for_connection("public.example.com", 8443)


def test_resolve_for_connection_allows_a_nonstandard_port_for_an_allowlisted_host() -> None:
    policy = EgressPolicy(make_settings(egress_allowed_hosts=("internal.example.com",)))
    with resolver_for({"internal.example.com": ["93.184.216.34"]}):
        addresses = policy.resolve_for_connection("internal.example.com", 8443)
    assert addresses == ("93.184.216.34",)


def test_url_validation_rejects_a_nonstandard_port_for_an_unlisted_host() -> None:
    policy = EgressPolicy(make_settings())
    with (
        resolver_for({"public.example.com": ["93.184.216.34"]}),
        pytest.raises(EgressBlockedError),
    ):
        policy.validate("https://public.example.com:8443/api")


def test_url_validation_allows_a_nonstandard_port_for_an_allowlisted_host() -> None:
    policy = EgressPolicy(make_settings(egress_allowed_hosts=("internal.example.com",)))
    with resolver_for({"internal.example.com": ["93.184.216.34"]}):
        destination = policy.validate("https://internal.example.com:8443/api")
    assert destination.port == 8443


# --- The transport actually connects to a validated literal -------------


async def test_transport_connects_to_the_validated_ip_not_the_hostname(
    http_server: LocalHttpServer,
) -> None:
    """The connection target must be the validated IP literal, with Host preserved."""
    policy = EgressPolicy(
        make_settings(
            egress_allowed_hosts=("pinned.example.com",),
            egress_allow_private_networks=True,
            egress_allow_insecure_http=True,
        )
    )
    backend = RecordingBackend()
    transport = PinnedAsyncHTTPTransport(
        policy=policy, network_backend=PinnedAsyncNetworkBackend(policy, backend=backend)
    )
    with resolver_for({"pinned.example.com": ["127.0.0.1"]}):
        async with httpx.AsyncClient(transport=transport) as client:
            response = await client.get(f"http://pinned.example.com:{http_server.port}/ok")

    assert response.status_code == 200
    # The transport connected to the literal, never the hostname.
    assert backend.connected == [("127.0.0.1", http_server.port)]
    # The Host header still carries the original hostname.
    assert http_server.requests[0]["host"] == f"pinned.example.com:{http_server.port}"


async def test_rebinding_between_url_check_and_connect_is_refused(
    http_server: LocalHttpServer,
) -> None:
    """The core F5 test: a DNS answer that changes after validation must be caught.

    The first resolution (the URL check) returns a public address and passes. The second
    resolution (at connection time) returns loopback. The transport must refuse to
    connect, and the recording backend must show that no connection was attempted.
    """
    policy = EgressPolicy(make_settings(egress_allow_insecure_http=True))
    backend = RecordingBackend()
    transport = PinnedAsyncHTTPTransport(
        policy=policy, network_backend=PinnedAsyncNetworkBackend(policy, backend=backend)
    )

    calls = {"count": 0}

    def rebinding(host: str, port: object, **kwargs: object) -> list[tuple]:
        calls["count"] += 1
        address = "93.184.216.34" if calls["count"] == 1 else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, 0))]

    with patch("socket.getaddrinfo", side_effect=rebinding):
        # The URL-level check resolves to a public address and passes.
        destination = policy.validate("http://rebind.example.com/")
        assert destination.resolved_addresses == ("93.184.216.34",)

        async with httpx.AsyncClient(transport=transport) as client:
            # The connection-time resolution returns loopback and must be refused.
            with pytest.raises(EgressBlockedError):
                await client.get("http://rebind.example.com/")

    assert calls["count"] >= 2, "the transport must re-resolve at connection time"
    assert backend.connected == [], "no connection may be attempted to a rebound address"


async def test_connect_is_refused_when_the_peer_is_not_the_validated_address(
    http_server: LocalHttpServer,
) -> None:
    """If the socket peer cannot be confirmed, the connection fails closed."""
    policy = EgressPolicy(
        make_settings(
            egress_allowed_hosts=("pinned.example.com",),
            egress_allow_private_networks=True,
            egress_allow_insecure_http=True,
        )
    )
    backend = PeerMismatchBackend()
    transport = PinnedAsyncHTTPTransport(
        policy=policy, network_backend=PinnedAsyncNetworkBackend(policy, backend=backend)
    )
    with resolver_for({"pinned.example.com": ["127.0.0.1"]}):
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(httpx.ConnectError):
                await client.get(f"http://pinned.example.com:{http_server.port}/ok")


# --- Redirects ----------------------------------------------------------


async def test_redirect_to_a_private_address_is_not_followed(
    http_server: LocalHttpServer,
) -> None:
    """A redirect to a metadata address must not be followed by the client."""
    policy = EgressPolicy(
        make_settings(
            egress_allowed_hosts=("pinned.example.com",),
            egress_allow_private_networks=True,
            egress_allow_insecure_http=True,
        )
    )
    backend = RecordingBackend()
    transport = PinnedAsyncHTTPTransport(
        policy=policy, network_backend=PinnedAsyncNetworkBackend(policy, backend=backend)
    )
    with resolver_for({"pinned.example.com": ["127.0.0.1"]}):
        async with httpx.AsyncClient(transport=transport, follow_redirects=False) as client:
            response = await client.get(f"http://pinned.example.com:{http_server.port}/redirect")

    # The 302 is returned as-is; the client did not chase it.
    assert response.status_code == 302
    assert len(backend.connected) == 1
    assert backend.connected[0] == ("127.0.0.1", http_server.port)
    # And the redirect target itself is refused by policy if it were ever followed.
    with pytest.raises(EgressBlockedError):
        policy.validate_redirect("http://169.254.169.254/latest/meta-data/")


async def test_redirect_chain_is_not_followed(
    http_server: LocalHttpServer,
) -> None:
    """Even a chain of redirects is not chased, because none are followed."""
    policy = EgressPolicy(
        make_settings(
            egress_allowed_hosts=("pinned.example.com",),
            egress_allow_private_networks=True,
            egress_allow_insecure_http=True,
        )
    )
    backend = RecordingBackend()
    transport = PinnedAsyncHTTPTransport(
        policy=policy, network_backend=PinnedAsyncNetworkBackend(policy, backend=backend)
    )
    with resolver_for({"pinned.example.com": ["127.0.0.1"]}):
        async with httpx.AsyncClient(transport=transport, follow_redirects=False) as client:
            response = await client.get(f"http://pinned.example.com:{http_server.port}/redirect")

    assert response.status_code == 302
    assert len(backend.connected) == 1, "only the original validated destination is contacted"


# --- TLS: SNI, certificate verification, and Host -----------------------


async def test_tls_verification_uses_the_hostname_and_preserves_sni(
    tls_material: tuple[Path, Path],
) -> None:
    """The pinned connection still verifies the certificate against the hostname."""
    cert_path, key_path = tls_material
    server = LocalTlsServer(cert_path, key_path)
    try:
        policy = EgressPolicy(
            make_settings(
                egress_allowed_hosts=("pinned.example.com",),
                egress_allow_private_networks=True,
            )
        )
        # Trust exactly the test certificate, so verification is real but anchored.
        context = ssl.create_default_context(cafile=str(cert_path))
        transport = PinnedAsyncHTTPTransport(policy=policy, verify=context)
        with resolver_for({"pinned.example.com": ["127.0.0.1"]}):
            async with httpx.AsyncClient(transport=transport) as client:
                response = await client.get(f"https://pinned.example.com:{server.port}/ok")
        assert response.status_code == 200
        # SNI carried the original hostname, not the IP literal.
        assert server.sni == "pinned.example.com"
    finally:
        server.close()


async def test_tls_verification_fails_for_an_untrusted_certificate(
    tls_material: tuple[Path, Path],
) -> None:
    """A certificate that does not validate against the hostname must abort the handshake."""
    cert_path, key_path = tls_material
    server = LocalTlsServer(cert_path, key_path)
    try:
        policy = EgressPolicy(
            make_settings(
                egress_allowed_hosts=("pinned.example.com",),
                egress_allow_private_networks=True,
            )
        )
        # Default trust store: the self-signed certificate is not trusted.
        transport = PinnedAsyncHTTPTransport(policy=policy)
        with resolver_for({"pinned.example.com": ["127.0.0.1"]}):
            async with httpx.AsyncClient(transport=transport) as client:
                with pytest.raises(httpx.ConnectError):
                    await client.get(f"https://pinned.example.com:{server.port}/ok")
    finally:
        server.close()


# --- Pooling, retries, proxies, unix sockets ----------------------------


async def test_connection_pool_reuses_a_validated_connection(
    http_server: LocalHttpServer,
) -> None:
    """A keep-alive connection is reused, so the connection is validated once."""
    policy = EgressPolicy(
        make_settings(
            egress_allowed_hosts=("pinned.example.com",),
            egress_allow_private_networks=True,
            egress_allow_insecure_http=True,
        )
    )
    backend = RecordingBackend()
    transport = PinnedAsyncHTTPTransport(
        policy=policy, network_backend=PinnedAsyncNetworkBackend(policy, backend=backend)
    )
    with resolver_for({"pinned.example.com": ["127.0.0.1"]}):
        async with httpx.AsyncClient(transport=transport) as client:
            await client.get(f"http://pinned.example.com:{http_server.port}/a")
            await client.get(f"http://pinned.example.com:{http_server.port}/b")

    assert len(backend.connected) == 1, "the pooled connection must be reused"
    assert backend.connected[0] == ("127.0.0.1", http_server.port)


async def test_pool_does_not_share_connections_across_hosts(
    http_server: LocalHttpServer,
) -> None:
    """Two hostnames that resolve to the same IP must not share a pooled connection."""
    policy = EgressPolicy(
        make_settings(
            egress_allowed_hosts=("one.example.com", "two.example.com"),
            egress_allow_private_networks=True,
            egress_allow_insecure_http=True,
        )
    )
    backend = RecordingBackend()
    transport = PinnedAsyncHTTPTransport(
        policy=policy, network_backend=PinnedAsyncNetworkBackend(policy, backend=backend)
    )
    with resolver_for({"one.example.com": ["127.0.0.1"], "two.example.com": ["127.0.0.1"]}):
        async with httpx.AsyncClient(transport=transport) as client:
            await client.get(f"http://one.example.com:{http_server.port}/a")
            await client.get(f"http://two.example.com:{http_server.port}/b")

    assert len(backend.connected) == 2
    hosts = {request["host"] for request in http_server.requests}
    assert hosts == {
        f"one.example.com:{http_server.port}",
        f"two.example.com:{http_server.port}",
    }


async def test_retry_falls_back_to_another_validated_address(
    http_server: LocalHttpServer,
) -> None:
    """When one validated address fails, another validated address is tried."""
    policy = EgressPolicy(
        make_settings(
            egress_allowed_hosts=("multi.example.com",),
            egress_allow_private_networks=True,
            egress_allow_insecure_http=True,
        )
    )

    class FirstFailsBackend(RecordingBackend):
        async def connect_tcp(
            self,
            host: str,
            port: int,
            timeout: float | None = None,
            local_address: str | None = None,
            socket_options: Any | None = None,
        ) -> httpcore.AsyncNetworkStream:
            self.connected.append((host, port))
            if host == "127.0.0.1":  # a validated literal that refuses connections
                raise httpcore.ConnectError("injected failure on the first address")
            return await self._inner.connect_tcp(
                host,
                port,
                timeout=timeout,
                local_address=local_address,
                socket_options=socket_options,
            )

    backend = FirstFailsBackend()
    transport = PinnedAsyncHTTPTransport(
        policy=policy, network_backend=PinnedAsyncNetworkBackend(policy, backend=backend)
    )
    with resolver_for({"multi.example.com": ["127.0.0.1", "127.0.0.2"]}):
        async with httpx.AsyncClient(transport=transport) as client:
            response = await client.get(f"http://multi.example.com:{http_server.port}/ok")

    assert response.status_code == 200
    # Both attempts were validated literals; the second succeeded. Addresses are tried in
    # the policy's deterministic (sorted) order.
    assert backend.connected == [
        ("127.0.0.1", http_server.port),
        ("127.0.0.2", http_server.port),
    ]


async def test_all_addresses_failing_surfaces_a_connect_error() -> None:
    policy = EgressPolicy(make_settings())
    backend = RecordingBackend(fail_all=True)
    transport = PinnedAsyncHTTPTransport(
        policy=policy, network_backend=PinnedAsyncNetworkBackend(policy, backend=backend)
    )
    with resolver_for({"unreachable.example.com": ["93.184.216.34"]}):
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(httpx.ConnectError):
                await client.get("http://unreachable.example.com/")


async def test_environment_proxy_is_ignored(
    http_server: LocalHttpServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A proxy from the environment must not become a second, unpinned path."""
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    policy = EgressPolicy(
        make_settings(
            egress_allowed_hosts=("pinned.example.com",),
            egress_allow_private_networks=True,
            egress_allow_insecure_http=True,
        )
    )
    backend = RecordingBackend()
    transport = PinnedAsyncHTTPTransport(
        policy=policy, network_backend=PinnedAsyncNetworkBackend(policy, backend=backend)
    )
    with resolver_for({"pinned.example.com": ["127.0.0.1"]}):
        async with httpx.AsyncClient(transport=transport) as client:
            response = await client.get(f"http://pinned.example.com:{http_server.port}/ok")

    assert response.status_code == 200
    assert backend.connected == [("127.0.0.1", http_server.port)]


async def test_unix_sockets_are_refused() -> None:
    policy = EgressPolicy(make_settings())
    backend = PinnedAsyncNetworkBackend(policy)
    with pytest.raises(httpcore.ConnectError):
        await backend.connect_unix_socket("/tmp/does-not-matter.sock")


# --- Integration: the ingestion sink uses the pinned transport ----------


async def test_http_ingestion_sink_uses_the_pinned_transport(
    http_server: LocalHttpServer,
) -> None:
    """The sink's client must be built on the pinned transport, not a default one."""
    from humisense.services.ingestion import HttpIngestionSink, RetryPolicy

    policy = EgressPolicy(
        make_settings(
            egress_allowed_hosts=("pinned.example.com",),
            egress_allow_private_networks=True,
            egress_allow_insecure_http=True,
        )
    )
    sink = None
    with resolver_for({"pinned.example.com": ["127.0.0.1"]}):
        sink = HttpIngestionSink(
            base_url=f"http://pinned.example.com:{http_server.port}",
            bearer_token="token",
            egress_policy=policy,
            retry_policy=RetryPolicy(),
        )
        client = sink._client_or_create()
        assert isinstance(client._transport, PinnedAsyncHTTPTransport)
    assert sink is not None
    await sink.close()
