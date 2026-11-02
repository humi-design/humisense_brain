"""Cryptographic helpers for sessions, CSRF tokens, and content hashing.

No home-made cryptography: all primitives come from the standard library's ``hashlib``,
``hmac``, and ``secrets`` modules.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from typing import Any

TOKEN_BYTES = 32
"""256 bits of entropy for session identifiers and CSRF tokens."""


def new_token() -> str:
    """Return a new URL-safe random token with 256 bits of entropy."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def sha256_hex(value: str | bytes) -> str:
    """Return the hex SHA-256 digest of ``value``."""
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def constant_time_equals(left: str, right: str) -> bool:
    """Compare two strings without leaking length or content through timing."""
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def canonical_json(value: Any) -> bytes:
    """Serialize ``value`` deterministically for hashing.

    Sorting keys and using a compact separator makes the digest stable across processes
    and Python versions, which is required for the record idempotency key.
    """
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    ).encode("utf-8")


def content_hash(payload: dict[str, Any]) -> str:
    """Return the content digest used in the source-record idempotency key."""
    return sha256_hex(canonical_json(payload))


def session_token_hash(token: str) -> str:
    """Hash a session token for storage.

    The raw token is never persisted; only this digest is, so a database read does not
    yield a usable session.
    """
    return sha256_hex(f"session:{token}")


def csrf_token_hash(token: str) -> str:
    return sha256_hex(f"csrf:{token}")
