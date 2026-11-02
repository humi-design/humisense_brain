"""Typed application configuration.

Production startup validation is a security control, not a convenience: an insecure
configuration must fail closed rather than degrade silently.
"""

from __future__ import annotations

import ipaddress
import os
import ssl as ssl_module
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from humisense.errors import ConfigurationError

DEFAULT_DEV_SESSION_SECRET = "dev-only-insecure-session-secret-change-me"
MIN_SECRET_LENGTH = 32

# The repository root, three levels above this file (backend/src/humisense/config.py).
# Relative defaults are anchored here rather than to the process working directory, so a
# documented command works no matter which directory it is run from. A source checkout is
# the only layout that has committed development data; an installed package must set an
# absolute CSV_ALLOWED_ROOTS, which production validation already requires to be explicit.
_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    """Runtime configuration, loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        # `.env.example` lists optional settings with empty values so the file documents
        # every knob. Without this, an empty value is passed to the field's validator
        # instead of falling back to the default, and a boolean field such as
        # DOCS_ENABLED fails to parse. Treating empty as unset keeps the documented
        # copy-and-run setup working, and keeps optional settings optional.
        env_ignore_empty=True,
    )

    # --- Core -------------------------------------------------------------
    app_name: str = "Humisense Brain / OS"
    app_env: Literal["development", "test", "production"] = "development"
    api_prefix: str = "/api/v1"
    log_level: str = "INFO"
    log_json: bool = True
    """Structured JSON logs. Set false only for local readability."""
    debug: bool = False
    """Never enable in production: it can expose internals through error paths."""

    # --- Database ---------------------------------------------------------
    database_url: SecretStr = SecretStr(
        "postgresql+asyncpg://humisense:humisense_dev_pw@localhost:5432/humisense"
    )
    """A SecretStr so the embedded password cannot leak through repr or a log line."""
    database_pool_size: int = 10
    database_max_overflow: int = 5
    database_statement_timeout_ms: int = 30_000
    database_require_tls: bool = False
    """Required in production: refuses to run against a database reached in plaintext."""
    database_tls_ca_path: str | None = None
    """CA bundle used to verify the database's TLS certificate when TLS is required."""

    # --- Redis ------------------------------------------------------------
    redis_url: str = "redis://localhost:6379/0"

    # --- Authentication ---------------------------------------------------
    auth_mode: Literal["dev", "oidc"] = "dev"
    """``dev`` issues local sessions for development. ``oidc`` verifies real tokens.

    ``dev`` is refused when ``app_env`` is ``production``.
    """

    oidc_issuer: str | None = None
    oidc_audience: str | None = None
    oidc_jwks_url: str | None = None
    oidc_algorithms: Annotated[tuple[str, ...], NoDecode] = ("RS256", "ES256")
    oidc_leeway_seconds: int = 60

    session_secret: SecretStr = SecretStr(DEFAULT_DEV_SESSION_SECRET)
    session_ttl_seconds: int = 8 * 60 * 60
    session_idle_timeout_seconds: int = 30 * 60
    session_cookie_name: str = "humisense_session"
    csrf_cookie_name: str = "humisense_csrf"
    csrf_header_name: str = "X-CSRF-Token"
    cookie_secure: bool = False
    """Must be true in production. Derived from ``app_env`` unless set explicitly."""

    # --- CORS -------------------------------------------------------------
    cors_origins: Annotated[tuple[str, ...], NoDecode] = ("http://localhost:5173",)
    trusted_proxy_count: int = 0
    """Number of trusted reverse proxies in front of the app. Governs client IP trust."""

    # --- Secrets provider -------------------------------------------------
    secrets_provider: Literal["env", "encrypted_db"] = "encrypted_db"
    secrets_master_key: SecretStr | None = None
    """Base64, 32 bytes, for the encrypted DB store. Required by that provider."""

    secrets_env_prefix: str = "HUMISENSE_SECRET_"
    secrets_allow_env_provider: bool = False
    """Set true only for a deployment that deliberately keeps credentials in the process
    environment. Production validation refuses the env provider unless this is set."""

    # --- Egress / SSRF policy --------------------------------------------
    egress_allowed_hosts: Annotated[tuple[str, ...], NoDecode] = ()
    """Explicit allowlist of hostnames or CIDRs connectors may reach."""

    egress_allow_private_networks: bool = False
    """Administrative override for documented internal integrations only."""

    egress_allow_insecure_http: bool = False
    egress_connect_timeout_seconds: float = 5.0
    egress_read_timeout_seconds: float = 20.0
    egress_max_response_bytes: int = 8 * 1024 * 1024
    egress_max_redirects: int = 2

    # --- Connector limits -------------------------------------------------
    csv_allowed_roots: Annotated[tuple[str, ...], NoDecode] = ("data/synthetic",)
    csv_max_file_bytes: int = 64 * 1024 * 1024
    csv_max_rows: int = 1_000_000
    csv_max_processing_seconds: float = 120.0
    upload_dir: str = "./var/uploads"
    upload_max_bytes: int = 32 * 1024 * 1024

    # --- Layer 2 handoff --------------------------------------------------
    layer2_mode: Literal["disabled", "local_test_receiver", "http"] = "disabled"
    layer2_base_url: str | None = None
    layer2_secret_ref: str | None = None
    layer2_bearer_token: SecretStr | None = None
    """Bootstrap only. Prefer ``layer2_secret_ref`` so the token lives in the secrets
    provider rather than the process environment."""
    layer2_timeout_seconds: float = 10.0
    layer2_max_attempts: int = 4

    # --- Layer 2 receiver -------------------------------------------------
    # The token Layer 1 presents when delivering to this deployment's own receiver. Only
    # needed when this process both produces and receives (a single-node deployment).
    layer2_ingest_token: SecretStr | None = None
    layer2_ingest_enabled: bool = True
    """Whether the ingestion receiver route is registered. Disabled means the route does
    not exist at all, rather than existing and rejecting."""
    layer2_max_batch_bytes: int = 1 * 1024 * 1024
    """Upper bound on a batch's serialized records. Kept at or below
    ``max_request_body_bytes`` so an HTTP caller is stopped by the same limit the receiver
    enforces, rather than by a smaller transport limit that makes this one dead."""
    layer2_publish_mode: Literal["disabled", "log", "http"] = "disabled"
    layer2_publish_base_url: str | None = None
    layer2_publish_secret_ref: str | None = None
    layer2_publish_bearer_token: SecretStr | None = None

    # --- Resource protection ---------------------------------------------
    rate_limit_enabled: bool = True
    task_queue_mode: Literal["arq", "inline"] = "arq"
    """``arq`` runs jobs in a separate worker process. ``inline`` runs them in the API
    process and is refused in production."""
    rate_limit_sync_per_hour: int = 60
    rate_limit_writes_per_minute: int = 120
    rate_limit_tests_per_minute: int = 30
    max_request_body_bytes: int = 1 * 1024 * 1024
    max_page_size: int = 200
    default_page_size: int = 50
    sync_max_concurrent_jobs_per_tenant: int = 3
    sync_batch_size: int = 500

    # --- Observability ----------------------------------------------------
    metrics_enabled: bool = True
    docs_enabled: bool | None = None
    """Defaults to true in development and test, false in production."""

    otel_enabled: bool = False
    otel_endpoint: str | None = None

    @field_validator("cors_origins", "egress_allowed_hosts", "csv_allowed_roots", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return ()
            if stripped.startswith("["):
                return value  # let pydantic parse JSON form
            return tuple(part.strip() for part in stripped.split(",") if part.strip())
        return value

    @field_validator("csv_allowed_roots", mode="after")
    @classmethod
    def _anchor_relative_roots(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Anchor relative roots to the repository root, not the working directory.

        The committed default (``data/synthetic``) and the documented quick start both
        run commands from ``backend/``, so a working-directory-relative default resolved
        to ``backend/data/synthetic`` and the CSV connector failed closed. Anchoring to
        the repository root makes the documented commands work from any directory.
        """
        anchored: list[str] = []
        for root in value:
            candidate = Path(root).expanduser()
            if not candidate.is_absolute():
                candidate = _REPOSITORY_ROOT / candidate
            anchored.append(str(candidate))
        return tuple(anchored)

    @field_validator("oidc_algorithms", mode="before")
    @classmethod
    def _split_algorithms(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(part.strip() for part in value.split(",") if part.strip())
        return value

    @field_validator("session_secret")
    @classmethod
    def _session_secret_strength(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if raw != DEFAULT_DEV_SESSION_SECRET and len(raw) < MIN_SECRET_LENGTH:
            raise ValueError(
                f"session_secret must be at least {MIN_SECRET_LENGTH} characters "
                "when it is not the development default"
            )
        return value

    @property
    def database_dsn(self) -> str:
        """The database URL, unwrapped. Call this at the point of use, never log it."""
        return self.database_url.get_secret_value()

    @property
    def database_connect_args(self) -> dict[str, object]:
        """asyncpg connection arguments derived from the TLS configuration.

        ``database_require_tls`` is a startup requirement, so it must also be *enforced*:
        without this the flag would only gate startup while the connection stayed in
        plaintext. When TLS is required, an ``ssl.SSLContext`` is built that verifies the
        server certificate and its hostname. The certificate is verified against
        ``database_tls_ca_path`` when one is given, and against the system trust store
        otherwise.

        Called at the point of engine construction, never logged.
        """
        if not self.database_require_tls:
            return {}
        context = ssl_module.create_default_context()
        # create_default_context already sets check_hostname and CERT_REQUIRED; stating
        # them keeps the guarantee visible if the helper's defaults ever change.
        context.check_hostname = True
        context.verify_mode = ssl_module.CERT_REQUIRED
        if self.database_tls_ca_path:
            context.load_verify_locations(cafile=self.database_tls_ca_path)
        return {"ssl": context}

    @property
    def session_secret_value(self) -> str:
        return self.session_secret.get_secret_value()

    @property
    def secrets_master_key_value(self) -> str | None:
        if self.secrets_master_key is None:
            return None
        return self.secrets_master_key.get_secret_value()

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def docs_public(self) -> bool:
        return (not self.is_production) if self.docs_enabled is None else self.docs_enabled

    @property
    def effective_cookie_secure(self) -> bool:
        return self.cookie_secure or self.is_production

    @property
    def is_development_auth(self) -> bool:
        return self.auth_mode == "dev"

    def layer2_bearer_token_resolved(self) -> str | None:
        """Return the Layer 2 bearer token from configuration or the environment."""
        if self.layer2_bearer_token is not None:
            return self.layer2_bearer_token.get_secret_value()
        return os.environ.get("HUMISENSE_LAYER2_TOKEN") or None

    def layer2_ingest_token_resolved(self) -> str | None:
        """Return the token this deployment accepts on its ingestion receiver.

        Prefers explicit configuration, then the environment. When neither is set the
        receiver cannot authenticate a caller, so the route refuses every request rather
        than accepting unauthenticated writes.
        """
        if self.layer2_ingest_token is not None:
            return self.layer2_ingest_token.get_secret_value()
        return os.environ.get("HUMISENSE_LAYER2_INGEST_TOKEN") or None

    def layer2_publish_token_resolved(self) -> str | None:
        if self.layer2_publish_bearer_token is not None:
            return self.layer2_publish_bearer_token.get_secret_value()
        return os.environ.get("HUMISENSE_LAYER2_PUBLISH_TOKEN") or None

    def validate_for_production(self) -> None:
        """Refuse to start with an insecure production configuration.

        Raises:
            ConfigurationError: if any production safety requirement is unmet.
        """
        if not self.is_production:
            return

        problems: list[str] = []
        if self.auth_mode == "dev":
            problems.append(
                "auth_mode='dev' is not permitted when app_env='production'; "
                "configure an OpenID Connect provider"
            )
        if self.session_secret_value == DEFAULT_DEV_SESSION_SECRET:
            problems.append("session_secret must be replaced with a strong random value")
        if "*" in self.cors_origins:
            problems.append("cors_origins must not contain the wildcard '*'")
        if not self.cors_origins:
            problems.append("cors_origins must list the explicit frontend origins")
        if any(origin.startswith("http://") for origin in self.cors_origins):
            problems.append("cors_origins must use https in production")
        if self.auth_mode == "oidc":
            if not self.oidc_issuer:
                problems.append("oidc_issuer is required when auth_mode='oidc'")
            if not self.oidc_audience:
                problems.append("oidc_audience is required when auth_mode='oidc'")
            if self.oidc_issuer and not self.oidc_issuer.startswith("https://"):
                problems.append("oidc_issuer must use https in production")
        if self.secrets_provider == "encrypted_db" and not self.secrets_master_key_value:
            problems.append("secrets_master_key is required when secrets_provider='encrypted_db'")
        if self.secrets_provider == "env" and not self.secrets_allow_env_provider:
            problems.append(
                "secrets_provider='env' is refused in production; use 'encrypted_db' or a "
                "vault-backed provider, or set secrets_allow_env_provider to accept the risk"
            )
        if self.debug:
            problems.append("debug must be false in production")
        if not self.database_require_tls:
            problems.append("database_require_tls must be true in production")
        if not self.effective_cookie_secure:
            problems.append("cookie_secure must be true in production")
        if self.egress_allow_private_networks and not self.egress_allowed_hosts:
            problems.append(
                "egress_allow_private_networks requires an explicit egress_allowed_hosts list"
            )
        if self.layer2_mode == "local_test_receiver":
            problems.append(
                "layer2_mode='local_test_receiver' is a test-only sink and is refused in production"
            )
        if problems:
            raise ConfigurationError(
                "Refusing to start with an insecure production configuration.",
                detail="; ".join(problems),
                context={"problem_count": len(problems)},
            )

    def require_production_ready(self) -> None:
        """Assert that this configuration is fit for a production deployment.

        This is the gate a deployment step or a release check calls. It is deliberately
        stricter than :meth:`validate_for_production`, which only refuses a configuration
        that is unsafe *given* that the environment claims to be production. This method
        also refuses a configuration that is merely not production-grade, so a release
        cannot ship a development configuration by relabelling the environment.

        Raises:
            ConfigurationError: listing every unmet requirement.
        """
        problems: list[str] = []
        if not self.is_production:
            problems.append("app_env must be 'production' for a production deployment")
        if self.is_development_auth:
            problems.append("auth_mode='dev' must not be used for a production deployment")
        if self.auth_mode != "oidc":
            problems.append("auth_mode='oidc' is required for a production deployment")
        if not self.oidc_issuer or not self.oidc_issuer.startswith("https://"):
            problems.append("oidc_issuer must be an https URL")
        if not self.oidc_audience:
            problems.append("oidc_audience is required")
        if self.session_secret_value == DEFAULT_DEV_SESSION_SECRET:
            problems.append("session_secret must be replaced with a strong random value")
        if self.secrets_provider == "encrypted_db" and not self.secrets_master_key:
            problems.append("secrets_master_key is required")
        if self.layer2_mode == "local_test_receiver":
            problems.append("layer2_mode='local_test_receiver' is a test-only sink")
        if self.layer2_mode == "http" and not self.layer2_bearer_token_resolved():
            problems.append("a Layer 2 bearer token is required for http delivery")
        if self.layer2_ingest_enabled and not self.layer2_ingest_token_resolved():
            # Without a token the receiver refuses every request. That is safe, but it means
            # the deployment silently accepts nothing, so it is a configuration error here.
            problems.append(
                "layer2_ingest_token is required while the ingestion receiver is enabled"
            )
        if self.layer2_publish_mode == "http":
            if not self.layer2_publish_base_url:
                problems.append("layer2_publish_base_url is required for http publication")
            if not self.layer2_publish_token_resolved():
                problems.append("a Layer 2 publish token is required for http publication")
        if self.task_queue_mode != "arq":
            problems.append("task_queue_mode must be 'arq' so sync work runs outside the API")
        if problems:
            raise ConfigurationError(
                "This configuration is not production-ready.",
                detail="; ".join(problems),
                context={"problem_count": len(problems)},
            )
        # Finally apply the in-production rules, so the two gates cannot disagree.
        self.validate_for_production()
        self.assert_runtime_safe()

    def assert_runtime_safe(self) -> None:
        """Validate invariants that must hold in every environment."""
        if self.max_page_size < 1 or self.max_page_size > 1000:
            raise ConfigurationError(detail="max_page_size must be between 1 and 1000")
        if self.default_page_size > self.max_page_size:
            raise ConfigurationError(detail="default_page_size must not exceed max_page_size")
        if self.sync_batch_size < 1:
            raise ConfigurationError(detail="sync_batch_size must be positive")
        if self.layer2_max_batch_bytes < 1:
            raise ConfigurationError(detail="layer2_max_batch_bytes must be positive")
        if self.layer2_max_batch_bytes > self.max_request_body_bytes:
            # The receiver's own bound would then never be reached, because the transport
            # limit rejects the request first: the configured value would be dead.
            raise ConfigurationError(
                detail="layer2_max_batch_bytes must not exceed max_request_body_bytes"
            )
        if self.egress_allow_private_networks and self.is_production:
            for entry in self.egress_allowed_hosts:
                self._assert_not_broad_private_cidr(entry)

    @staticmethod
    def _assert_not_broad_private_cidr(entry: str) -> None:
        try:
            network = ipaddress.ip_network(entry, strict=False)
        except ValueError:
            return
        if network.prefixlen < 24 and network.is_private:
            raise ConfigurationError(
                detail=f"egress_allowed_hosts entry {entry!r} is too broad for a private range"
            )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()


def reset_settings_cache() -> None:
    """Clear the settings cache. Used by tests that mutate the environment."""
    get_settings.cache_clear()
    os.environ.pop("_HUMISENSE_SETTINGS_LOADED", None)


AnnotatedSettings = Annotated[Settings, Field()]
