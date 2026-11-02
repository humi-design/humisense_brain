"""Severity scoring.

Severity is a business judgement, not a restatement of the anomaly score. A z-score of 6 is
not automatically CRITICAL: a large deviation that lasted one sample on a noisy sensor is
less serious than a modest deviation that has persisted for hours on a critical asset.

Severity combines six inputs, each of which can only *raise* it:

* **magnitude** — how far the observation is from what was expected, in robust terms;
* **persistence** — how many consecutive samples have shown the condition;
* **duration** — how long the condition has held;
* **rarity** — how unusual this magnitude is for the signal's own history;
* **spread** — how far outside the baseline's central band the value sits;
* **data quality** — a detection on degraded data is never CRITICAL.

The result is deterministic and explainable: the same inputs always produce the same
severity, and the contributing factors are returned alongside it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from humisense.layer3.contract import SEVERITY_ORDER, DataQualityStatus, Severity

# Robust-z magnitudes at which each severity becomes reachable, when other factors are neutral.
MAGNITUDE_THRESHOLDS: tuple[tuple[float, Severity], ...] = (
    (12.0, Severity.CRITICAL),
    (8.0, Severity.HIGH),
    (4.5, Severity.MEDIUM),
    (3.0, Severity.LOW),
)
"""A value beyond 3 robust deviations is at least LOW; beyond 12 at least CRITICAL."""

PERSISTENCE_BONUS: tuple[tuple[int, int], ...] = (
    (20, 2),
    (8, 1),
)
"""(consecutive samples, severity levels added)."""

DURATION_BONUS: tuple[tuple[float, int], ...] = (
    (3600.0, 2),
    (900.0, 1),
)
"""(seconds held, severity levels added)."""


@dataclass(slots=True)
class SeverityAssessment:
    """A severity and the factors that produced it."""

    severity: Severity
    factors: dict[str, float] = field(default_factory=dict)
    rationale: str = ""


def _severity_index(severity: Severity) -> int:
    return SEVERITY_ORDER.index(severity)


def _from_index(index: int) -> Severity:
    return SEVERITY_ORDER[max(0, min(index, len(SEVERITY_ORDER) - 1))]


def assess_severity(
    *,
    magnitude: float | None,
    persistence: int = 1,
    duration_seconds: float = 0.0,
    rarity: float | None = None,
    spread: float | None = None,
    data_quality: DataQualityStatus = DataQualityStatus.GOOD,
) -> SeverityAssessment:
    """Combine the severity factors into a final severity.

    ``magnitude`` is the absolute robust z-score of the deviation. ``rarity`` is a
    probability-like figure in [0, 1] (smaller is rarer). ``spread`` is the absolute
    distance from the baseline median expressed in baseline standard deviations.
    """
    factors: dict[str, float] = {}
    base_index = 0

    for threshold, severity in MAGNITUDE_THRESHOLDS:
        if magnitude is not None and abs(magnitude) >= threshold:
            base_index = _severity_index(severity)
            factors["magnitude"] = abs(magnitude)
            break

    # An unclear magnitude is INFO unless another factor raises it.
    if not factors and magnitude is not None:
        factors["magnitude"] = abs(magnitude)

    index = base_index
    if persistence >= 20:
        index += 2
        factors["persistence"] = float(persistence)
    elif persistence >= 8:
        index += 1
        factors["persistence"] = float(persistence)

    for seconds, bonus in DURATION_BONUS:
        if duration_seconds >= seconds:
            index += bonus
            factors["duration_seconds"] = duration_seconds
            break

    if rarity is not None and rarity <= 0.001:
        index += 1
        factors["rarity"] = rarity
    if spread is not None and abs(spread) >= 6.0:
        index += 1
        factors["spread"] = abs(spread)

    index = min(index, len(SEVERITY_ORDER) - 1)

    # A detection on degraded or unknown data may be notable, but not CRITICAL: the
    # evidence itself is not trustworthy enough for the top severity.
    if data_quality in {
        DataQualityStatus.DEGRADED,
        DataQualityStatus.UNKNOWN,
        DataQualityStatus.ACCEPTABLE,
    }:
        index = min(index, _severity_index(Severity.HIGH))
        factors["quality_cap"] = 1.0

    severity = _from_index(index)
    rationale = _rationale(severity, factors)
    return SeverityAssessment(severity=severity, factors=factors, rationale=rationale)


def _rationale(severity: Severity, factors: dict[str, float]) -> str:
    parts: list[str] = []
    if "magnitude" in factors:
        parts.append(f"magnitude {factors['magnitude']:.1f} robust deviations")
    if "persistence" in factors:
        parts.append(f"persistent for {int(factors['persistence'])} samples")
    if "duration_seconds" in factors:
        parts.append(f"held for {factors['duration_seconds'] / 60:.0f} minutes")
    if "rarity" in factors:
        parts.append(f"rarity {factors['rarity']:.4f}")
    if "spread" in factors:
        parts.append(f"{factors['spread']:.1f} baseline deviations from centre")
    if "quality_cap" in factors:
        parts.append("capped because the window is not clean")
    if not parts:
        return f"Severity {severity.value}: no single factor dominated."
    return f"Severity {severity.value} from " + ", ".join(parts) + "."


def rarity_from_z(magnitude: float | None) -> float | None:
    """A rough two-sided rarity for a robust z-score, without a normal CDF dependency.

    Uses a bounded rational approximation of the normal tail so a rarity figure exists
    without SciPy. The figure is a heuristic, and is only ever used to *raise* severity.
    """
    if magnitude is None or not math.isfinite(magnitude):
        return None
    z = abs(magnitude)
    # Tail approximation: P(|Z| > z) ~ exp(-z^2 / 2) / (z * sqrt(2*pi)) for z > 0.
    if z == 0:
        return 1.0
    tail = math.exp(-0.5 * z * z) / (z * math.sqrt(2.0 * math.pi))
    return max(0.0, min(1.0, 2.0 * tail))


__all__ = ["SeverityAssessment", "assess_severity", "rarity_from_z"]
