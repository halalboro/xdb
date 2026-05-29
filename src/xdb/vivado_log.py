from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import re
import textwrap
from typing import Any, cast

_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_DIAGNOSTIC_RE = re.compile(
    r"^\s*(?:\*\*\s*(?:CERR|ERROR):\s*)?"
    r"(?P<severity>CRITICAL\s+WARNING|WARNING|ERROR|FATAL)\s*:\s*"
    r"(?P<message>.*)\s*$",
    re.IGNORECASE,
)
_CODE_RE = re.compile(r"^\s*\[(?P<code>[^\]]+)\]\s*(?P<body>.*)$")
_SUMMARY_COUNTS_RE = re.compile(
    r"(?P<infos>\d+)\s+Infos?,\s+"
    r"(?P<warnings>\d+)\s+Warnings?,\s+"
    r"(?P<critical_warnings>\d+)\s+Critical\s+Warnings?\s+and\s+"
    r"(?P<errors>\d+)\s+Errors?\s+encountered",
    re.IGNORECASE,
)
_COMMAND_FAILED_RE = re.compile(
    r"^\s*(?P<command>[A-Za-z_][A-Za-z0-9_:.+-]*)\s+failed\b(?P<detail>.*)$",
    re.IGNORECASE,
)
_ABNORMAL_TERMINATION_RE = re.compile(
    r"^\s*Abnormal program termination(?:\s*\((?P<signal>\d+)\))?\s*$",
    re.IGNORECASE,
)
_SEGFAULT_RE = re.compile(r"^\s*segfault in .*\bvivado\b.*", re.IGNORECASE)
_MAKE_ERROR_RE = re.compile(
    r"^\s*(?P<tool>(?:g?make|ninja)(?:\[\d+\])?):\s+\*\*\*\s+(?P<message>.*\bError\s+\d+.*)$",
    re.IGNORECASE,
)
_VIVADO_HEADER_RE = re.compile(
    r"Vivado v|Start of session|Exiting Vivado|Log file:|Journal file:",
    re.IGNORECASE,
)
_VIVADO_DIAGNOSTIC_MARKER_RE = re.compile(
    r"\b(?:INFO|WARNING|ERROR|CRITICAL WARNING):\s*\[[A-Za-z_]+\s+\d+-\d+\]",
    re.IGNORECASE,
)

_SEVERITY_NAMES = {
    "CRITICAL WARNING": "critical_warning",
    "WARNING": "warning",
    "ERROR": "error",
    "FATAL": "fatal",
}

_ROOT_CATEGORIES = {
    "application_exception",
    "black_box",
    "command_failed",
    "constraint_conflict",
    "hdl_compile",
    "missing_executable",
    "placement",
    "routing",
    "timing_not_met",
}


@dataclass(frozen=True)
class _Candidate:
    score: int
    line: int
    item: dict[str, Any]


def _clean_line(line: str) -> str:
    return _ANSI_RE.sub("", line).replace("\x0f", "").rstrip("\n\r")


def _normalize_severity(value: str) -> str:
    text = " ".join(value.upper().split())
    return _SEVERITY_NAMES.get(text, text.lower().replace(" ", "_"))


def _parse_code(message: str) -> tuple[str | None, str]:
    match = _CODE_RE.match(message)
    if not match:
        return None, message.strip()
    return match.group("code").strip(), match.group("body").strip()


def _category_for(code: str | None, message: str) -> str:
    lowered = message.lower()
    code_lower = (code or "").lower()

    if "not found in path" in lowered or "command not found" in lowered or "no such file or directory" in lowered:
        return "missing_executable"
    if "application exception" in lowered:
        return "application_exception"
    if "command failed" in lowered or re.search(r"\b\w+\s+failed\b", lowered):
        return "command_failed"
    if "did not meet timing" in lowered or "timing constraints are not met" in lowered:
        return "timing_not_met"
    if "cannot set loc" in lowered or "pad is already occupied" in lowered:
        return "constraint_conflict"
    if "constraints failed evaluation" in lowered:
        return "constraint_conflict"
    if "could not resolve non-primitive black box" in lowered or "black box" in lowered:
        return "black_box"
    if "not declared" in lowered or "undeclared" in lowered or code_lower.startswith("hdl "):
        return "hdl_compile"
    if "could not find module" in lowered and ".xdc" in lowered:
        return "ip_xdc_module_missing"
    if "not connected to a valid source" in lowered and "clk" in lowered:
        return "debug_clock"
    if "route" in code_lower or "routing" in lowered or "routable" in lowered:
        return "routing"
    if "place" in code_lower or "placement" in lowered:
        return "placement"
    if "license" in lowered or "licence" in lowered:
        return "license"
    return "vivado_diagnostic"


def _score_diagnostic(diag: dict[str, Any], line_count: int) -> int:
    severity = str(diag.get("severity") or "")
    category = str(diag.get("category") or "")
    line = int(diag.get("line") or 0)
    late_bonus = int(20 * (line / max(line_count, 1)))

    if severity == "fatal":
        return 120 + late_bonus
    if severity == "error":
        return 100 + late_bonus
    if severity == "critical_warning" and category in _ROOT_CATEGORIES:
        return 75 + late_bonus
    if severity == "critical_warning":
        return 45 + late_bonus
    if severity == "warning" and category in {"command_failed", "timing_not_met"}:
        return 35 + late_bonus
    return 0


def _score_failure(failure: dict[str, Any], line_count: int) -> int:
    line = int(failure.get("line") or 0)
    late_bonus = int(20 * (line / max(line_count, 1)))
    category = str(failure.get("category") or "")
    if category == "vivado_crash":
        return 120 + late_bonus
    if category == "build_system_error":
        return 95 + late_bonus
    return 90 + late_bonus


def _compact_count_map(counter: Counter[str]) -> dict[str, int]:
    return {key: counter[key] for key in sorted(counter)}


def summarize_vivado_log_text(text: str, *, source: str = "stdin") -> dict[str, Any]:
    """Summarize Vivado diagnostics in log text.

    This parser deliberately knows only about text/Vivado diagnostics.  It does
    not know about build systems, derivations, or where the text came from.
    """

    raw_lines = text.splitlines()
    lines = [_clean_line(line) for line in raw_lines]
    diagnostics: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    reported_counts: dict[str, int] | None = None

    for index, line in enumerate(lines, start=1):
        summary_match = _SUMMARY_COUNTS_RE.search(line)
        if summary_match:
            reported_counts = {
                key: int(value) for key, value in summary_match.groupdict().items()
            }

        diag_match = _DIAGNOSTIC_RE.match(line)
        if diag_match:
            severity = _normalize_severity(diag_match.group("severity"))
            raw_message = diag_match.group("message").strip()
            code, body = _parse_code(raw_message)
            diagnostics.append(
                {
                    "line": index,
                    "severity": severity,
                    "code": code,
                    "category": _category_for(code, body),
                    "message": body,
                    "raw": line.strip(),
                }
            )
            continue

        failed_match = _COMMAND_FAILED_RE.match(line)
        if failed_match:
            command = failed_match.group("command")
            detail = failed_match.group("detail").strip()
            message = f"{command} failed" + (f" {detail}" if detail else "")
            failures.append(
                {
                    "line": index,
                    "category": "command_failed",
                    "command": command,
                    "message": message,
                    "raw": line.strip(),
                }
            )
            continue

        abnormal_match = _ABNORMAL_TERMINATION_RE.match(line)
        if abnormal_match:
            signal = abnormal_match.group("signal")
            failures.append(
                {
                    "line": index,
                    "category": "vivado_crash",
                    "signal": int(signal) if signal is not None else None,
                    "message": line.strip(),
                    "raw": line.strip(),
                }
            )
            continue

        if _SEGFAULT_RE.match(line):
            failures.append(
                {
                    "line": index,
                    "category": "vivado_crash",
                    "message": line.strip(),
                    "raw": line.strip(),
                }
            )
            continue

        make_error_match = _MAKE_ERROR_RE.match(line)
        if make_error_match:
            failures.append(
                {
                    "line": index,
                    "category": "build_system_error",
                    "command": make_error_match.group("tool"),
                    "message": make_error_match.group("message").strip(),
                    "raw": line.strip(),
                }
            )

    vivado_header_count = sum(1 for line in lines if _VIVADO_HEADER_RE.search(line))
    vivado_diagnostic_marker_count = sum(
        1 for line in lines if _VIVADO_DIAGNOSTIC_MARKER_RE.search(line)
    )
    source_lower = source.lower()
    source_is_log_file = source_lower.endswith(".log") or ".log." in source_lower
    source_is_journal_file = source_lower.endswith(".jou") or ".jou." in source_lower
    looks_like_vivado_log = vivado_diagnostic_marker_count > 0 or (
        source_is_log_file and vivado_header_count > 0
    )
    input_warnings: list[str] = []
    if source_is_journal_file and not vivado_diagnostic_marker_count:
        input_warnings.append(
            "input looks like a Vivado journal, not a Vivado log; pass the corresponding vivado.log"
        )
    elif not looks_like_vivado_log:
        input_warnings.append(
            "input does not look like a Vivado log: no Vivado-style diagnostics found"
        )

    severity_counts = Counter(str(item["severity"]) for item in diagnostics)
    category_counts = Counter(str(item["category"]) for item in diagnostics)

    candidates: list[_Candidate] = []
    line_count = len(lines)
    for diag in diagnostics:
        score = _score_diagnostic(diag, line_count)
        if score > 0:
            candidates.append(_Candidate(score=score, line=int(diag["line"]), item=diag))
    for failure in failures:
        candidates.append(_Candidate(score=_score_failure(failure, line_count), line=int(failure["line"]), item=failure))

    candidates.sort(key=lambda item: (-item.score, -item.line))
    root_cause_candidates = [dict(candidate.item, score=candidate.score) for candidate in candidates]

    failed = bool(severity_counts.get("error") or severity_counts.get("fatal") or failures)
    if not looks_like_vivado_log:
        status = "unrecognized"
    elif failed:
        status = "failed"
    elif severity_counts.get("critical_warning"):
        status = "critical_warnings"
    elif severity_counts.get("warning"):
        status = "warnings"
    else:
        status = "ok"

    return {
        "source": source,
        "line_count": line_count,
        "status": status,
        "looks_like_vivado_log": looks_like_vivado_log,
        "input_warnings": input_warnings,
        "counts": {
            "errors": severity_counts.get("error", 0) + severity_counts.get("fatal", 0),
            "critical_warnings": severity_counts.get("critical_warning", 0),
            "warnings": severity_counts.get("warning", 0),
            "diagnostics": len(diagnostics),
            "failures": len(failures),
        },
        "reported_counts": reported_counts,
        "categories": _compact_count_map(category_counts),
        "diagnostics": diagnostics,
        "failures": failures,
        "root_cause_candidates": root_cause_candidates,
    }


def _shorten_middle(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    if max_len <= 5:
        return text[:max_len]
    keep_left = max_len // 2 - 1
    keep_right = max_len - keep_left - 3
    return f"{text[:keep_left]}...{text[-keep_right:]}"


def _shorten_hierarchy(text: str, *, max_len: int = 96) -> str:
    if len(text) <= max_len:
        return text
    parts = [part for part in text.split("/") if part]
    if len(parts) >= 4:
        shortened = f"{parts[0]}/.../{'/'.join(parts[-3:])}"
        if len(shortened) <= max_len:
            return shortened
    return _shorten_middle(text, max_len)


def _source_location(message: str) -> str | None:
    matches = re.findall(r"\[([^\[\]]+?:\d+)\]", message)
    return matches[-1] if matches else None


def _concise_constraint_conflict(message: str) -> str | None:
    loc_match = re.search(r"Cannot set LOC property of instance '([^']+)'", message)
    site_match = re.search(r"\bsite\s+([A-Za-z0-9_]+)", message)
    occupied_match = re.search(r"\boccupied by\s+(.+?)(?:\.\s+This could|\s+This could|$)", message)
    if loc_match and site_match:
        instance = _shorten_hierarchy(loc_match.group(1))
        site = site_match.group(1)
        location = _source_location(message)
        out = f"Cannot set LOC: {instance} -> {site}"
        if occupied_match:
            out += f"; occupied by {_shorten_hierarchy(occupied_match.group(1))}"
        if location:
            out += f" [{_shorten_hierarchy(location, max_len=80)}]"
        return out

    restore_match = re.search(
        r"Instance\s+(.+?)\s+was already placed at\s+(\S+),\s+restoration for site\s+(\S+)",
        message,
    )
    if restore_match:
        return (
            "Placement restore ignored: "
            f"{_shorten_hierarchy(restore_match.group(1))} already at {restore_match.group(2)}, "
            f"not {restore_match.group(3)}"
        )

    overwrite_match = re.search(
        r"Site\s+(\S+)\s+had a top level port\s+(\S+).*?overwritten by\s+(\S+)!?",
        message,
    )
    if overwrite_match:
        return (
            f"Site {overwrite_match.group(1)}: top-level port {overwrite_match.group(2)} "
            f"overwritten by {overwrite_match.group(3)}"
        )

    return None


def _format_message(item: dict[str, Any], *, verbose: bool = False) -> str:
    message = " ".join(str(item.get("message") or item.get("raw") or "").split())
    if verbose:
        return message
    category = str(item.get("category") or "")
    if category == "constraint_conflict":
        concise = _concise_constraint_conflict(message)
        if concise:
            return concise
    return _shorten_middle(message, 240)


def _format_item(item: dict[str, Any], *, verbose: bool = False) -> str:
    line = item.get("line", "?")
    category = item.get("category") or "unknown"
    severity = item.get("severity")
    code = item.get("code")
    message = _format_message(item, verbose=verbose)
    prefix = f"L{line}: "
    if severity:
        prefix += str(severity).replace("_", " ")
    else:
        prefix += str(category).replace("_", " ")
    if code:
        prefix += f" [{code}]"
    prefix += f" {category}: "
    return textwrap.fill(
        prefix + message,
        width=120,
        subsequent_indent="    ",
        break_long_words=False,
        break_on_hyphens=False,
    )


def format_vivado_log_summary(
    summary: dict[str, Any],
    *,
    max_items: int | None = 10,
    verbose: bool = False,
) -> str:
    if summary.get("looks_like_vivado_log") is False:
        return "not a Vivado log"

    counts_obj = summary.get("counts")
    counts = cast(dict[str, Any], counts_obj if isinstance(counts_obj, dict) else {})
    lines = [
        "vivado log summary",
        f"source: {summary.get('source', '?')}",
        f"status: {summary.get('status', '?')}",
        f"lines: {summary.get('line_count', 0)}",
        "diagnostics: "
        f"{counts.get('errors', 0)} error(s), "
        f"{counts.get('critical_warnings', 0)} critical warning(s), "
        f"{counts.get('warnings', 0)} warning(s), "
        f"{counts.get('failures', 0)} failed command marker(s)",
    ]

    reported = summary.get("reported_counts")
    if isinstance(reported, dict):
        lines.append(
            "reported by Vivado: "
            f"{reported.get('errors', 0)} error(s), "
            f"{reported.get('critical_warnings', 0)} critical warning(s), "
            f"{reported.get('warnings', 0)} warning(s), "
            f"{reported.get('infos', 0)} info(s)"
        )

    candidates = [item for item in list(summary.get("root_cause_candidates") or []) if isinstance(item, dict)]
    lines.append("root-cause candidates:")
    if candidates:
        shown_candidates = candidates if max_items is None else candidates[:max_items]
        for item in shown_candidates:
            lines.append(f"  {_format_item(item, verbose=verbose)}")
        if max_items is not None and len(candidates) > max_items:
            lines.append(f"  ... {len(candidates) - max_items} more")
    else:
        lines.append("  none")

    categories = summary.get("categories")
    if isinstance(categories, dict) and categories:
        lines.append("categories:")
        for category, count in sorted(categories.items(), key=lambda item: (-int(item[1]), item[0])):
            lines.append(f"  {category}: {count}")

    critical = [
        item
        for item in list(summary.get("diagnostics") or [])
        if isinstance(item, dict) and item.get("severity") == "critical_warning"
    ]
    if critical:
        lines.append("critical warnings:")
        shown_critical = critical if max_items is None else critical[:max_items]
        for item in shown_critical:
            lines.append(f"  {_format_item(item, verbose=verbose)}")
        if max_items is not None and len(critical) > max_items:
            lines.append(f"  ... {len(critical) - max_items} more")

    return "\n".join(lines)
