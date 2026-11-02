"""Documentation must not cite tests or files that do not exist.

Regression: `docs/THREAT_MODEL.md` cited test files (``test_rls.py``,
``test_security_authz.py``, ...) and test functions (``::test_audit_immutable``, ...) that
were never created, and `docs/ARCHITECTURE.md` referenced a ``tools/`` directory that does
not exist. A reviewer who follows a citation to verify a control finds nothing, which
undermines trust in the controls that are real. These tests fail the build when a cited
artifact is missing.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.security

REPO_ROOT = Path(__file__).resolve().parents[2]
TESTS_DIR = Path(__file__).resolve().parent

# Documents whose live test references must resolve. README and AGENTS.md describe the
# test suite at a high level and are included because they also name files.
#
# `docs/AUDIT-LAYER1.md` is deliberately excluded: it is a point-in-time record that names
# the test files which were *missing* when the audit ran, so those names would fail a
# citation check by design. Its own live references are checked separately below.
DOCUMENTS = (
    REPO_ROOT / "docs" / "THREAT_MODEL.md",
    REPO_ROOT / "docs" / "ARCHITECTURE.md",
    REPO_ROOT / "docs" / "CANARY.md",
    REPO_ROOT / "README.md",
    REPO_ROOT / "AGENTS.md",
)

# `test_file.py` optionally followed by `::test_name`.
_REFERENCE = re.compile(r"`(test_[a-z0-9_]+\.py)(?:::([a-zA-Z0-9_]+))?`")


def _references() -> list[tuple[Path, str, str | None]]:
    found: list[tuple[Path, str, str | None]] = []
    for document in DOCUMENTS:
        if not document.is_file():
            continue
        for match in _REFERENCE.finditer(document.read_text(encoding="utf-8")):
            found.append((document, match.group(1), match.group(2)))
    return found


def test_documents_only_cite_test_files_that_exist() -> None:
    missing = [
        f"{document.name}: {name}"
        for document, name, _ in _references()
        if not (TESTS_DIR / name).is_file()
    ]
    assert missing == [], f"documentation cites missing test files: {missing}"


def test_documents_only_cite_test_functions_that_exist() -> None:
    missing = []
    for document, name, function in _references():
        if function is None:
            continue
        body = (TESTS_DIR / name).read_text(encoding="utf-8")
        if f"def {function}(" not in body:
            missing.append(f"{document.name}: {name}::{function}")
    assert missing == [], f"documentation cites missing test functions: {missing}"


def test_the_audit_report_references_resolve() -> None:
    """The audit report is a security document; its live citations must be verifiable."""
    report = REPO_ROOT / "docs" / "AUDIT-LAYER1.md"
    assert report.is_file(), "the Layer 1 audit report must exist"
    text = report.read_text(encoding="utf-8")
    for finding in ("F1", "F2", "F3"):
        assert finding in text
    # Files the report names as existing must exist.
    for cited in (
        "docs/SECRETS.md",
        "backend/src/humisense/connectors/csv_connector.py",
        "backend/tests/test_csv_connector.py",
        "backend/tests/test_config_hardening.py",
        ".github/workflows/ci.yml",
    ):
        assert (REPO_ROOT / cited).is_file(), f"audit report cites a missing file: {cited}"
