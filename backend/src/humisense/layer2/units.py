"""Explicit unit handling for industrial normalization.

Two rules drive this module:

1. **No unit is assumed.** A historian tag's unit is whatever the source reports. Different
   tags on the same asset routinely use different units, so the unit travels with the value.
2. **No conversion is silent.** A conversion records the original value and unit, the
   normalized value and unit, and the rule that connects them. A consumer can audit or
   reverse it, and a missing rule is a *reported error*, not a guess.

Layer 2 normalizes a small, well-defined set of units to a canonical form (SI where one
exists). It never interpolates, never fills a missing value, and never invents a unit.
"""

from __future__ import annotations

from dataclasses import dataclass

CANONICAL_UNITS: dict[str, str] = {
    "temperature": "degC",
    "pressure": "kPa",
    "vibration": "mm_s",
    "throughput": "units_min",
    "mass": "kg",
    "length": "m",
    "duration": "s",
    "power": "kW",
    "energy": "kWh",
    "flow": "L_min",
    "voltage": "V",
    "current": "A",
    "frequency": "Hz",
    "speed": "m_s",
    "dimensionless": "1",
}
"""The canonical unit for each physical quantity Layer 2 normalizes."""


@dataclass(frozen=True, slots=True)
class ConversionRule:
    """An affine conversion: ``normalized = original * factor + offset``."""

    rule_id: str
    source_unit: str
    target_unit: str
    factor: float
    offset: float = 0.0

    def apply(self, value: float) -> float:
        return value * self.factor + self.offset


def _rule(
    rule_id: str, source: str, target: str, factor: float, offset: float = 0.0
) -> ConversionRule:
    return ConversionRule(
        rule_id=rule_id, source_unit=source, target_unit=target, factor=factor, offset=offset
    )


CONVERSION_RULES: tuple[ConversionRule, ...] = (
    # Temperature
    _rule("degF_to_degC", "degF", "degC", 5.0 / 9.0, -32.0 * 5.0 / 9.0),
    _rule("K_to_degC", "K", "degC", 1.0, -273.15),
    # Pressure (canonical kPa)
    _rule("bar_to_kPa", "bar", "kPa", 100.0),
    _rule("psi_to_kPa", "psi", "kPa", 6.894757),
    _rule("mbar_to_kPa", "mbar", "kPa", 0.1),
    _rule("Pa_to_kPa", "Pa", "kPa", 0.001),
    _rule("MPa_to_kPa", "MPa", "kPa", 1000.0),
    # Vibration (canonical mm/s)
    _rule("mm_s_to_mm_s", "mm_s", "mm_s", 1.0),
    _rule("in_s_to_mm_s", "in_s", "mm_s", 25.4),
    _rule("m_s2_to_mm_s", "m_s2", "mm_s", 1000.0),
    # Throughput
    _rule("units_min_to_units_min", "units_min", "units_min", 1.0),
    _rule("units_h_to_units_min", "units_h", "units_min", 1.0 / 60.0),
    # Mass
    _rule("g_to_kg", "g", "kg", 0.001),
    _rule("lb_to_kg", "lb", "kg", 0.45359237),
    _rule("t_to_kg", "t", "kg", 1000.0),
    # Length
    _rule("mm_to_m", "mm", "m", 0.001),
    _rule("cm_to_m", "cm", "m", 0.01),
    _rule("in_to_m", "in", "m", 0.0254),
    _rule("ft_to_m", "ft", "m", 0.3048),
    # Duration
    _rule("ms_to_s", "ms", "s", 0.001),
    _rule("min_to_s", "min", "s", 60.0),
    _rule("h_to_s", "h", "s", 3600.0),
    # Power / energy
    _rule("W_to_kW", "W", "kW", 0.001),
    _rule("MW_to_kW", "MW", "kW", 1000.0),
    _rule("J_to_kWh", "J", "kWh", 1.0 / 3_600_000.0),
    _rule("MWh_to_kWh", "MWh", "kWh", 1000.0),
    # Flow
    _rule("L_s_to_L_min", "L_s", "L_min", 60.0),
    _rule("m3_h_to_L_min", "m3_h", "L_min", 1000.0 / 60.0),
    _rule("gpm_to_L_min", "gpm", "L_min", 3.785411784),
    # Electrical
    _rule("mV_to_V", "mV", "V", 0.001),
    _rule("kV_to_V", "kV", "V", 1000.0),
    _rule("mA_to_A", "mA", "A", 0.001),
    _rule("kHz_to_Hz", "kHz", "Hz", 1000.0),
    _rule("rpm_to_Hz", "rpm", "Hz", 1.0 / 60.0),
    _rule("km_h_to_m_s", "km_h", "m_s", 1.0 / 3.6),
)

_RULES_BY_KEY: dict[tuple[str, str], ConversionRule] = {
    (rule.source_unit, rule.target_unit): rule for rule in CONVERSION_RULES
}

# Unit aliases: several spellings of the same physical unit.
UNIT_ALIASES: dict[str, str] = {
    "c": "degC",
    "degc": "degC",
    "°c": "degC",
    "celsius": "degC",
    "f": "degF",
    "degf": "degF",
    "°f": "degF",
    "fahrenheit": "degF",
    "kelvin": "K",
    "bar": "bar",
    "kpa": "kPa",
    "psi": "psi",
    "pa": "Pa",
    "mpa": "MPa",
    "mm/s": "mm_s",
    "mm_s": "mm_s",
    "in/s": "in_s",
    "g": "g",
    "kg": "kg",
    "lb": "lb",
    "lbs": "lb",
    "mm": "mm",
    "cm": "cm",
    "m": "m",
    "ms": "ms",
    "s": "s",
    "sec": "s",
    "min": "min",
    "h": "h",
    "hr": "h",
    "w": "W",
    "kw": "kW",
    "mw": "MW",
    "kwh": "kWh",
    "v": "V",
    "mv": "mV",
    "kv": "kV",
    "a": "A",
    "ma": "mA",
    "hz": "Hz",
    "khz": "kHz",
    "rpm": "rpm",
    "units/min": "units_min",
    "units_min": "units_min",
    "units/h": "units_h",
    "1": "1",
    "": "",
}


def normalize_unit_name(raw: str | None) -> str | None:
    """Return the canonical spelling of a unit, or None when there is no unit."""
    if raw is None:
        return None
    stripped = raw.strip()
    if stripped == "":
        return None
    return UNIT_ALIASES.get(stripped.lower(), stripped)


def find_rule(source_unit: str, target_unit: str) -> ConversionRule | None:
    if source_unit == target_unit:
        return ConversionRule(
            rule_id=f"identity_{source_unit}",
            source_unit=source_unit,
            target_unit=target_unit,
            factor=1.0,
        )
    return _RULES_BY_KEY.get((source_unit, target_unit))


def canonical_unit_for(quantity: str) -> str | None:
    return CANONICAL_UNITS.get(quantity)


def infer_quantity(unit: str) -> str | None:
    """Infer the physical quantity a canonical unit belongs to."""
    for quantity, canonical in CANONICAL_UNITS.items():
        if canonical == unit:
            return quantity
    return None


def target_unit_for(unit: str) -> str | None:
    """Return the canonical unit a source unit should be normalized to.

    A unit that is already canonical maps to itself. Otherwise the target is taken from the
    conversion rules, so a source spelling like ``degF`` resolves to ``degC``. A unit with no
    known target returns None, and the caller preserves it rather than guessing.
    """
    if unit in CANONICAL_UNITS.values():
        return unit
    for rule in CONVERSION_RULES:
        if rule.source_unit == unit:
            return rule.target_unit
    return None


__all__ = [
    "CANONICAL_UNITS",
    "CONVERSION_RULES",
    "ConversionRule",
    "canonical_unit_for",
    "find_rule",
    "infer_quantity",
    "normalize_unit_name",
]
