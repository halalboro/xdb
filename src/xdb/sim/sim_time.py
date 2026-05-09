from __future__ import annotations

import re
from decimal import Decimal

from xdb.errors import XdbError

SIM_TIME_UNITS = {
    "fs": Decimal("1e-15"),
    "ps": Decimal("1e-12"),
    "ns": Decimal("1e-9"),
    "us": Decimal("1e-6"),
    "ms": Decimal("1e-3"),
    "s": Decimal("1"),
}


def _match_sim_time(text: str) -> re.Match[str]:
    normalized = text.strip()
    match = re.match(r"^([0-9]+(?:\.[0-9]*)?)\s*([a-zA-Z]+)$", normalized)
    if not match:
        raise XdbError(f"unsupported simulation time format: {text!r}")
    return match


def parse_sim_time(text: str) -> Decimal:
    match = _match_sim_time(text)
    value = Decimal(match.group(1))
    unit = match.group(2).lower()
    if unit not in SIM_TIME_UNITS:
        raise XdbError(f"unsupported simulation time unit: {unit!r}")
    return value * SIM_TIME_UNITS[unit]


def parse_duration_tokens(tokens: list[str]) -> tuple[str, Decimal]:
    joined = " ".join(token.strip() for token in tokens if token.strip())
    if not joined:
        raise XdbError("missing duration")
    return joined, parse_sim_time(joined)


def duration_unit_from_tokens(tokens: list[str]) -> str:
    joined = " ".join(token.strip() for token in tokens if token.strip())
    unit = _match_sim_time(joined).group(2).lower()
    if unit not in SIM_TIME_UNITS:
        raise XdbError(f"unsupported simulation time unit: {unit!r}")
    return unit


def _format_decimal(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def format_sim_duration_tokens(seconds: Decimal, *, preferred_unit: str = "ns") -> list[str]:
    if seconds <= 0:
        raise XdbError("duration must be > 0")
    unit = preferred_unit.lower()
    if unit not in SIM_TIME_UNITS:
        unit = "ns"
    value = seconds / SIM_TIME_UNITS[unit]
    return [_format_decimal(value), unit]
