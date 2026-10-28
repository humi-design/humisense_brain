"""SSRF and outbound-network policy tests.

Covers T5. The policy must reject every internal, reserved, and metadata destination, and
must not be bypassable through a redirect, a decimal or octal IP encoding, an IPv6
mapped form, or credentials embedded in the URL.
"""

from __future__ import annotations

import socket
from unittest.mock import patch

import pytest

from humisense.config import Settings
from humisense.errors import EgressBlockedError
from humisense.security.egress import EgressPolicy

pytestmark = pytest.mark.security


def make_settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "app_env": "test",
        "egress_allowed_hosts": (),
        "egress_allow_private_networks": False,
        "egress_allow_insecure_http": False,
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


def fake_resolve(address: str) -> object:
    """Patch ``socket.getaddrinfo`` so a hostname resolves to a chosen address."""

    def _getaddrinfo(host: str, port: object, **kwargs: object) -> list[tuple]:
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, 0))]

    return patch("socket.getaddrinfo", side_effect=_getaddrinfo)


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://169.254.170.2/v2/metadata",
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://100.100.100.200/latest/meta-data/",
        "http://[fd00:ec2::254]/latest/meta-data/",
    ],
)
def test_cloud_metadata_endpoints_are_rejected(url: str) -> None:
    policy = EgressPolicy(make_settings())
    with pytest.raises(EgressBlockedError):
        policy.validate(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1/",
        "https://127.0.0.1:5432/",
        "https://[::1]/",
        "https://localhost/",
        "https://10.0.0.5/",
        "https://192.168.1.1/",
        "https://172.16.0.1/",
        "https://169.254.1.1/",
        "https://0.0.0.0/",
        "https://224.0.0.1/",
        "https://240.0.0.1/",
    ],
)
def test_loopback_private_and_reserved_addresses_are_rejected(url: str) -> None:
    policy = EgressPolicy(make_settings())
    with pytest.raises(EgressBlockedError):
        policy.validate(url)


def test_localhost_hostname_resolving_to_loopback_is_rejected() -> None:
    policy = EgressPolicy(make_settings())
    with fake_resolve("127.0.0.1"), pytest.raises(EgressBlockedError):
        policy.validate("https://internal.example.com/")


def test_hostname_resolving_to_private_range_is_rejected() -> None:
    """DNS rebinding to an RFC1918 address must not pass validation."""
    policy = EgressPolicy(make_settings())
    with fake_resolve("10.10.10.10"), pytest.raises(EgressBlockedError):
        policy.validate("https://looks-public.example.com/")


def test_public_address_is_accepted() -> None:
    policy = EgressPolicy(make_settings())
    with fake_resolve("93.184.216.34"):
        destination = policy.validate("https://example.com/api")
    assert destination.hostname == "example.com"
    assert destination.port == 443
    assert destination.is_https is True


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "gopher://example.com/",
        "ftp://example.com/",
        "dict://example.com/",
        "ldap://example.com/",
        "javascript:alert(1)",
        "data:text/plain,hello",
        "//example.com/path",
        "example.com/path",
    ],
)
def test_non_http_schemes_are_rejected(url: str) -> None:
    policy = EgressPolicy(make_settings())
    with pytest.raises(EgressBlockedError):
        policy.validate(url)


def test_plaintext_http_is_rejected_by_default() -> None:
    policy = EgressPolicy(make_settings())
    with fake_resolve("93.184.216.34"), pytest.raises(EgressBlockedError):
        policy.validate("http://example.com/")


def test_plaintext_http_allowed_only_when_enabled() -> None:
    policy = EgressPolicy(make_settings(egress_allow_insecure_http=True))
    with fake_resolve("93.184.216.34"):
        destination = policy.validate("http://example.com/")
    assert destination.scheme == "http"
    assert destination.is_https is False


@pytest.mark.parametrize(
    "url",
    [
        "https://user:password@example.com/",
        "https://admin@example.com/",
        "https://user:pass@169.254.169.254/",
    ],
)
def test_embedded_credentials_are_rejected(url: str) -> None:
    policy = EgressPolicy(make_settings())
    with pytest.raises(EgressBlockedError):
        policy.validate(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/\nHost: evil.com",
        "https://example.com/\r\nX-Injected: 1",
        "https://example.com/\x00",
    ],
)
def test_control_characters_are_rejected(url: str) -> None:
    policy = EgressPolicy(make_settings())
    with pytest.raises(EgressBlockedError):
        policy.validate(url)


def test_overlong_url_is_rejected() -> None:
    policy = EgressPolicy(make_settings())
    with pytest.raises(EgressBlockedError):
        policy.validate("https://example.com/" + "a" * 3000)


def test_empty_url_is_rejected() -> None:
    policy = EgressPolicy(make_settings())
    with pytest.raises(EgressBlockedError):
        policy.validate("")


def test_url_without_host_is_rejected() -> None:
    policy = EgressPolicy(make_settings())
    with pytest.raises(EgressBlockedError):
        policy.validate("https:///path")


def test_unresolvable_hostname_is_rejected() -> None:
    policy = EgressPolicy(make_settings())
    with pytest.raises(EgressBlockedError):
        policy.validate("https://this-host-does-not-exist-abcxyz.invalid/")


def test_redirect_target_is_revalidated() -> None:
    """A redirect to a metadata address must be refused, not followed."""
    policy = EgressPolicy(make_settings())
    with pytest.raises(EgressBlockedError):
        policy.validate_redirect("http://169.254.169.254/latest/meta-data/")


def test_redirect_is_refused_when_disabled() -> None:
    policy = EgressPolicy(make_settings(egress_max_redirects=0))
    with pytest.raises(EgressBlockedError):
        policy.validate_redirect("https://example.com/next")


def test_redirect_to_public_host_is_allowed() -> None:
    policy = EgressPolicy(make_settings())
    with fake_resolve("93.184.216.34"):
        destination = policy.validate_redirect("https://cdn.example.com/asset")
    assert destination.hostname == "cdn.example.com"


def test_private_network_requires_explicit_allowlist() -> None:
    """T5: opening private networks without naming hosts must be refused at startup."""
    settings = make_settings(
        app_env="production",
        egress_allow_private_networks=True,
        egress_allowed_hosts=(),
        auth_mode="oidc",
        oidc_issuer="https://idp.example.com",
        oidc_audience="humisense",
        session_secret="a-sufficiently-long-production-session-secret",
        secrets_provider="env",
        cors_origins=("https://app.example.com",),
    )
    from humisense.errors import ConfigurationError

    with pytest.raises(ConfigurationError):
        settings.validate_for_production()


def test_allowlisted_private_host_is_permitted_when_administratively_enabled() -> None:
    """A documented internal integration must be expressible without disabling SSRF
    protection globally."""
    policy = EgressPolicy(
        make_settings(
            egress_allow_private_networks=True,
            egress_allowed_hosts=("historian.internal", "10.20.30.0/24"),
        )
    )
    with fake_resolve("10.20.30.40"):
        destination = policy.validate("https://historian.internal/api")
    assert destination.hostname == "historian.internal"


def test_allowlisted_private_host_outside_the_cidr_is_still_rejected() -> None:
    policy = EgressPolicy(
        make_settings(
            egress_allow_private_networks=True,
            egress_allowed_hosts=("historian.internal", "10.20.30.0/24"),
        )
    )
    with fake_resolve("10.99.99.99"), pytest.raises(EgressBlockedError):
        policy.validate("https://historian.internal/api")


def test_private_networks_still_rejected_when_not_enabled() -> None:
    policy = EgressPolicy(make_settings(egress_allowed_hosts=("historian.internal",)))
    with fake_resolve("10.20.30.40"), pytest.raises(EgressBlockedError):
        policy.validate("https://historian.internal/api")


@pytest.mark.parametrize(
    "url",
    [
        "https://2130706433/",
        "https://0177.0.0.1/",
        "https://0x7f.0.0.1/",
        "https://[::ffff:127.0.0.1]/",
        "https://[::ffff:169.254.169.254]/",
    ],
)
def test_alternate_ip_encodings_are_rejected(url: str) -> None:
    """Decimal, octal, hex, and IPv4-mapped forms must not bypass the address check."""
    policy = EgressPolicy(make_settings())
    with pytest.raises(EgressBlockedError):
        policy.validate(url)


def test_safe_host_label_omits_query_string() -> None:
    policy = EgressPolicy(make_settings())
    label = policy.safe_host_label("https://example.com/path?token=secret-value")
    assert label == "https://example.com"
    assert "secret-value" not in label


def test_policy_exposes_timeouts_and_limits() -> None:
    policy = EgressPolicy(
        make_settings(
            egress_connect_timeout_seconds=3.0,
            egress_read_timeout_seconds=7.0,
            egress_max_response_bytes=1024,
            egress_max_redirects=1,
        )
    )
    assert policy.connect_timeout == 3.0
    assert policy.read_timeout == 7.0
    assert policy.max_response_bytes == 1024
    assert policy.max_redirects == 1


def test_blocked_error_does_not_echo_the_full_url() -> None:
    policy = EgressPolicy(make_settings())
    with pytest.raises(EgressBlockedError) as excinfo:
        policy.validate("https://user:hunter2@169.254.169.254/secret?q=1")
    assert "hunter2" not in excinfo.value.public_message
    assert "169.254.169.254" not in excinfo.value.public_message
