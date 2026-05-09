from __future__ import annotations

from decimal import Decimal
from typing import Any

from ..errors import XdbError
from .sim_time import parse_duration_tokens, parse_sim_time


def _trace_wallclock(value: dict[str, Any]) -> float | None:
    raw = value.get("wallclock_seconds")
    if isinstance(raw, int | float):
        return float(raw)
    return None


def _trace_sim_time(value: dict[str, Any]) -> Decimal | None:
    raw = value.get("time")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return parse_sim_time(raw)
    except XdbError:
        return None


def _event_has_address(event: dict[str, Any]) -> bool:
    address_fields = ("addr", "addr_hex", "src_addr", "src_addr_hex", "dst_addr", "dst_addr_hex")
    return any(field in event for field in address_fields)


def _event_matches_correlation_mode(event: dict[str, Any], correlate_by: str) -> bool:
    if correlate_by == "nearest":
        return True
    if correlate_by == "opcode":
        return event.get("opcode") is not None
    if correlate_by == "addr":
        return _event_has_address(event)
    return True


def _transaction_label(event: dict[str, Any]) -> str:
    event_type = str(event.get("type") or "transaction")
    opcode = event.get("opcode")
    if opcode is not None:
        return f"{event_type}:{opcode}"
    addr_hex = event.get("addr_hex")
    if addr_hex is not None:
        return f"{event_type}@{addr_hex}"
    return event_type


def _axis_label(record: dict[str, Any]) -> str:
    interface = str(record.get("interface") or "axis")
    beat = record.get("beat_index")
    if beat is not None:
        return f"{interface} beat {beat}"
    return interface


def correlate_trace(
    transactions: dict[str, Any],
    axis: dict[str, Any],
    *,
    correlate_by: str = "nearest",
    window_tokens: list[str] | None = None,
) -> dict[str, Any]:
    if correlate_by not in {"nearest", "opcode", "addr"}:
        raise XdbError(f"unsupported correlation mode: {correlate_by}")
    window_text = None
    window_seconds = None
    if window_tokens:
        window_text, window_seconds = parse_duration_tokens(window_tokens)
    tx_events = [
        dict(item)
        for item in list(transactions.get("events") or [])
        if isinstance(item, dict)
    ]
    axis_records = [
        dict(item)
        for item in list(axis.get("records") or [])
        if isinstance(item, dict)
    ]
    timeline: list[dict[str, Any]] = []
    for index, event in enumerate(tx_events):
        timeline.append(
            {
                "kind": "transaction",
                "index": index,
                "label": _transaction_label(event),
                "time": event.get("time"),
                "wallclock_seconds": _trace_wallclock(event),
                "event": event,
            }
        )
    for index, record in enumerate(axis_records):
        timeline.append(
            {
                "kind": "axis",
                "index": index,
                "label": _axis_label(record),
                "time": record.get("time"),
                "wallclock_seconds": _trace_wallclock(record),
                "record": record,
            }
        )
    timeline.sort(
        key=lambda item: (
            item.get("wallclock_seconds") is None,
            float(item.get("wallclock_seconds") or 0.0),
            str(item.get("time") or ""),
            str(item.get("kind") or ""),
            int(item.get("index") or 0),
        )
    )

    axis_with_time: list[tuple[int, dict[str, Any], float, Decimal | None]] = []
    for index, record in enumerate(axis_records):
        wallclock = _trace_wallclock(record)
        if wallclock is not None:
            axis_with_time.append((index, record, wallclock, _trace_sim_time(record)))
    links: list[dict[str, Any]] = []
    skipped_by_mode = 0
    skipped_by_window = 0
    for tx_index, event in enumerate(tx_events):
        if not _event_matches_correlation_mode(event, correlate_by):
            skipped_by_mode += 1
            continue
        maybe_tx_wallclock = _trace_wallclock(event)
        if maybe_tx_wallclock is None or not axis_with_time:
            continue
        tx_wallclock = maybe_tx_wallclock
        tx_sim_time = _trace_sim_time(event)

        candidates: list[tuple[int, dict[str, Any], float, Decimal | None, Decimal | None]] = []
        for axis_index, axis_record, axis_wallclock, axis_sim_time in axis_with_time:
            delta_sim_seconds = None
            if tx_sim_time is not None and axis_sim_time is not None:
                delta_sim_seconds = axis_sim_time - tx_sim_time
                if window_seconds is not None and abs(delta_sim_seconds) > window_seconds:
                    continue
            candidates.append((axis_index, axis_record, axis_wallclock, axis_sim_time, delta_sim_seconds))
        if not candidates:
            skipped_by_window += 1
            continue

        axis_index, axis_record, axis_wallclock, _axis_sim_time, delta_sim_seconds = min(
            candidates,
            key=lambda item: (
                abs(item[4]) if item[4] is not None else Decimal("Infinity"),
                abs(item[2] - tx_wallclock),
            ),
        )
        link: dict[str, Any] = {
            "transaction_index": tx_index,
            "transaction_label": _transaction_label(event),
            "axis_index": axis_index,
            "axis_label": _axis_label(axis_record),
            "delta_wallclock_seconds": axis_wallclock - tx_wallclock,
            "transaction_time": event.get("time"),
            "axis_time": axis_record.get("time"),
            "correlate_by": correlate_by,
        }
        if delta_sim_seconds is not None:
            link["delta_sim_seconds"] = float(delta_sim_seconds)
        links.append(link)

    return {
        "transaction_count": len(tx_events),
        "axis_record_count": len(axis_records),
        "timeline_count": len(timeline),
        "link_count": len(links),
        "correlate_by": correlate_by,
        "window": window_text,
        "skipped_by_mode": skipped_by_mode,
        "skipped_by_window": skipped_by_window,
        "timeline": timeline,
        "links": links,
        "notes": [
            "correlation is ordered by collection wallclock when available",
            "nearest links prefer simulator-time proximity when both sides have simulator timestamps",
            "--correlate-by opcode only links transaction events with an opcode field",
            "--correlate-by addr only links transaction events with address fields",
            "AXIS records are sampled; handshakes shorter than the sampling step can still be missed",
        ],
    }
