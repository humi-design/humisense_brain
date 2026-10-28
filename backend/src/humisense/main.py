"""Application entry point.

Run with::

    uv run uvicorn humisense.main:app --reload --port 8000
"""

from __future__ import annotations

from humisense.app import create_app

app = create_app()

__all__ = ["app"]
