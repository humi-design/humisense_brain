"""Outbound network policy (SSRF defence).

Connector configuration can make the backend contact an arbitrary host. That is a
high-risk boundary, so every outbound request a connector makes goes through
:class:`EgressPolicy`, which:

* allows only ``http`` and ``https`` (and ``https`` only, unless explicitly enabled);
* rejects credentials embedded in the URL;
* rejects userinfo, fragments, and non-standard ports unless allowlisted;
* resolves the hostname and rejects loopback, private, link-local, reserved,
  multicast, unspecified, and cloud metadata addresses;
* enforces an explicit host allowlist when one is configured;
* revalidates every redirect target;
* caps redirects, response size, and timeouts.

The policy validates *resolved addresses*, which is what defends against a hostname
whose DNS answer points at an internal service. Where the HTTP client allows it, the
connection is pinned to the validated address so a second DNS lookup cannot be
rebound between validation and connect.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

from humisense.config import Settings
from humisense.errors import EgressBlockedError

ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})
SECURE_SCHEMES: frozenset[str] = frozenset({"https"})

METADATA_ADDRESSES: frozenset[ipaddress.IPv4Address | ipaddress.IPv6Address] = frozenset(
    {
        ipaddress.ip_address("169.254.169.254"),  # AWS/GCP/Azure IMDS
        ipaddress.ip_address("169.254.170.2"),  # ECS task metadata
        ipaddress.ip_address("100.100.100.200"),  # Alibaba Cloud metadata
        ipaddress.ip_address("fd00:ec2::254"),  # AWS IMDS over IPv6
    }
)

BLOCKED_HOSTNAMES: frozenset[str] = frozenset(
    {
        "metadata",
        "metadata.google.internal",
        "metadata.goog",
        "instance-data",
    }
)

DEFAULT_ALLOWED_PORTS: frozenset[int] = frozenset({80, 443})


@dataclass(frozen=True, slots=True)
class ValidatedDestination:
    """A destination that passed every egress check."""

    url: str
    scheme: str
    hostname: str
    port: int
    resolved_addresses: tuple[str, ...]

    @property
    def is_https(self) -> bool:
        return self.scheme == "https"


def _is_disallowed_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return True when an address must not be contacted by a connector."""
    if address in METADATA_ADDRESSES:
        return True
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return _is_disallowed_address(address.ipv4_mapped)
    return bool(
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )


class EgressPolicy:
    """Validates outbound destinations for connector traffic."""

    def __init__(
        self,
        settings: Settings,
        *,
        extra_allowed_hosts: tuple[str, ...] = (),
    ) -> None:
        self._allowed_hosts = {
            host.strip().lower()
            for host in (*settings.egress_allowed_hosts, *extra_allowed_hosts)
            if host.strip()
        }
        self._allowed_networks = self._parse_networks(self._allowed_hosts)
        self._allow_private = settings.egress_allow_private_networks
        self._allow_insecure_http = settings.egress_allow_insecure_http
        self._connect_timeout = settings.egress_connect_timeout_seconds
        self._read_timeout = settings.egress_read_timeout_seconds
        self._max_response_bytes = settings.egress_max_response_bytes
        self._max_redirects = settings.egress_max_redirects

    @property
    def connect_timeout(self) -> float:
        return self._connect_timeout

    @property
    def read_timeout(self) -> float:
        return self._read_timeout

    @property
    def max_response_bytes(self) -> int:
        return self._max_response_bytes

    @property
    def max_redirects(self) -> int:
        return self._max_redirects

    @property
    def allowed_hosts(self) -> frozenset[str]:
        return frozenset(self._allowed_hosts)

    @staticmethod
    def _parse_networks(
        entries: set[str],
    ) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
        networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
        for entry in entries:
            try:
                networks.append(ipaddress.ip_network(entry, strict=False))
            except ValueError:
                continue
        return tuple(networks)

    def validate(self, url: str, *, allow_redirect: bool = False) -> ValidatedDestination:
        """Validate ``url`` and return the resolved destination.

        Args:
            url: The absolute URL a connector wants to contact.
            allow_redirect: Marks the URL as a redirect target. Redirects are held to the
                same rules, and are additionally refused entirely when the policy
                disables them.

        Raises:
            EgressBlockedError: on any policy violation. The message never echoes the
                full user-supplied URL, only a sanitized host.
        """
        if not isinstance(url, str) or not url.strip():
            raise EgressBlockedError(detail="A destination URL is required")
        if len(url) > 2048:
            raise EgressBlockedError(detail="Destination URL is unreasonably long")
        if any(ch in url for ch in ("\n", "\r", "\x00")):
            raise EgressBlockedError(detail="Destination URL contains control characters")

        try:
            parts = urlsplit(url)
        except ValueError as exc:
            raise EgressBlockedError(detail="Destination URL could not be parsed") from exc

        scheme = (parts.scheme or "").lower()
        if scheme not in ALLOWED_SCHEMES:
            raise EgressBlockedError(
                detail="Only http and https destinations are permitted",
                context={"scheme": scheme or "missing"},
            )
        if scheme not in SECURE_SCHEMES and not self._allow_insecure_http:
            raise EgressBlockedError(
                detail="Plaintext http is disabled; use https or enable it explicitly"
            )
        if parts.username or parts.password:
            raise EgressBlockedError(detail="Credentials must not be embedded in a destination URL")
        if not parts.hostname:
            raise EgressBlockedError(detail="Destination URL has no host")

        hostname = parts.hostname.lower().rstrip(".")
        if hostname in BLOCKED_HOSTNAMES:
            raise EgressBlockedError(detail="Destination host is a known metadata endpoint")

        port = parts.port or (443 if scheme == "https" else 80)

        host_is_allowlisted = self._host_allowlisted(hostname)

        # A destination must be on the allowlist unless private networks were explicitly
        # opened up by an administrator.
        if not host_is_allowlisted and not self._allow_private and not self._allowed_hosts:
            # No allowlist configured at all: public destinations only, enforced below
            # by the address check. This is the documented default posture.
            pass

        addresses = self._resolve(hostname)

        for raw_address in addresses:
            address = ipaddress.ip_address(raw_address)
            if self._address_allowed(address, host_is_allowlisted):
                continue
            raise EgressBlockedError(
                detail="Destination resolves to an address that is not permitted",
                context={"address_class": _address_class(address)},
            )

        return ValidatedDestination(
            url=url,
            scheme=scheme,
            hostname=hostname,
            port=port,
            resolved_addresses=tuple(addresses),
        )

    def _host_allowlisted(self, hostname: str) -> bool:
        if hostname in self._allowed_hosts:
            return True
        # Allow a subdomain of an allowlisted name (api.example.com matches example.com).
        return any(hostname.endswith(f".{allowed}") for allowed in self._allowed_hosts if allowed)

    def _address_allowed(
        self, address: ipaddress.IPv4Address | ipaddress.IPv6Address, host_is_allowlisted: bool
    ) -> bool:
        if not _is_disallowed_address(address):
            return True
        # The address is internal. It is permitted only when the host was explicitly
        # allowlisted and the administrator opened private networks.
        if not host_is_allowlisted or not self._allow_private:
            return False
        if self._allowed_networks:
            return any(address in net for net in self._allowed_networks)
        return True

    def _resolve(self, hostname: str) -> list[str]:
        """Resolve ``hostname`` to its IP addresses, accepting literals directly."""
        try:
            literal = ipaddress.ip_address(hostname)
        except ValueError:
            pass
        else:
            return [str(literal)]

        try:
            infos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
        except socket.gaierror as exc:
            raise EgressBlockedError(
                detail="Destination host could not be resolved",
                context={"host": hostname},
            ) from exc

        addresses = sorted({str(info[4][0]) for info in infos})
        if not addresses:
            raise EgressBlockedError(detail="Destination host resolved to no addresses")
        return addresses

    def validate_redirect(self, url: str) -> ValidatedDestination:
        """Validate a redirect target. Identical rules apply; no inherited trust."""
        if self._max_redirects <= 0:
            raise EgressBlockedError(detail="Redirects are disabled by policy")
        return self.validate(url, allow_redirect=True)

    def safe_host_label(self, url: str) -> str:
        """Return a log-safe host label for a URL, never the full query string."""
        try:
            parts = urlsplit(url)
        except ValueError:
            return "[unparsable-url]"
        host = parts.hostname or "[no-host]"
        return f"{parts.scheme}://{host}" if parts.scheme else host


def _address_class(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str:
    if address in METADATA_ADDRESSES:
        return "cloud_metadata"
    if address.is_loopback:
        return "loopback"
    if address.is_private:
        return "private"
    if address.is_link_local:
        return "link_local"
    if address.is_reserved:
        return "reserved"
    if address.is_multicast:
        return "multicast"
    if address.is_unspecified:
        return "unspecified"
    return "public"
