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


def parse_sim_time(text: str) -> Decimal:
    normalized = text.strip()
    match = re.match(r"^([0-9]+(?:\.[0-9]*)?)\s*([a-zA-Z]+)$", normalized)
    if not match:
        raise XdbError(f"unsupported simulation time format: {text!r}")
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
