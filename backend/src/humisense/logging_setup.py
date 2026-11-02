"""Structured logging with credential redaction.

The redaction filter is a security control: log statements must be safe by default
rather than relying on every call site to remember not to log a secret.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog

REDACTED = "[REDACTED]"

SENSITIVE_KEY_PATTERN = re.compile(
    r"(pass(word|phrase)?|secret|token|api[_-]?key|apikey|authorization|auth|credential"
    r"|private[_-]?key|client[_-]?secret|refresh[_-]?token|access[_-]?token|session[_-]?id"
    r"|cookie|bearer|signature|dsn|connection[_-]?string|master[_-]?key)",
    re.IGNORECASE,
)

VALUE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE), f"Bearer {REDACTED}"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}"), REDACTED),
    (re.compile(r"\b(?:sk|pk|ghp|gho|glpat|xox[baprs])[-_][A-Za-z0-9_-]{12,}"), REDACTED),
    (re.compile(r"(?i)(password|passwd|pwd)\s*[=:]\s*[^\s,;'\"]+"), rf"\1={REDACTED}"),
    (re.compile(r"://[^:/@\s]+:[^@/\s]+@"), f"://{REDACTED}@{REDACTED}@"),
    (
        re.compile(
            r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"
            r"(?=[^\n]*\bkey\b)",
            re.IGNORECASE,
        ),
        REDACTED,
    ),
)


def redact_value(value: Any, *, key: str | None = None, depth: int = 0) -> Any:
    """Return a redacted copy of ``value``.

    Keys matching :data:`SENSITIVE_KEY_PATTERN` have their entire value replaced.
    Strings elsewhere are scrubbed of credential-shaped substrings.
    """
    if depth > 6:
        return "[TRUNCATED]"
    if key is not None and SENSITIVE_KEY_PATTERN.search(key):
        return REDACTED
    if isinstance(value, dict):
        return {str(k): redact_value(v, key=str(k), depth=depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_value(item, depth=depth + 1) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def redact_text(text: str) -> str:
    """Scrub credential-shaped substrings from free text."""
    result = text
    for pattern, replacement in VALUE_PATTERNS:
        result = pattern.sub(replacement, result)
    return result


def redact_mapping(mapping: MutableMapping[str, Any]) -> None:
    """Redact a mapping in place. Used by the structlog processor chain."""
    for key in list(mapping.keys()):
        mapping[key] = redact_value(mapping[key], key=key)


class RedactionFilter(logging.Filter):
    """Scrub credential-shaped substrings from stdlib log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_text(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: redact_value(v, key=k) for k, v in record.args.items()}
            elif isinstance(record.args, tuple):
                record.args = tuple(redact_value(a) for a in record.args)
        return True


def _redaction_processor(
    logger: Any, method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    redact_mapping(event_dict)
    return event_dict


def configure_logging(*, json_logs: bool = True, level: str = "INFO") -> None:
    """Configure structlog and stdlib logging with redaction applied."""
    numeric_level = logging.getLevelNamesMapping().get(level.upper(), logging.INFO)

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        _redaction_processor,
    ]

    renderer: Any = (
        structlog.processors.JSONRenderer()
        if json_logs
        else structlog.dev.ConsoleRenderer(colors=False)
    )

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(RedactionFilter())
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(numeric_level)

    for noisy in ("uvicorn.access", "uvicorn.error"):
        logging.getLogger(noisy).setLevel(max(numeric_level, logging.INFO))
    # SQL is never emitted at INFO: a statement with bound parameters would put record
    # content and identifiers into the log stream.
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine.Engine").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.pool").setLevel(logging.WARNING)


def get_logger(name: str | None = None) -> Any:
    """Return a structlog bound logger."""
    return structlog.get_logger(name)
