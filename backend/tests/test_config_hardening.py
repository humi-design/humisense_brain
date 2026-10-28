"""Production configuration hardening tests.

Section 4.1 and 17: a production deployment must refuse an insecure configuration rather
than start with one. These tests assert that each unsafe combination is rejected.
"""

from __future__ import annotations

import base64

import pytest
from pydantic import ValidationError

from humisense.config import Settings
from humisense.errors import ConfigurationError
from humisense.secrets.encrypted_db import KEY_BYTES

pytestmark = pytest.mark.security

A_KEY = base64.b64encode(b"k" * KEY_BYTES).decode()


def production(**overrides: object) -> Settings:
    """Return a baseline production configuration that is expected to validate."""
    defaults: dict[str, object] = {
        "app_env": "production",
        "auth_mode": "oidc",
        "oidc_issuer": "https://identity.example.com",
        "oidc_audience": "humisense-brain",
        "oidc_jwks_url": "https://identity.example.com/.well-known/jwks.json",
        "secrets_provider": "encrypted_db",
        "secrets_master_key": A_KEY,
        "database_url": "postgresql+asyncpg://app:pw@db.internal:5432/humisense",
        "database_require_tls": True,
        "cookie_secure": True,
        "session_secret": "a-sufficiently-long-production-session-secret",
        "cors_origins": ("https://app.example.com",),
        "egress_allowed_hosts": ("historian.example.com",),
        "docs_enabled": False,
        "layer2_mode": "disabled",
        "task_queue_mode": "arq",
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


def reject(**overrides: object) -> None:
    """Assert that a production configuration is refused by the startup gate."""
    with pytest.raises((ConfigurationError, ValidationError)):
        production(**overrides).require_production_ready()


def test_baseline_production_configuration_is_accepted() -> None:
    settings = production()
    settings.require_production_ready()
    assert settings.is_production is True


def test_production_environment_is_recognized() -> None:
    assert Settings(app_env="production").is_production is True


@pytest.mark.parametrize("environment", ["prod", "live", "Production"])
def test_ambiguous_environment_names_are_refused(environment: str) -> None:
    """Only an exact match counts; a near-miss must not silently mean production."""
    with pytest.raises(ValidationError):
        Settings(app_env=environment)


def test_development_mode_is_allowed_outside_production() -> None:
    """A safe development mode is available; it is simply isolated from production."""
    settings = Settings(app_env="development", auth_mode="dev")
    assert settings.auth_mode == "dev"
    assert settings.is_production is False


def test_production_rejects_dev_auth_mode() -> None:
    """T17: the development sign-in shim must never be reachable in production."""
    with pytest.raises(ConfigurationError) as excinfo:
        production(auth_mode="dev").require_production_ready()
    assert "auth_mode" in str(excinfo.value).lower()


def test_production_requires_oidc_configuration() -> None:
    reject(oidc_issuer=None, oidc_audience=None, oidc_jwks_url=None)


def test_production_requires_a_secrets_master_key() -> None:
    reject(secrets_master_key=None)


def test_production_rejects_the_environment_secrets_provider() -> None:
    """The environment provider is for bootstrap, not for customer credentials."""
    reject(secrets_provider="env")


def test_production_forces_secure_cookies() -> None:
    """T9: a session cookie without Secure can be sent over plaintext.

    Production does not merely reject an explicit false: it forces the flag on, so a
    deployment that omits the setting cannot end up with a plaintext-transport cookie.
    """
    settings = production(cookie_secure=False)
    assert settings.effective_cookie_secure is True
    settings.require_production_ready()


def test_development_honours_the_explicit_cookie_flag() -> None:
    assert Settings(app_env="test", cookie_secure=False).effective_cookie_secure is False


def test_production_requires_an_explicit_cors_allowlist() -> None:
    reject(cors_origins=())


def test_production_rejects_a_wildcard_cors_origin() -> None:
    """A wildcard origin with credentials is refused by the browser, and by us."""
    reject(cors_origins=("*",))


def test_production_refuses_the_test_layer2_sink() -> None:
    """Section 13: the development receiver must not be a production configuration."""
    reject(layer2_mode="local_test_receiver")


def test_production_requires_tls_for_the_oidc_issuer() -> None:
    reject(oidc_issuer="http://identity.example.com")


def test_production_requires_an_https_database_host() -> None:
    """A production database must not be reached over a plaintext transport."""
    reject(database_require_tls=False)


def test_production_rejects_private_egress_by_default() -> None:
    settings = production()
    assert settings.egress_allow_private_networks is False


def test_enabling_private_egress_is_an_explicit_decision() -> None:
    """Section 4.4: private destinations require an explicit administrative choice."""
    settings = production(egress_allow_private_networks=True)
    assert settings.egress_allow_private_networks is True


def test_production_rejects_plaintext_http_egress() -> None:
    settings = production()
    assert settings.egress_allow_insecure_http is False


def test_docs_are_disabled_in_production() -> None:
    settings = production()
    assert settings.docs_enabled is False


def test_debug_must_not_be_enabled_in_production() -> None:
    reject(debug=True)


def test_session_lifetime_is_bounded() -> None:
    settings = Settings(app_env="test")
    assert 0 < settings.session_ttl_seconds <= 24 * 60 * 60
    assert 0 < settings.session_idle_timeout_seconds <= settings.session_ttl_seconds


def test_rate_limiting_is_enabled_by_default() -> None:
    assert Settings(app_env="test").rate_limit_enabled is True


def test_sync_batch_size_is_bounded() -> None:
    """T9: an unbounded batch would let one job exhaust memory."""
    settings = Settings(app_env="test")
    assert 0 < settings.sync_batch_size <= 10_000


def test_development_mode_is_the_default_environment() -> None:
    """The default must be the safe development mode, not production."""
    assert Settings().app_env in {"development", "dev", "local"}


def test_master_key_is_never_echoed_in_the_settings_representation() -> None:
    """T13: a settings dump must not disclose the master key."""
    settings = production()
    assert A_KEY not in repr(settings)
    assert A_KEY not in str(settings.model_dump())


def test_database_password_is_never_echoed_in_the_settings_representation() -> None:
    settings = production(database_url="postgresql+asyncpg://app:sekrit@db.internal:5432/app")
    rendered = f"{settings!r}{settings.model_dump()}"
    assert "sekrit" not in rendered


def test_missing_production_secret_is_reported_not_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("HUMISENSE_LAYER2_TOKEN", raising=False)
    settings = production(layer2_mode="http", layer2_base_url="https://layer2.example.com")
    with pytest.raises(ConfigurationError):
        settings.require_production_ready()


def test_require_production_ready_accepts_a_valid_configuration() -> None:
    production().require_production_ready()


def test_require_production_ready_refuses_a_development_configuration() -> None:
    """The gate must not pass merely because the environment name is not production."""
    with pytest.raises(ConfigurationError):
        Settings(app_env="test", auth_mode="dev").require_production_ready()


def test_env_example_contains_no_real_credentials() -> None:
    """Section 4.3: the committed example carries placeholder names only."""
    from pathlib import Path

    candidates = [
        Path(__file__).resolve().parents[1] / ".env.example",
        Path(__file__).resolve().parents[2] / ".env.example",
    ]
    example = next((path for path in candidates if path.exists()), None)
    assert example is not None, "a .env.example file is required"

    text = example.read_text()
    assert text.strip()

    # Variables that must be replaced before use.
    secret_variables = {
        "SESSION_SECRET",
        "SECRETS_MASTER_KEY",
        "LAYER2_BEARER_TOKEN",
        "DATABASE_URL",
    }
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        value = value.strip().strip("'\"")
        if not value or name.strip() not in secret_variables:
            continue
        lowered = value.lower()
        assert any(
            marker in lowered
            for marker in ("change", "replace", "your", "example", "placeholder", "<", "xxx")
        ), f"{name} carries what looks like a real value: {stripped!r}"


def test_committed_files_do_not_contain_a_master_key() -> None:
    """T13/T16: no secret material may be committed to the repository."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    suspicious: list[str] = []
    checked = 0
    for path in root.rglob("*"):
        if not path.is_file() or ".git" in path.parts or ".venv" in path.parts:
            continue
        # Test fixtures deliberately contain fixed, non-secret test keys. The scan is
        # about production source, configuration, and deployment files.
        if "tests" in path.parts:
            continue
        if path.suffix not in {
            ".py",
            ".toml",
            ".cfg",
            ".ini",
            ".yml",
            ".yaml",
            ".env",
            ".example",
            ".md",
        }:
            continue
        checked += 1
        try:
            text = path.read_text(errors="ignore")
        except OSError:
            continue
        if "secrets_master_key" in text or "SECRETS_MASTER_KEY" in text:
            for line in text.splitlines():
                if "MASTER_KEY" not in line.upper():
                    continue
                if "=" not in line:
                    continue
                value = line.partition("=")[2].strip().strip("'\"")
                # A 32-byte base64 key is 44 characters and is real material.
                if len(value) >= 44 and " " not in value and "your" not in value.lower():
                    suspicious.append(f"{path.relative_to(root)}: {line.strip()[:80]}")

    assert checked > 0
    assert suspicious == [], f"possible committed key material: {suspicious}"
