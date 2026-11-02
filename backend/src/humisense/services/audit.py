"""Audit event writer.

Audit records are append-only. The database enforces this with a trigger that rejects
UPDATE and DELETE, so the guarantee does not depend on application discipline.

What is recorded: actor, tenant, action, target, outcome, correlation id, and
non-sensitive metadata. What is never recorded: credentials, tokens, full source
payloads, or anything from the redaction filter's denylist.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

import structlog
from sqlalchemy import select

from humisense.db.models import ActorType, AuditEvent
from humisense.db.session import Database
from humisense.logging_setup import redact_value

logger = structlog.get_logger(__name__)

MAX_METADATA_KEYS = 20
MAX_METADATA_VALUE_LENGTH = 200


class AuditAction:
    """Canonical action names. Kept as constants so the trail is queryable."""

    CONNECTION_CREATED = "connection.created"
    CONNECTION_UPDATED = "connection.updated"
    CONNECTION_DELETED = "connection.deleted"
    CONNECTION_ENABLED = "connection.enabled"
    CONNECTION_DISABLED = "connection.disabled"
    CONNECTION_TESTED = "connection.tested"
    CONNECTION_CREDENTIALS_SET = "connection.credentials_set"
    CONNECTION_CREDENTIALS_REVOKED = "connection.credentials_revoked"
    SYNC_REQUESTED = "sync.requested"
    SYNC_STARTED = "sync.started"
    SYNC_SUCCEEDED = "sync.succeeded"
    SYNC_FAILED = "sync.failed"
    SYNC_PARTIAL = "sync.partial"
    SYNC_CANCELLED = "sync.cancelled"
    SYNC_DENIED = "sync.denied"
    RECORDS_VIEWED = "records.viewed"
    AUDIT_VIEWED = "audit.viewed"
    AUTHORIZATION_DENIED = "authorization.denied"
    AUTHENTICATION_FAILED = "authentication.failed"
    SESSION_CREATED = "session.created"
    SESSION_REVOKED = "session.revoked"
    INGESTION_DELIVERED = "ingestion.delivered"
    INGESTION_FAILED = "ingestion.failed"
    INGESTION_BATCH_RECEIVED = "ingestion.batch_received"
    INGESTION_BATCH_REJECTED = "ingestion.batch_rejected"
    QUARANTINE_REPLAYED = "quarantine.replayed"
    QUARANTINE_REPLAY_FAILED = "quarantine.replay_failed"
    ANALYTICS_RUN_REQUESTED = "analytics.run_requested"
    ANALYTICS_RUN_SUCCEEDED = "analytics.run_succeeded"
    ANALYTICS_RUN_FAILED = "analytics.run_failed"
    ANALYTICS_RULE_UPDATED = "analytics.rule_updated"


OUTCOME_SUCCESS = "success"
OUTCOME_FAILURE = "failure"
OUTCOME_DENIED = "denied"


@dataclass(slots=True)
class AuditActor:
    """The actor responsible for an audited action."""

    organization_id: uuid.UUID
    actor_id: uuid.UUID | None = None
    actor_type: ActorType = ActorType.USER
    correlation_id: str | None = None

    @classmethod
    def user(cls, principal: Any, *, correlation_id: str | None = None) -> AuditActor:
        return cls(
            organization_id=principal.organization_id,
            actor_id=principal.user_id,
            actor_type=ActorType.USER,
            correlation_id=correlation_id,
        )

    @classmethod
    def worker(cls, organization_id: uuid.UUID, *, correlation_id: str | None = None) -> AuditActor:
        return cls(
            organization_id=organization_id,
            actor_id=None,
            actor_type=ActorType.WORKER,
            correlation_id=correlation_id,
        )


@dataclass(slots=True)
class AuditEntry:
    """One audit record to persist."""

    action: str
    outcome: str
    target_type: str | None = None
    target_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


def _sanitize_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """Bound and redact audit metadata.

    Two controls: credential-shaped keys and values are redacted, and the record is
    bounded so a caller cannot use the audit table as bulk storage.
    """
    redacted = redact_value(metadata)
    if not isinstance(redacted, dict):
        return {}
    bounded: dict[str, Any] = {}
    for key, value in list(redacted.items())[:MAX_METADATA_KEYS]:
        if isinstance(value, str):
            bounded[str(key)] = value[:MAX_METADATA_VALUE_LENGTH]
        elif isinstance(value, (int, float, bool)) or value is None:
            bounded[str(key)] = value
        elif isinstance(value, list):
            bounded[str(key)] = value[:10]
        else:
            bounded[str(key)] = "[omitted]"
    return bounded


class AuditService:
    """Writes audit events. Never raises into the caller's business path."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def record(self, actor: AuditActor, entry: AuditEntry) -> None:
        """Persist one audit event.

        A failure to write an audit record is logged at error level but does not abort
        the business operation, because aborting would let an attacker suppress work by
        filling the audit table. The failure is visible in the log and in metrics.
        """
        try:
            async with self._database.tenant_session(actor.organization_id) as session:
                session.add(
                    AuditEvent(
                        organization_id=actor.organization_id,
                        actor_id=actor.actor_id,
                        actor_type=actor.actor_type,
                        action=entry.action,
                        target_type=entry.target_type,
                        target_id=entry.target_id,
                        outcome=entry.outcome,
                        correlation_id=actor.correlation_id,
                        event_metadata=_sanitize_metadata(entry.metadata),
                    )
                )
        except Exception as exc:  # noqa: BLE001 - audit must not break the request path
            logger.error(
                "audit_write_failed",
                action=entry.action,
                outcome=entry.outcome,
                organization_id=str(actor.organization_id),
                error_type=type(exc).__name__,
            )

    async def list_events(
        self,
        organization_id: uuid.UUID,
        *,
        limit: int,
        offset: int = 0,
        action: str | None = None,
    ) -> list[AuditEvent]:
        """Return audit events for one tenant, newest first."""
        async with self._database.tenant_session(organization_id) as session:
            statement = select(AuditEvent).where(AuditEvent.organization_id == organization_id)
            if action:
                statement = statement.where(AuditEvent.action == action)
            statement = statement.order_by(AuditEvent.created_at.desc()).limit(limit).offset(offset)
            return list((await session.scalars(statement)).all())

    async def count_events(self, organization_id: uuid.UUID) -> int:
        from sqlalchemy import func

        async with self._database.tenant_session(organization_id) as session:
            total = await session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.organization_id == organization_id)
            )
        return int(total or 0)
