"""Application error taxonomy.

Every error that can reach a client carries a stable machine-readable ``code`` and a
``public_message`` that is safe to display. Internal detail is attached as ``detail``
and is only ever emitted to structured logs, never to a response body.
"""

from __future__ import annotations

from typing import Any


class HumisenseError(Exception):
    """Base class for all application errors."""

    code: str = "internal_error"
    http_status: int = 500
    public_message: str = "An internal error occurred."

    def __init__(
        self,
        public_message: str | None = None,
        *,
        detail: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        self.public_message = public_message or type(self).public_message
        self.detail = detail
        self.context = context or {}
        super().__init__(self.detail or self.public_message)


class ConfigurationError(HumisenseError):
    code = "configuration_error"
    http_status = 500
    public_message = "The service is misconfigured."


class AuthenticationError(HumisenseError):
    code = "unauthenticated"
    http_status = 401
    public_message = "Authentication is required."


class AuthorizationError(HumisenseError):
    code = "forbidden"
    http_status = 403
    public_message = "You are not authorized to perform this action."


class CsrfError(HumisenseError):
    code = "csrf_failed"
    http_status = 403
    public_message = "The request could not be verified."


class NotFoundError(HumisenseError):
    code = "not_found"
    http_status = 404
    public_message = "The requested resource was not found."


class ConflictError(HumisenseError):
    code = "conflict"
    http_status = 409
    public_message = "The request conflicts with the current state."


class ValidationFailure(HumisenseError):
    code = "validation_failed"
    http_status = 422
    public_message = "The request was rejected by validation."


class RateLimitedError(HumisenseError):
    code = "rate_limited"
    http_status = 429
    public_message = "Too many requests. Try again later."


class PayloadTooLargeError(HumisenseError):
    code = "payload_too_large"
    http_status = 413
    public_message = "The request payload is too large."


class EgressBlockedError(HumisenseError):
    """Raised by the egress policy when a destination is not permitted."""

    code = "egress_blocked"
    http_status = 400
    public_message = "The destination is not permitted by the outbound network policy."


class ConnectorError(HumisenseError):
    """Base for connector failures. ``code`` is machine-readable."""

    code = "connector_error"
    http_status = 502
    public_message = "The connector could not complete the operation."


class ConnectorConfigError(ConnectorError):
    code = "connector_config_invalid"
    http_status = 422
    public_message = "The connector configuration is invalid."


class ConnectorAuthError(ConnectorError):
    """Permanent: retrying will not help."""

    code = "connector_auth_failed"
    http_status = 502
    public_message = "The connector could not authenticate to the source system."


class ConnectorUnavailableError(ConnectorError):
    code = "connector_unavailable"
    http_status = 503
    public_message = "The connector is not available."


class ConnectorNotConfiguredError(ConnectorError):
    """The connector type exists but has no verified integration."""

    code = "connector_not_configured"
    http_status = 501
    public_message = "This connector is not configured for this environment."


class ConnectorSourceError(ConnectorError):
    """Transient: the source system failed. Retryable."""

    code = "connector_source_error"
    http_status = 502
    public_message = "The source system returned an error."


class ConnectorDataError(ConnectorError):
    """The source returned data that violates the contract."""

    code = "connector_data_invalid"
    http_status = 502
    public_message = "The source returned data that failed validation."


class ConnectorResourceLimitError(ConnectorError):
    code = "connector_resource_limit"
    http_status = 413
    public_message = "The connector exceeded a configured resource limit."


class SyncCancelledError(HumisenseError):
    code = "sync_cancelled"
    http_status = 409
    public_message = "The synchronization job was cancelled."


class SecretNotFoundError(HumisenseError):
    code = "secret_not_found"
    http_status = 404
    public_message = "The referenced credential does not exist."


class SecretDecryptionError(HumisenseError):
    code = "secret_undecryptable"
    http_status = 500
    public_message = "The stored credential could not be decrypted."


class PathNotAllowedError(HumisenseError):
    code = "path_not_allowed"
    http_status = 400
    public_message = "The requested path is outside the permitted locations."


class IngestionError(HumisenseError):
    code = "ingestion_failed"
    http_status = 502
    public_message = "Records could not be delivered to the downstream layer."


TRANSIENT_ERROR_CODES = frozenset(
    {
        ConnectorSourceError.code,
        ConnectorUnavailableError.code,
        IngestionError.code,
        RateLimitedError.code,
    }
)
"""Codes for which retrying the same operation may succeed."""

PERMANENT_ERROR_CODES = frozenset(
    {
        ConnectorConfigError.code,
        ConnectorAuthError.code,
        ConnectorDataError.code,
        ConnectorResourceLimitError.code,
        PathNotAllowedError.code,
        EgressBlockedError.code,
        SecretNotFoundError.code,
    }
)
"""Codes for which retrying the same operation will not succeed."""


def is_retryable(code: str) -> bool:
    """Classify an error code as transient (retry) or permanent (do not retry)."""
    # Unknown codes default to non-retryable so a bug cannot become a retry storm.
    if code in PERMANENT_ERROR_CODES:
        return False
    return code in TRANSIENT_ERROR_CODES
