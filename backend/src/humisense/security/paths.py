"""Path safety for file-backed connectors.

The rules are deliberately strict, because a CSV connector that accepts an arbitrary path
is an arbitrary-file-read primitive:

* Every path must resolve, after following symlinks, inside one of the configured roots.
* Paths containing a NUL byte are rejected before touching the filesystem.
* Only regular files are opened; directories and device nodes are rejected.
* The root list comes from server configuration, never from a request.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from humisense.errors import PathNotAllowedError


@dataclass(frozen=True, slots=True)
class ResolvedPath:
    """A path proven to be inside an allowed root."""

    path: Path
    root: Path
    size_bytes: int

    @property
    def relative_name(self) -> str:
        return str(self.path.relative_to(self.root))


class PathPolicy:
    """Resolves and validates filesystem paths against an allowlist of roots."""

    def __init__(self, allowed_roots: tuple[str, ...], *, max_file_bytes: int) -> None:
        if not allowed_roots:
            raise PathNotAllowedError(
                detail="No allowed roots are configured; file connectors are disabled"
            )
        self._roots = tuple(self._canonical_root(root) for root in allowed_roots)
        self._max_file_bytes = max_file_bytes

    @property
    def roots(self) -> tuple[Path, ...]:
        return self._roots

    @staticmethod
    def _canonical_root(root: str) -> Path:
        candidate = Path(root).expanduser()
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise PathNotAllowedError(
                detail=f"Configured allowed root {root!r} is not usable",
            ) from exc
        if not resolved.is_dir():
            raise PathNotAllowedError(detail=f"Configured allowed root {root!r} is not a directory")
        return resolved

    def resolve_file(self, requested: str, *, must_exist: bool = True) -> ResolvedPath:
        """Resolve ``requested`` to a regular file inside an allowed root.

        Args:
            requested: An absolute path, or a path relative to an allowed root.
            must_exist: When false, the containment check runs without requiring the
                file to exist. Used by configuration validation, which must be able to
                accept a path that will exist later.

        Raises:
            PathNotAllowedError: if the path escapes every root, is not a regular file,
                is too large, or cannot be resolved safely.
        """
        if not requested or not isinstance(requested, str):
            raise PathNotAllowedError(detail="A file path is required")
        if "\x00" in requested:
            raise PathNotAllowedError(detail="Path contains a NUL byte")
        if len(requested) > 4096:
            raise PathNotAllowedError(detail="Path is unreasonably long")

        candidate = Path(requested).expanduser()

        resolved: Path | None = None
        root: Path | None = None
        for allowed_root in self._roots:
            attempt = candidate if candidate.is_absolute() else allowed_root / candidate
            try:
                # strict=True follows symlinks, so a symlink pointing outside the root
                # resolves outside the root and is rejected by the containment check.
                outcome = attempt.resolve(strict=must_exist)
            except (OSError, RuntimeError):
                continue
            if self._is_within(outcome, allowed_root):
                resolved = outcome
                root = allowed_root
                break

        if resolved is None or root is None:
            raise PathNotAllowedError(
                detail="Path is not inside any configured allowed root",
                context={"requested_length": len(requested)},
            )

        if not must_exist:
            return ResolvedPath(path=resolved, root=root, size_bytes=0)

        try:
            file_stat = resolved.stat()
        except OSError as exc:
            raise PathNotAllowedError(detail="Path could not be inspected") from exc

        if not stat.S_ISREG(file_stat.st_mode):
            raise PathNotAllowedError(detail="Path is not a regular file")
        if file_stat.st_size > self._max_file_bytes:
            raise PathNotAllowedError(
                detail="File exceeds the configured maximum size",
                context={"size_bytes": file_stat.st_size, "max_bytes": self._max_file_bytes},
            )
        return ResolvedPath(path=resolved, root=root, size_bytes=file_stat.st_size)

    @staticmethod
    def _is_within(candidate: Path, root: Path) -> bool:
        """Return True when ``candidate`` is ``root`` or a descendant of it."""
        try:
            candidate.relative_to(root)
        except ValueError:
            return False
        return True

    @staticmethod
    def is_symlink(path: Path) -> bool:
        return path.is_symlink()


def sanitize_filename(name: str, *, fallback: str = "upload") -> str:
    """Reduce an untrusted filename to a safe basename.

    Used for display and for the stored upload name. The result never contains a path
    separator, a traversal sequence, or a control character.
    """
    if not name:
        return fallback
    base = os.path.basename(name.replace("\\", "/"))
    cleaned = "".join(ch for ch in base if ch.isprintable() and ch not in {'"', "'", "<", ">", "|"})
    cleaned = cleaned.strip().strip(".")
    if not cleaned or cleaned in {".", ".."}:
        return fallback
    return cleaned[:128]


def safe_download_cell(value: str) -> str:
    """Neutralize spreadsheet formula injection in exported cell values.

    A cell beginning with one of ``= + - @`` (or a tab/CR followed by one) is interpreted
    as a formula by spreadsheet applications. Prefixing an apostrophe forces text
    interpretation while preserving the visible content.
    """
    if not value:
        return value
    dangerous = ("=", "+", "-", "@", "\t", "\r")
    if value.startswith(dangerous):
        return f"'{value}"
    return value
