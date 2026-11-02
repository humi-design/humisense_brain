"""Inbound webhook signature verification.

Layer 1 does not expose an inbound webhook route, so this module is not wired into the
application. It is implemented and tested because the threat model requires the control
to exist before any such route is added, and because a tested helper is cheaper than an
untested one written under pressure later.

When a route is added it must also implement replay protection with a nonce store; the
timestamp window alone only narrows the replay opportunity.
"""

from __future__ import annotations

import hashlib
import hmac
import time

from humisense.errors import AuthenticationError

SIGNATURE_HEADER = "X-Humisense-Signature"
TIMESTAMP_HEADER = "X-Humisense-Timestamp"
DEFAULT_TOLERANCE_SECONDS = 300


def compute_signature(secret: str, timestamp: str, body: bytes) -> str:
    """Compute the hex HMAC-SHA256 signature over ``timestamp.body``."""
    mac = hmac.new(secret.encode("utf-8"), digestmod=hashlib.sha256)
    mac.update(timestamp.encode("utf-8"))
    mac.update(b".")
    mac.update(body)
    return mac.hexdigest()


def verify_signature(
    *,
    secret: str,
    body: bytes,
    signature: str | None,
    timestamp: str | None,
    tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
    now: float | None = None,
) -> None:
    """Verify a webhook signature and timestamp.

    The comparison is constant time, so a caller cannot learn the expected signature by
    timing. The timestamp must be within ``tolerance_seconds`` of now, which bounds the
    window in which a captured request can be replayed.

    Raises:
        AuthenticationError: if the signature or timestamp is missing, stale, or wrong.
    """
    if not signature or not timestamp:
        raise AuthenticationError(detail="Webhook signature or timestamp header is missing")

    try:
        sent_at = float(timestamp)
    except (TypeError, ValueError) as exc:
        raise AuthenticationError(detail="Webhook timestamp header is not numeric") from exc

    current = time.time() if now is None else now
    if abs(current - sent_at) > tolerance_seconds:
        raise AuthenticationError(
            detail="Webhook timestamp is outside the accepted tolerance",
            context={"tolerance_seconds": tolerance_seconds},
        )

    expected = compute_signature(secret, timestamp, body)
    if not hmac.compare_digest(expected, signature.strip().lower()):
        raise AuthenticationError(detail="Webhook signature does not match")
