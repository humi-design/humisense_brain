"""Allow ``python -m humisense`` to run the operator CLI."""

from __future__ import annotations

import sys

from humisense.devtools.cli import main

if __name__ == "__main__":
    sys.exit(main())
