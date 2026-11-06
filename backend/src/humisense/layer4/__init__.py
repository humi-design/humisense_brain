"""Layer 4: Work Intelligence Engine.

Turns verified Layer 2 canonical records and Layer 3 detection events into a structured,
connected representation of organizational work. Layer 4 understands work; it does not
remember, connect, retrieve evidence, reason, plan, or act. Those belong to Layers 5+.

The package is deterministic and dependency-free: derivation, classification, deadline
arithmetic, dependency resolution, blocker inference, follow-up computation, and conflict
detection are all pure standard-library logic, so every output is explainable and
reproducible.
"""

from __future__ import annotations

from humisense.layer4.contract import WORK_PROCESSING_VERSION, WORK_SCHEMA_VERSION

__all__ = ["WORK_PROCESSING_VERSION", "WORK_SCHEMA_VERSION"]
