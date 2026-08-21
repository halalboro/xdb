from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast


def _json_text(data: dict) -> str:
    return json.dumps(data, indent=2, sort_keys=False)


def _emit_text(text: str, out_path: str | None = None) -> None:
    if out_path:
        Path(out_path).expanduser().write_text(text if text.endswith("\n") else f"{text}\n")
    else:
        print(text)


def _print(data: dict) -> None:
    _emit_text(_json_text(data))


def _emit_json(data: dict, out_path: str | None = None) -> None:
    _emit_text(_json_text(data), out_path)


def _format_with_trace_ndjson(result: dict) -> str:
    lines: list[str] = []
    window = {
        "kind": "window",
        "duration": result.get("duration"),
        "step": result.get("step"),
        "time_before": result.get("time_before"),
        "time_after": result.get("time_after"),
    }
    lines.append(json.dumps(window, sort_keys=False))

    action = result.get("action")
    if isinstance(action, dict):
        lines.append(json.dumps({"kind": "action", **action}, sort_keys=False))

    transactions = result.get("transactions")
    if isinstance(transactions, dict):
        for index, event in enumerate(list(transactions.get("events") or [])):
            if isinstance(event, dict):
                lines.append(
                    json.dumps({"kind": "transaction", "index": index, **event}, sort_keys=False)
                )

    axis = result.get("axis")
    if isinstance(axis, dict):
        for index, record in enumerate(list(axis.get("records") or [])):
            if isinstance(record, dict):
                lines.append(
                    json.dumps({"kind": "axis", "index": index, **record}, sort_keys=False)
                )

    correlation = result.get("correlation")
    if isinstance(correlation, dict):
        for index, item in enumerate(list(correlation.get("timeline") or [])):
            if isinstance(item, dict):
                lines.append(
                    json.dumps(
                        {"kind": "correlation", "index": index, "entry": item},
                        sort_keys=False,
                    )
                )
    return "\n".join(lines)


def _format_bool(value: object) -> str:
    if value is True:
        return "yes"
    if value is False:
        return "no"
    if value is None:
        return "n/a"
    return str(value)


def _format_doctor_summary(result: dict) -> str:
    checks = [check for check in list(result.get("checks") or []) if isinstance(check, dict)]
    errors = [
        check for check in checks if not check.get("ok", False) and check.get("severity") == "error"
    ]
    warnings = [
        check for check in checks if not check.get("ok", False) and check.get("severity") != "error"
    ]
    lines = [
        "doctor summary",
        f"ok: {_format_bool(result.get('ok'))}",
        f"session: {result.get('session', '?')} ({result.get('session_id', '?')})",
        f"anchor: {result.get('anchor_dir', '?')}",
        f"checks: {len(checks)} total, {len(errors)} error(s), {len(warnings)} warning(s)",
    ]
    if errors or warnings:
        lines.append("issues:")
        for check in [*errors, *warnings]:
            detail = str(check.get("detail") or "")
            suffix = f" - {detail}" if detail else ""
            lines.append(f"  {check.get('severity', 'error')}: {check.get('name', '?')}{suffix}")
    suggestions = [str(item) for item in list(result.get("suggestions") or []) if str(item)]
    if suggestions:
        lines.append("suggestions:")
        for suggestion in suggestions:
            lines.append(f"  {suggestion}")
    paths = result.get("paths") if isinstance(result.get("paths"), dict) else {}
    if paths:
        lines.append("paths:")
        for key in ("session_dir", "daemon_log", "vivado_log", "socket"):
            if paths.get(key):
                lines.append(f"  {key}: {paths[key]}")
    return "\n".join(lines)


def _format_provenance_summary(result: dict) -> str:
    requested = cast(
        dict[str, Any], result.get("requested") if isinstance(result.get("requested"), dict) else {}
    )
    live = cast(
        dict[str, Any],
        result.get("live_session") if isinstance(result.get("live_session"), dict) else {},
    )
    runtime = cast(
        dict[str, Any], result.get("runtime") if isinstance(result.get("runtime"), dict) else {}
    )
    comparisons = cast(
        dict[str, Any],
        result.get("comparisons") if isinstance(result.get("comparisons"), dict) else {},
    )
    lines = [
        "provenance summary",
        f"session: {result.get('session', '?')} ({result.get('session_id', '?')})",
        f"anchor: {result.get('anchor_dir', '?')}",
        f"requested: simset={requested.get('simset', '?')} mode={requested.get('mode', '?')} top={requested.get('top', '?')}",
        f"live: {_format_bool(live.get('present'))} state={live.get('state') or 'n/a'} pid={live.get('pid') or 'n/a'}",
        f"runtime available: {_format_bool(runtime.get('available'))}",
    ]
    if runtime.get("available"):
        lines.extend(
            [
                f"package_runtime: {runtime.get('package_runtime', '?')}",
                f"workspace: {runtime.get('workspace', '?')}",
                f"workspace exists: {_format_bool(runtime.get('workspace_exists'))}",
                f"needs stage: {_format_bool(runtime.get('needs_stage'))}",
                f"stage source matches package: {_format_bool(runtime.get('stage_source_matches_package'))}",
                f"stage fingerprint matches package: {_format_bool(runtime.get('stage_fingerprint_matches_package'))}",
                f"live session matches request: {_format_bool(comparisons.get('live_session_matches_request'))}",
            ]
        )
    elif runtime.get("error"):
        lines.append(f"runtime error: {runtime.get('error')}")
    return "\n".join(lines)


def _format_with_trace_summary(result: dict) -> str:
    action = result.get("action") if isinstance(result.get("action"), dict) else {}
    axis = result.get("axis") if isinstance(result.get("axis"), dict) else {}
    transactions = (
        result.get("transactions") if isinstance(result.get("transactions"), dict) else {}
    )
    correlation = result.get("correlation") if isinstance(result.get("correlation"), dict) else {}
    axis_records = list(axis.get("records") or []) if isinstance(axis, dict) else []
    tx_events = list(transactions.get("events") or []) if isinstance(transactions, dict) else []
    correlation_links = (
        list(correlation.get("links") or []) if isinstance(correlation, dict) else []
    )
    action_op = str(action.get("op") or "unknown") if isinstance(action, dict) else "unknown"

    lines = [
        "with-trace summary",
        f"window: {result.get('time_before', '?')} -> {result.get('time_after', '?')}",
        f"duration: {result.get('duration', '?')}  step: {result.get('step', '?')}",
        f"action: {action_op}",
        f"transactions: {len(tx_events)} event(s)",
        f"axis: {len(axis_records)} record(s)",
        f"correlation links: {len(correlation_links)}",
    ]
    if isinstance(correlation, dict) and correlation:
        lines.append(
            "correlation: "
            f"mode={correlation.get('correlate_by', 'nearest')} "
            f"window={correlation.get('window') or 'unbounded'} "
            f"skipped_by_mode={correlation.get('skipped_by_mode', 0)} "
            f"skipped_by_window={correlation.get('skipped_by_window', 0)}"
        )
    if tx_events:
        lines.append("transaction events:")
        for index, event in enumerate(tx_events[:10]):
            if isinstance(event, dict):
                event_type = str(event.get("type") or "event")
                opcode = event.get("opcode")
                time_text = event.get("time")
                suffix = ""
                if opcode is not None:
                    suffix += f" opcode={opcode}"
                if time_text is not None:
                    suffix += f" time={time_text}"
                lines.append(f"  {index}: {event_type}{suffix}")
        if len(tx_events) > 10:
            lines.append(f"  ... {len(tx_events) - 10} more")
    if axis_records:
        lines.append("axis records:")
        for index, record in enumerate(axis_records[:10]):
            if isinstance(record, dict):
                interface = record.get("interface", "?")
                time_text = record.get("time", "?")
                handshake = record.get("handshake", False)
                beat = record.get("beat_index")
                beat_text = "" if beat is None else f" beat={beat}"
                lines.append(
                    f"  {index}: {interface} time={time_text} handshake={handshake}{beat_text}"
                )
        if len(axis_records) > 10:
            lines.append(f"  ... {len(axis_records) - 10} more")
    if correlation_links:
        lines.append("correlation links:")
        for index, link in enumerate(correlation_links[:10]):
            if isinstance(link, dict):
                lines.append(
                    "  "
                    f"{index}: tx={link.get('transaction_label', '?')} "
                    f"axis={link.get('axis_label', '?')} "
                    f"delta={link.get('delta_wallclock_seconds', '?')}s"
                )
        if len(correlation_links) > 10:
            lines.append(f"  ... {len(correlation_links) - 10} more")
    return "\n".join(lines)
