"""Path policy and file-safety tests.

Covers T7 (path traversal, symlink escape, arbitrary file access) and part of T8
(malformed and oversized files).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from humisense.errors import PathNotAllowedError
from humisense.security.paths import PathPolicy, safe_download_cell, sanitize_filename

pytestmark = pytest.mark.security


@pytest.fixture
def roots(tmp_path: Path) -> tuple[Path, Path]:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    (allowed / "nested").mkdir()
    (allowed / "data.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    (allowed / "nested" / "inner.csv").write_text("a,b\n3,4\n", encoding="utf-8")

    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "credentials.txt").write_text("do-not-read", encoding="utf-8")
    return allowed, secret


@pytest.fixture
def policy(roots: tuple[Path, Path]) -> PathPolicy:
    allowed, _ = roots
    return PathPolicy((str(allowed),), max_file_bytes=1_000_000)


def test_relative_path_inside_root_is_allowed(policy: PathPolicy, roots: tuple[Path, Path]) -> None:
    allowed, _ = roots
    resolved = policy.resolve_file("data.csv")
    assert resolved.path == (allowed / "data.csv").resolve()


def test_nested_relative_path_is_allowed(policy: PathPolicy) -> None:
    resolved = policy.resolve_file("nested/inner.csv")
    assert resolved.path.name == "inner.csv"


def test_absolute_path_inside_root_is_allowed(policy: PathPolicy, roots: tuple[Path, Path]) -> None:
    allowed, _ = roots
    resolved = policy.resolve_file(str(allowed / "data.csv"))
    assert resolved.path.name == "data.csv"


@pytest.mark.parametrize(
    "attempt",
    [
        "../secret/credentials.txt",
        "../../etc/passwd",
        "nested/../../secret/credentials.txt",
        "/etc/passwd",
        "/etc/shadow",
        "....//....//etc/passwd",
        "nested/../../../root/.ssh/id_rsa",
    ],
)
def test_traversal_attempts_are_rejected(policy: PathPolicy, attempt: str) -> None:
    with pytest.raises(PathNotAllowedError):
        policy.resolve_file(attempt)


def test_absolute_path_outside_root_is_rejected(
    policy: PathPolicy, roots: tuple[Path, Path]
) -> None:
    _, secret = roots
    with pytest.raises(PathNotAllowedError):
        policy.resolve_file(str(secret / "credentials.txt"))


def test_symlink_escaping_root_is_rejected(policy: PathPolicy, roots: tuple[Path, Path]) -> None:
    """A symlink is resolved before the containment check, so it cannot escape."""
    allowed, secret = roots
    link = allowed / "escape.csv"
    try:
        os.symlink(secret / "credentials.txt", link)
    except OSError:  # pragma: no cover - platform without symlink support
        pytest.skip("symlinks are not available")
    with pytest.raises(PathNotAllowedError):
        policy.resolve_file("escape.csv")


def test_symlink_inside_root_is_allowed(policy: PathPolicy, roots: tuple[Path, Path]) -> None:
    allowed, _ = roots
    link = allowed / "alias.csv"
    try:
        os.symlink(allowed / "data.csv", link)
    except OSError:  # pragma: no cover
        pytest.skip("symlinks are not available")
    resolved = policy.resolve_file("alias.csv")
    assert resolved.path == (allowed / "data.csv").resolve()


def test_symlinked_directory_escape_is_rejected(
    policy: PathPolicy, roots: tuple[Path, Path]
) -> None:
    allowed, secret = roots
    link = allowed / "secretdir"
    try:
        os.symlink(secret, link)
    except OSError:  # pragma: no cover
        pytest.skip("symlinks are not available")
    with pytest.raises(PathNotAllowedError):
        policy.resolve_file("secretdir/credentials.txt")


def test_nul_byte_is_rejected(policy: PathPolicy) -> None:
    with pytest.raises(PathNotAllowedError):
        policy.resolve_file("data.csv\x00.txt")


def test_directory_is_rejected(policy: PathPolicy) -> None:
    with pytest.raises(PathNotAllowedError):
        policy.resolve_file("nested")


def test_empty_path_is_rejected(policy: PathPolicy) -> None:
    with pytest.raises(PathNotAllowedError):
        policy.resolve_file("")


def test_overlong_path_is_rejected(policy: PathPolicy) -> None:
    with pytest.raises(PathNotAllowedError):
        policy.resolve_file("a" * 5000)


def test_oversized_file_is_rejected(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    (allowed / "big.csv").write_bytes(b"x" * 2048)
    policy = PathPolicy((str(allowed),), max_file_bytes=1024)
    with pytest.raises(PathNotAllowedError):
        policy.resolve_file("big.csv")


def test_policy_requires_at_least_one_root() -> None:
    with pytest.raises(PathNotAllowedError):
        PathPolicy((), max_file_bytes=1024)


def test_nonexistent_root_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(PathNotAllowedError):
        PathPolicy((str(tmp_path / "missing"),), max_file_bytes=1024)


def test_file_root_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "file.txt"
    target.write_text("x", encoding="utf-8")
    with pytest.raises(PathNotAllowedError):
        PathPolicy((str(target),), max_file_bytes=1024)


def test_must_exist_false_allows_future_file(policy: PathPolicy) -> None:
    """Configuration validation must accept a path that will exist at capture time."""
    resolved = policy.resolve_file("later.csv", must_exist=False)
    assert resolved.path.name == "later.csv"
    with pytest.raises(PathNotAllowedError):
        policy.resolve_file("later.csv")


def test_must_exist_false_still_enforces_containment(policy: PathPolicy) -> None:
    with pytest.raises(PathNotAllowedError):
        policy.resolve_file("../secret/credentials.txt", must_exist=False)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("../../etc/passwd", "passwd"),
        ("/absolute/path.csv", "path.csv"),
        ("windows\\path.csv", "path.csv"),
        ("..", "upload"),
        ("", "upload"),
        (".", "upload"),
        ("name\x00.csv", "name.csv"),
        ("<script>.csv", "script.csv"),
    ],
)
def test_sanitize_filename(raw: str, expected: str) -> None:
    assert sanitize_filename(raw) == expected


def test_sanitize_filename_bounds_length() -> None:
    assert len(sanitize_filename("x" * 500)) == 128


@pytest.mark.parametrize(
    "value",
    ["=1+1", "+cmd", "-2+3", "@SUM(A1)", "\tpayload", "\rpayload"],
)
def test_formula_injection_is_neutralized(value: str) -> None:
    """T8: an exported cell must not be interpreted as a formula by a spreadsheet."""
    assert safe_download_cell(value).startswith("'")


def test_ordinary_cell_is_unchanged() -> None:
    assert safe_download_cell("LINE-01") == "LINE-01"
    assert safe_download_cell("60.000") == "60.000"
    assert safe_download_cell("") == ""
