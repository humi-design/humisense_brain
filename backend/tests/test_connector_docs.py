"""Every advertised documentation link must resolve to a real file.

Regression: the Canary manifest pointed at ``docs/CANARY.md``, which had never been
created. Nothing failed — the Vite dev server's SPA fallback served ``index.html`` for the
missing path, so the Connector Catalog's "Documentation" link opened a blank page instead
of an error. A 200 response alone therefore proves nothing; the file has to exist.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from humisense.connectors.base import get_manifest, load_builtin_connectors, registry

pytestmark = pytest.mark.security

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_every_documentation_url_points_at_a_file_that_exists() -> None:
    missing: list[str] = []
    load_builtin_connectors()
    for manifest in registry.manifests():
        url = manifest.documentation_url
        if not url:
            continue
        # Documentation links are repo-relative paths served from the frontend's public
        # directory. An absolute URL points at a third party and cannot be checked here.
        if "://" in url:
            continue
        target = REPO_ROOT / url
        if not target.is_file():
            missing.append(f"{manifest.connector_type}: {url}")

    assert missing == [], f"documentation_url targets that do not exist: {missing}"


def test_the_canary_documentation_link_matches_its_manifest() -> None:
    """The specific link an operator sees must be the file that documents the blocker."""
    load_builtin_connectors()
    canary = get_manifest("canary_labs")
    assert canary.documentation_url is not None

    text = (REPO_ROOT / canary.documentation_url).read_text()
    assert "not configured" in text.lower()
    assert "no network call" in text.lower()
