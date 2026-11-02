"""Normalizer SDK and the static registry.

A normalizer turns a validated source payload into a typed, canonical
``normalized_payload``. The registry is a **static allowlist** populated at import time
from an explicit list, mirroring the Layer 1 connector registry. There is no mechanism to
load a normalizer from a name, path, or module supplied by a request or a record, so a
crafted payload cannot turn into arbitrary code execution.

Every normalizer declares:

* the record types it supports;
* the input schema versions it accepts;
* the output schema version it produces;
* a version, so a stored record can be tied to the code that produced it.

A normalizer returns either a :class:`NormalizedResult` or a list of :class:`StageError`.
It never raises for bad input; raising is reserved for a programming error.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar

from humisense.layer2.errors import StageError


@dataclass(slots=True)
class NormalizationInput:
    """Everything a normalizer needs, and nothing more.

    The normalizer receives the preserved source payload and the metadata Layer 1 attached.
    It has no database session, no network, and no way to reach another tenant's data.
    """

    record_type: str
    source_schema_version: str
    payload: dict[str, Any]
    metadata: dict[str, Any] = field(default_factory=dict)
    source_timestamp: datetime | None = None


@dataclass(slots=True)
class NormalizedResult:
    """The output of a successful normalization."""

    normalized_payload: dict[str, Any]
    data_family: str
    entity_type: str | None = None
    event_type: str | None = None
    quality_code: str | None = None
    unit_conversions: list[dict[str, Any]] = field(default_factory=list)
    extensions: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    event_timestamp: datetime | None = None
    """Set only when the source distinguishes the event time from the sample time."""


class BaseNormalizer(ABC):
    """Contract every normalizer implements."""

    name: ClassVar[str]
    version: ClassVar[str]
    supported_record_types: ClassVar[tuple[str, ...]]
    input_schema_versions: ClassVar[tuple[str, ...]]
    output_schema_version: ClassVar[str]

    def supports(self, record_type: str, schema_version: str) -> bool:
        return (
            record_type in self.supported_record_types
            and schema_version in self.input_schema_versions
        )

    @abstractmethod
    def normalize(self, data: NormalizationInput) -> NormalizedResult | list[StageError]:
        """Normalize one validated record, or return structured errors.

        Implementations must be deterministic: the same input yields the same output.
        """


class NormalizerRegistry:
    """Static registry of normalizers, keyed by record type."""

    def __init__(self) -> None:
        self._normalizers: list[BaseNormalizer] = []

    def register(self, normalizer: BaseNormalizer) -> BaseNormalizer:
        if not getattr(normalizer, "name", None):
            raise ValueError(f"{type(normalizer).__name__} does not declare a name")
        if not normalizer.supported_record_types:
            raise ValueError(f"{normalizer.name} does not declare any supported record types")
        self._normalizers.append(normalizer)
        return normalizer

    def find(self, record_type: str, schema_version: str) -> BaseNormalizer | None:
        """Return the first normalizer that supports this record type and version.

        Registration order is precedence order. Returning None (rather than raising) lets
        the pipeline quarantine the record with a clear ``NO_NORMALIZER`` code.
        """
        for normalizer in self._normalizers:
            if normalizer.supports(record_type, schema_version):
                return normalizer
        return None

    def __len__(self) -> int:
        return len(self._normalizers)

    def names(self) -> list[str]:
        return [n.name for n in self._normalizers]


registry = NormalizerRegistry()


def build_default_registry() -> NormalizerRegistry:
    """Return the process-wide registry, loading built-in normalizers once.

    The imports are explicit, so the set of registrable normalizers is visible in a code
    review and cannot be influenced by configuration or by record content.
    """
    if len(registry) == 0:
        from humisense.layer2.normalizers.business import BusinessNormalizer
        from humisense.layer2.normalizers.generic import GenericNormalizer
        from humisense.layer2.normalizers.historian import HistorianNormalizer

        for normalizer in (HistorianNormalizer(), BusinessNormalizer(), GenericNormalizer()):
            registry.register(normalizer)
    return registry
