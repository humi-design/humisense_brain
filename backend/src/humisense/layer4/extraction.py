"""Communication-to-work interface.

Business activity frequently lives in emails, messages, and documents rather than formal task
systems. Layer 4 **consumes** extraction results from an approved upstream extractor; it does
not run an LLM and does not fake extraction.

The interface below is the contract an extractor publishes to. Everything in this module is
deterministic handling of what the extractor produced, and it encodes the rules the brief
requires:

1. A request is not an accepted commitment.
2. A proposed deadline is not a confirmed deadline.
3. A person mentioned in a message is not the owner.
4. An extracted task is not automatically assigned.
5. Conflicting dates are preserved as conflicts.
6. Ambiguous references remain unresolved.
7. External communication is untrusted data.
8. Prompt-injection text inside a message is never an executable instruction.

Because of rule 8, nothing here evaluates, executes, or interpolates extracted text. Extracted
fields are stored as data and compared structurally.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from humisense.db.models import (
    ExtractionCandidateType,
    ExtractionVerification,
    WorkOrigin,
)

# Fields an extractor may propose. Anything outside this set is ignored, so an extractor
# cannot smuggle an unexpected field into a work entity.
ALLOWED_EXTRACTION_FIELDS: frozenset[str] = frozenset(
    {
        "title",
        "description",
        "work_type",
        "domain",
        "requested_due_at",
        "promised_due_at",
        "requester",
        "mentioned_organizations",
        "mentioned_customer",
        "mentioned_supplier",
        "mentioned_product",
        "mentioned_order",
        "next_expected_action",
        "commitment_description",
        "committing_party",
        "receiving_party",
        "dependency_ref",
    }
)

MAX_EXTRACTION_FIELD_LENGTH = 2000


@dataclass(frozen=True, slots=True)
class ExtractionCandidate:
    """One candidate fact an extractor proposed from a communication."""

    extraction_key: str
    candidate_type: ExtractionCandidateType
    extraction_method: str
    extraction_version: str
    fields: dict[str, object]
    confidence: float
    canonical_record_id: uuid.UUID | None = None
    source_reference: str | None = None
    created_at: datetime | None = None
    verification_status: ExtractionVerification = ExtractionVerification.UNVERIFIED
    rejected_fields: list[str] = field(default_factory=list)


def sanitize_extraction(
    *,
    extraction_key: str,
    candidate_type: ExtractionCandidateType,
    extraction_method: str,
    extraction_version: str,
    fields: dict[str, object],
    confidence: float,
    canonical_record_id: uuid.UUID | None = None,
    source_reference: str | None = None,
    created_at: datetime | None = None,
) -> ExtractionCandidate:
    """Validate and bound an extractor's proposed fields.

    Unknown fields are dropped (and reported), string values are length-bounded, and the
    confidence is clamped. Extracted text is stored verbatim as data and never interpreted.
    """
    accepted: dict[str, object] = {}
    rejected: list[str] = []
    for name, value in fields.items():
        if name not in ALLOWED_EXTRACTION_FIELDS:
            rejected.append(name)
            continue
        accepted[name] = _bound_value(value)
    clamped = min(max(confidence, 0.0), 1.0)
    return ExtractionCandidate(
        extraction_key=extraction_key[:300],
        candidate_type=candidate_type,
        extraction_method=extraction_method[:120],
        extraction_version=extraction_version[:40],
        fields=accepted,
        confidence=clamped,
        canonical_record_id=canonical_record_id,
        source_reference=source_reference[:300] if source_reference else None,
        created_at=created_at,
        rejected_fields=rejected,
    )


def _bound_value(value: object) -> object:
    if isinstance(value, str):
        return value[:MAX_EXTRACTION_FIELD_LENGTH]
    if isinstance(value, list):
        return [_bound_value(item) for item in value[:50]]
    if isinstance(value, dict):
        return {str(k)[:120]: _bound_value(v) for k, v in list(value.items())[:50]}
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:MAX_EXTRACTION_FIELD_LENGTH]


@dataclass(frozen=True, slots=True)
class PromotionDecision:
    """Whether a candidate may be promoted to work, and as what.

    A candidate is only promoted when it is verified, or when the candidate is a structural
    fact (an explicit task record) rather than a text-derived guess. Text-derived candidates
    stay ``UNVERIFIED`` until an authorized action verifies them.
    """

    promote: bool
    origin: WorkOrigin
    confidence: float
    reason: str


def decide_promotion(
    candidate: ExtractionCandidate,
    *,
    auto_promote_threshold: float | None = None,
) -> PromotionDecision:
    """Decide whether a candidate becomes work.

    By default nothing is auto-promoted from an extractor: the decision returns
    ``promote=False`` with ``origin=EXTRACTED`` so the candidate is stored, visible, and
    reviewable but not asserted as fact. A tenant may set a threshold, and even then the
    promoted work is marked ``EXTRACTED`` with its confidence, never ``EXPLICIT``.
    """
    if candidate.verification_status == ExtractionVerification.REJECTED:
        return PromotionDecision(False, WorkOrigin.EXTRACTED, candidate.confidence, "rejected")
    if candidate.verification_status == ExtractionVerification.VERIFIED:
        return PromotionDecision(True, WorkOrigin.EXTRACTED, candidate.confidence, "verified")
    if auto_promote_threshold is not None and candidate.confidence >= auto_promote_threshold:
        return PromotionDecision(
            True,
            WorkOrigin.EXTRACTED,
            candidate.confidence,
            "above_configured_threshold",
        )
    return PromotionDecision(
        False,
        WorkOrigin.EXTRACTED,
        candidate.confidence,
        "unverified_extraction_requires_review",
    )


def is_untrusted_source(canonical_metadata: dict[str, object] | None) -> bool:
    """Return whether a source record is flagged untrusted.

    Layer 2 marks ingested source content untrusted. Layer 4 preserves that flag and never
    treats untrusted text as an instruction; this helper makes the check explicit at the
    point a communication-derived candidate is considered.
    """
    if not canonical_metadata:
        return True
    return bool(canonical_metadata.get("untrusted", True))


__all__ = [
    "ALLOWED_EXTRACTION_FIELDS",
    "MAX_EXTRACTION_FIELD_LENGTH",
    "ExtractionCandidate",
    "PromotionDecision",
    "decide_promotion",
    "is_untrusted_source",
    "sanitize_extraction",
]
