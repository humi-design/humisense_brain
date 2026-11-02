"""Business-record normalizer.

Normalizes Work-Brain records — email, conversations, documents, tasks, orders, inventory,
production and maintenance events — into a typed canonical payload.

The guiding rule is **preserve, do not interpret**. This normalizer:

* keeps original text verbatim (it is never summarized, classified, or rewritten);
* keeps source metadata verbatim;
* extracts only *structural* identifiers (a message id, a document id, an order number) that
  a later layer needs to correlate records;
* records no relationships and infers no meaning.

Summarization, classification, and relationship inference belong to Layers 5 and 6, not
here. Doing them here would bake a model's judgement into the durable record.
"""

from __future__ import annotations

from typing import Any, ClassVar

from humisense.layer2.errors import ErrorCode, Stage, StageError
from humisense.layer2.normalizers.base import (
    BaseNormalizer,
    NormalizationInput,
    NormalizedResult,
)

CANONICAL_SCHEMA = "2.0"

# Structural identifier aliases per business record kind.
_IDENTIFIER_KEYS: dict[str, tuple[str, ...]] = {
    "email": ("message_id", "email_id", "id"),
    "conversation": ("conversation_id", "thread_id", "id"),
    "document": ("document_id", "doc_id", "id"),
    "task": ("task_id", "activity_id", "id"),
    "order": ("order_id", "purchase_order_id", "po_number", "id"),
    "inventory": ("inventory_id", "item_id", "sku", "id"),
    "production": ("production_id", "work_order_id", "batch_id", "id"),
    "maintenance": ("maintenance_id", "work_order_id", "id"),
}

_TEXT_KEYS = ("body", "text", "content", "description", "subject", "title", "summary_text")
_PARTY_KEYS = ("from", "sender", "author", "owner", "assignee", "customer_id", "supplier_id")


class BusinessNormalizer(BaseNormalizer):
    """Normalizes business and organizational records."""

    name: ClassVar[str] = "business"
    version: ClassVar[str] = "1.0.0"
    supported_record_types: ClassVar[tuple[str, ...]] = (
        "email_message",
        "conversation",
        "document",
        "task",
        "purchase_order",
        "inventory_record",
        "production_record",
        "maintenance_event",
        "business_record",
    )
    input_schema_versions: ClassVar[tuple[str, ...]] = ("1.0",)
    output_schema_version: ClassVar[str] = CANONICAL_SCHEMA

    def normalize(self, data: NormalizationInput) -> NormalizedResult | list[StageError]:
        payload = data.payload
        record_kind = _business_kind(data.record_type)

        identifier = _first_present(payload, _IDENTIFIER_KEYS.get(record_kind, ("id",)))
        if identifier is None:
            return [
                StageError(
                    stage=Stage.NORMALIZATION,
                    code=ErrorCode.MISSING_REQUIRED_FIELD,
                    detail="A business record needs a structural identifier",
                    field="id",
                )
            ]

        # Text is preserved verbatim, bounded only in length, never rewritten.
        text_value = _first_present(payload, _TEXT_KEYS)
        party = _first_present(payload, _PARTY_KEYS)

        normalized_payload: dict[str, Any] = {
            "record_kind": record_kind,
            "identifier": str(identifier),
            "text": str(text_value) if text_value is not None else None,
            "party": str(party) if party is not None else None,
        }

        # Everything else is preserved as an extension, so no source field is lost.
        consumed = set(_TEXT_KEYS) | set(_PARTY_KEYS)
        for keys in _IDENTIFIER_KEYS.values():
            consumed.update(keys)
        extensions: dict[str, Any] = {}
        for key, item in payload.items():
            if key not in consumed:
                extensions[key] = item

        return NormalizedResult(
            normalized_payload=normalized_payload,
            data_family="business",
            entity_type=record_kind,
            event_type="record",
            extensions=extensions,
            metadata={"text_preserved": text_value is not None},
        )


def _business_kind(record_type: str) -> str:
    lowered = record_type.lower()
    for kind in _IDENTIFIER_KEYS:
        if kind in lowered:
            return kind
    if "mail" in lowered:
        return "email"
    if "doc" in lowered:
        return "document"
    if "order" in lowered:
        return "order"
    if "inventory" in lowered:
        return "inventory"
    return "record"


def _first_present(payload: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in payload and payload[key] not in (None, ""):
            return payload[key]
    return None
