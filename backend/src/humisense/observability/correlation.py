"""Correlation identifiers and OpenTelemetry-compatible tracing hooks.

A correlation id is attached to every request, propagated to jobs, and recorded on every
audit event and log line. It is generated server-side and validated when it arrives from
a client so a caller cannot inject an unbounded or control-character-bearing value into
logs.
"""

from __future__ import annotations

import re
import uuid
from contextvars import ContextVar

CORRELATION_HEADER = "X-Correlation-Id"
_VALID_CORRELATION = re.compile(r"^[A-Za-z0-9._:-]{8,64}$")

_current: ContextVar[str | None] = ContextVar("humisense_correlation_id", default=None)


def new_correlation_id() -> str:
    return uuid.uuid4().hex


def sanitize_correlation_id(value: str | None) -> str:
    """Return a safe correlation id, generating one when the supplied value is unusable."""
    if value and _VALID_CORRELATION.match(value):
        return value
    return new_correlation_id()


def set_correlation_id(value: str) -> str:
    safe = sanitize_correlation_id(value)
    _current.set(safe)
    return safe


def get_correlation_id() -> str:
    value = _current.get()
    if value is None:
        value = new_correlation_id()
        _current.set(value)
    return value


def current_trace_id() -> str | None:
    """Return the active OpenTelemetry trace id, when tracing is enabled.

    Returns None rather than importing the OpenTelemetry SDK unconditionally, so the
    dependency stays optional.
    """
    try:
        from opentelemetry import trace
    except ImportError:
        return None
    # opentelemetry exposes this through a module-level proxy, so mypy cannot see the
    # attribute even when the package is installed. Resolve it at runtime instead.
    get_current_span = getattr(trace, "get_current_span", None)
    if get_current_span is None:
        return None
    span = get_current_span()
    context = span.get_span_context() if span else None
    if context is None or not context.is_valid:
        return None
    return format(context.trace_id, "032x")
