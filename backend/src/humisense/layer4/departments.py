"""Departmental work intelligence.

Maps source records onto tenant departments and provides per-department aggregation for the
managerial view. The mapping is explicit and tenant-configured; Layer 4 does not guess that a
record belongs to a department because a word appears in its text.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from humisense.db.models import WorkDomain

# Default department seeds offered to a tenant. They are *seeds*, not engine logic: a tenant
# may add, rename, or remove them, and nothing in the derivation depends on these keys.
DEFAULT_DEPARTMENTS: tuple[tuple[str, str, WorkDomain], ...] = (
    ("production", "Production", WorkDomain.PRODUCTION),
    ("procurement", "Procurement and Purchase", WorkDomain.PROCUREMENT),
    ("sales", "Sales and Customer Management", WorkDomain.SALES),
    ("maintenance", "Maintenance and Engineering", WorkDomain.MAINTENANCE),
    ("quality", "Quality", WorkDomain.QUALITY),
    ("finance", "Finance and Accounts", WorkDomain.FINANCE),
    ("management", "Management and Executive Office", WorkDomain.MANAGEMENT),
)

# Record kinds (Layer 2 business normalizer ``record_kind``/``entity_type``) mapped to the
# domain they most plausibly belong to. This maps a *record kind* to a *domain*, not a record
# to a department: the department is resolved from the tenant's configured departments.
RECORD_KIND_DOMAIN: dict[str, WorkDomain] = {
    "order": WorkDomain.PROCUREMENT,
    "purchase_order": WorkDomain.PROCUREMENT,
    "inventory": WorkDomain.PROCUREMENT,
    "production": WorkDomain.PRODUCTION,
    "maintenance": WorkDomain.MAINTENANCE,
    "task": WorkDomain.GENERAL,
    "document": WorkDomain.GENERAL,
    "email": WorkDomain.GENERAL,
    "conversation": WorkDomain.GENERAL,
    "record": WorkDomain.GENERAL,
}

# Explicit payload keys that name a department or domain. Only these establish a department;
# free text is never scanned.
DEPARTMENT_KEYS = ("department", "department_id", "department_key", "cost_center", "team")
DOMAIN_KEYS = ("domain", "work_domain", "business_area")


@dataclass(frozen=True, slots=True)
class DepartmentMapping:
    """Maps a tenant's department keys to ids and resolves a record's domain."""

    by_key: dict[str, uuid.UUID]
    domain_by_key: dict[str, WorkDomain]

    def resolve_department(
        self, payload: dict[str, object], *, record_kind: str
    ) -> uuid.UUID | None:
        """Resolve a department id from an explicit field, or from the record kind's domain.

        Returns None when nothing establishes a department; the work item then has no
        department rather than a guessed one.
        """
        for key in DEPARTMENT_KEYS:
            value = payload.get(key)
            if value in (None, ""):
                continue
            department_id = self.by_key.get(str(value).strip().lower())
            if department_id is not None:
                return department_id
        domain = resolve_domain(payload, record_kind=record_kind)
        for key, candidate_domain in self.domain_by_key.items():
            if candidate_domain == domain and domain != WorkDomain.GENERAL:
                return self.by_key[key]
        return None


def resolve_domain(payload: dict[str, object], *, record_kind: str) -> WorkDomain:
    """Return the work domain for a record.

    Prefers an explicit domain field; falls back to the record kind's domain; otherwise
    ``GENERAL``. Never guesses from text.
    """
    for key in DOMAIN_KEYS:
        value = payload.get(key)
        if value in (None, ""):
            continue
        try:
            return WorkDomain(str(value).strip().lower())
        except ValueError:
            continue
    return RECORD_KIND_DOMAIN.get(record_kind.lower(), WorkDomain.GENERAL)


def build_department_mapping(
    departments: list[tuple[str, uuid.UUID, WorkDomain]],
) -> DepartmentMapping:
    """Build the mapping from a tenant's ``(key, id, domain)`` rows."""
    by_key = {key.lower(): department_id for key, department_id, _ in departments}
    domain_by_key = {key.lower(): domain for key, _, domain in departments}
    return DepartmentMapping(by_key=by_key, domain_by_key=domain_by_key)


__all__ = [
    "DEFAULT_DEPARTMENTS",
    "DEPARTMENT_KEYS",
    "DOMAIN_KEYS",
    "RECORD_KIND_DOMAIN",
    "DepartmentMapping",
    "build_department_mapping",
    "resolve_domain",
]
