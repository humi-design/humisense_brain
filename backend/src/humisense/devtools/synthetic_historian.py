"""Synthetic historian CSV fixture.

**This data is synthetic.** It was generated for development and testing. It is not
Canary Labs telemetry, not real historian output, and must never be presented as live
plant data. The file is committed so that the CSV connector has a deterministic
end-to-end fixture and so tests do not depend on a customer environment.

Columns: ``timestamp,machine_id,tag,value,unit``. Values follow a simple deterministic
sine plus offset pattern so tests can assert on specific rows without floating-point
surprises.
"""

from __future__ import annotations

import csv
import math
from pathlib import Path

SYNTHETIC_HEADER = (
    "# SYNTHETIC DATA - NOT REAL HISTORIAN TELEMETRY - GENERATED FOR DEVELOPMENT ONLY"
)
HEADER_ROW = ["timestamp", "machine_id", "tag", "value", "unit"]
MACHINES = ("LINE-01", "LINE-02", "LINE-03")
TAGS = (
    ("temperature", "degC", 60.0, 5.0),
    ("pressure", "bar", 4.5, 0.6),
    ("vibration", "mm_s", 1.2, 0.3),
    ("throughput", "units_min", 120.0, 12.0),
)


def generate_rows(count: int, *, start_minute: int = 0) -> list[list[str]]:
    """Return ``count`` synthetic rows with a deterministic pattern."""
    rows: list[list[str]] = []
    for index in range(count):
        minute = start_minute + index
        machine = MACHINES[index % len(MACHINES)]
        tag, unit, base, amplitude = TAGS[(index // len(MACHINES)) % len(TAGS)]
        value = base + amplitude * math.sin(index / 7.0)
        timestamp = f"2026-01-01T{minute // 60:02d}:{minute % 60:02d}:00Z"
        rows.append([timestamp, machine, tag, f"{value:.3f}", unit])
    return rows


def write_fixture(path: Path, *, row_count: int = 500) -> Path:
    """Write the synthetic fixture file. Returns the path written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(f"{SYNTHETIC_HEADER}\n")
        writer = csv.writer(handle)
        writer.writerow(HEADER_ROW)
        writer.writerows(generate_rows(row_count))
    return path


def write_malformed_fixture(path: Path) -> Path:
    """Write a fixture with a deliberately inconsistent row width."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(HEADER_ROW)
        writer.writerow(["2026-01-01T00:00:00Z", "LINE-01", "temperature", "60.000", "degC"])
        writer.writerow(["2026-01-01T00:01:00Z", "LINE-01", "pressure"])
        writer.writerow(["2026-01-01T00:02:00Z", "LINE-02", "vibration", "1.100", "mm_s"])
    return path
