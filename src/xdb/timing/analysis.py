from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
import json
import re
from typing import Any, Iterable, Literal, cast

from xdb.backend.vivado import _extract_json, _run_vivado_tcl
from xdb.errors import XdbError
from xdb.reports.utilization import (
    discover_utilization_report,
    parse_utilization_report,
)

TimingCommand = Literal["summary", "paths", "clocks", "drc", "net", "triage", "compare"]


@dataclass
class TimingPath:
    slack: float | None
    status: str | None
    source: str | None
    destination: str | None
    path_group: str | None
    path_type: str | None
    start_clock: str | None
    end_clock: str | None


def _read_text(path: Path, description: str) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        raise XdbError(f"failed to read {description}: {path}") from e


def _existing_file(path: Path, description: str) -> Path:
    resolved = path.expanduser()
    if not resolved.is_file():
        raise XdbError(f"{description} not found: {path}")
    return resolved


def _existing_dir(path: Path, description: str) -> Path:
    resolved = path.expanduser()
    if not resolved.is_dir():
        raise XdbError(f"{description} not found: {path}")
    return resolved


def _parse_number(value: str) -> int | float | None:
    text = value.strip().replace(",", "")
    if text in {"", "-", "NA", "N/A", "n/a"}:
        return None
    text = text.rstrip("%")
    try:
        if "." in text:
            return float(text)
        return int(text)
    except ValueError:
        return None


def _as_float(value: object) -> float | None:
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        parsed = _parse_number(value)
        if isinstance(parsed, int | float):
            return float(parsed)
    return None


def _as_int(value: object) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        parsed = _parse_number(value)
        if isinstance(parsed, int | float):
            return int(parsed)
    return None


def _metadata_key_value(line: str) -> tuple[str, str] | None:
    match = re.match(r"^\|\s*([^:|]+?)\s*:\s*(.*?)\s*\|?\s*$", line)
    if not match:
        return None
    return match.group(1).strip(), match.group(2).strip()


_METADATA_KEYS = {
    "Tool Version": "tool_version",
    "Date": "date",
    "Host": "host",
    "Command": "command",
    "Design": "design",
    "Device": "device",
    "Speed File": "speed_file",
    "Design State": "design_state",
}


def discover_checkpoint(path: str | Path | None = None, dcp: str | Path | None = None) -> Path:
    """Resolve a routed DCP from --dcp, a DCP path, or a build/package output directory."""

    root = Path(path).expanduser() if path is not None else None
    if dcp is not None:
        candidate = Path(dcp).expanduser()
        if not candidate.is_absolute() and root is not None and root.is_dir():
            candidate = root / candidate
        return _existing_file(candidate, "checkpoint")

    if root is None:
        raise XdbError("missing checkpoint: pass --dcp or a build/package output directory")
    if root.is_file():
        if root.suffix.lower() != ".dcp":
            raise XdbError(f"not a DCP checkpoint: {root}")
        return root
    if not root.is_dir():
        raise XdbError(f"path not found: {root}")

    candidates = [
        root / "checkpoints" / "shell_routed.dcp",
        root / "checkpoints" / "routed.dcp",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate

    checkpoints = root / "checkpoints"
    if checkpoints.is_dir():
        routed = sorted(checkpoints.rglob("*routed*.dcp"))
        if routed:
            return routed[0]
        dcps = sorted(checkpoints.rglob("*.dcp"))
        if dcps:
            return dcps[0]

    raise XdbError(f"no Vivado checkpoint found under: {root}")


def discover_reports_dir(
    path: str | Path | None = None,
    reports: str | Path | None = None,
    dcp: str | Path | None = None,
) -> Path | None:
    if reports is not None:
        return _existing_dir(Path(reports), "reports directory")
    if path is not None:
        root = Path(path).expanduser()
        if root.is_dir() and (root / "reports").is_dir():
            return root / "reports"
        if root.is_dir() and any(root.glob("*.rpt")):
            return root
    if dcp is not None:
        checkpoint = Path(dcp).expanduser()
        candidate = checkpoint.parent.parent / "reports"
        if candidate.is_dir():
            return candidate
    return None


def discover_log(
    path: str | Path | None = None,
    log: str | Path | None = None,
    dcp: str | Path | None = None,
) -> Path | None:
    if log is not None:
        return _existing_file(Path(log), "Vivado log")
    roots: list[Path] = []
    if path is not None:
        roots.append(Path(path).expanduser())
    if dcp is not None:
        checkpoint = Path(dcp).expanduser()
        roots.extend([checkpoint.parent.parent, checkpoint.parent])
    for root in roots:
        if root.is_file():
            parent = root.parent
            for candidate in [parent / "vivado.log", parent / "logs" / "vivado.log"]:
                if candidate.is_file():
                    return candidate
        if root.is_dir():
            for candidate in [root / "logs" / "vivado.log", root / "vivado.log"]:
                if candidate.is_file():
                    return candidate
    return None


def discover_timing_summary_report(reports_dir: str | Path | None) -> Path | None:
    if reports_dir is None:
        return None
    root = Path(reports_dir).expanduser()
    candidates = [root / "shell_timing_summary.rpt", root / "timing_summary.rpt"]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    matches = sorted(root.glob("*timing_summary*.rpt"))
    return matches[0] if matches else None


def discover_drc_report(reports_dir: str | Path | None) -> Path | None:
    if reports_dir is None:
        return None
    root = Path(reports_dir).expanduser()
    candidates = [root / "shell_drc_bitstream_checks.rpt", root / "drc.rpt"]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    matches = sorted(root.glob("*drc*.rpt"))
    return matches[0] if matches else None


def parse_timing_summary_report(path: str | Path) -> dict[str, Any]:
    report = _existing_file(Path(path), "timing summary report")
    return parse_timing_summary_text(_read_text(report, "timing summary report"), source=str(report))


def _line_tokens(line: str) -> list[str]:
    return [token for token in line.strip().split() if token]


def _summary_from_tokens(tokens: list[str]) -> dict[str, Any] | None:
    if len(tokens) < 12:
        return None
    values = [_parse_number(token) for token in tokens[:12]]
    if not isinstance(values[0], int | float):
        return None
    return {
        "wns": _as_float(values[0]),
        "tns": _as_float(values[1]),
        "tns_failing_endpoints": _as_int(values[2]),
        "tns_total_endpoints": _as_int(values[3]),
        "whs": _as_float(values[4]),
        "ths": _as_float(values[5]),
        "ths_failing_endpoints": _as_int(values[6]),
        "ths_total_endpoints": _as_int(values[7]),
        "wpws": _as_float(values[8]),
        "tpws": _as_float(values[9]),
        "tpws_failing_endpoints": _as_int(values[10]),
        "tpws_total_endpoints": _as_int(values[11]),
    }


def _find_section(lines: list[str], title: str) -> tuple[int, int] | None:
    start = -1
    for index, line in enumerate(lines):
        if line.strip() == f"| {title}" or line.strip() == title:
            start = index
            break
    if start < 0:
        return None
    end = len(lines)
    for index in range(start + 1, len(lines)):
        stripped = lines[index].strip()
        if not stripped.startswith("| ") or stripped == f"| {title}":
            continue
        section_text = stripped[2:].strip()
        if section_text and set(section_text) <= {"-"}:
            continue
        end = index
        break
    return start, end


def _parse_design_summary(lines: list[str]) -> dict[str, Any]:
    section = _find_section(lines, "Design Timing Summary")
    search_lines = lines if section is None else lines[section[0]:section[1]]
    for index, line in enumerate(search_lines):
        if "WNS(ns)" not in line or "TNS(ns)" not in line:
            continue
        for data_line in search_lines[index + 1:index + 8]:
            tokens = _line_tokens(data_line)
            parsed = _summary_from_tokens(tokens)
            if parsed is not None:
                parsed["timing_met"] = not (
                    (_as_float(parsed.get("wns")) or 0.0) < 0.0
                    or (_as_float(parsed.get("whs")) or 0.0) < 0.0
                    or (_as_float(parsed.get("wpws")) or 0.0) < 0.0
                )
                return parsed
    return {}


def _parse_clock_summary(lines: list[str]) -> list[dict[str, Any]]:
    section = _find_section(lines, "Clock Summary")
    if section is None:
        return []
    clocks: list[dict[str, Any]] = []
    for line in lines[section[0]:section[1]]:
        match = re.match(r"^(\s*)(\S.*?)\s+\{([^}]+)\}\s+([-0-9.]+)\s+([-0-9.]+)\s*$", line)
        if not match:
            continue
        indent, name, waveform, period, frequency = match.groups()
        clocks.append(
            {
                "name": name.strip(),
                "depth": len(indent) // 2,
                "waveform": waveform.strip(),
                "period": _as_float(period),
                "frequency_mhz": _as_float(frequency),
            }
        )
    return clocks


def _parse_clock_pair_tables(lines: list[str]) -> list[dict[str, Any]]:
    pairs: list[dict[str, Any]] = []

    intra = _find_section(lines, "Intra Clock Table")
    if intra is not None:
        for line in lines[intra[0]:intra[1]]:
            tokens = _line_tokens(line)
            if len(tokens) < 13:
                continue
            values = [_parse_number(token) for token in tokens[-12:]]
            if not isinstance(values[0], int | float):
                continue
            clock = " ".join(tokens[:-12]).strip()
            pairs.append(
                {
                    "kind": "intra",
                    "from_clock": clock,
                    "to_clock": clock,
                    "wns": _as_float(values[0]),
                    "tns": _as_float(values[1]),
                    "tns_failing_endpoints": _as_int(values[2]),
                    "tns_total_endpoints": _as_int(values[3]),
                    "whs": _as_float(values[4]),
                    "ths": _as_float(values[5]),
                    "ths_failing_endpoints": _as_int(values[6]),
                    "ths_total_endpoints": _as_int(values[7]),
                }
            )

    inter = _find_section(lines, "Inter Clock Table")
    if inter is not None:
        for line in lines[inter[0]:inter[1]]:
            tokens = _line_tokens(line)
            if len(tokens) < 10:
                continue
            values = [_parse_number(token) for token in tokens[-8:]]
            if not isinstance(values[0], int | float):
                continue
            pairs.append(
                {
                    "kind": "inter",
                    "from_clock": tokens[0],
                    "to_clock": tokens[1],
                    "wns": _as_float(values[0]),
                    "tns": _as_float(values[1]),
                    "tns_failing_endpoints": _as_int(values[2]),
                    "tns_total_endpoints": _as_int(values[3]),
                    "whs": _as_float(values[4]),
                    "ths": _as_float(values[5]),
                    "ths_failing_endpoints": _as_int(values[6]),
                    "ths_total_endpoints": _as_int(values[7]),
                }
            )

    other = _find_section(lines, "Other Path Groups Table")
    if other is not None:
        for line in lines[other[0]:other[1]]:
            tokens = _line_tokens(line)
            if len(tokens) < 11:
                continue
            values = [_parse_number(token) for token in tokens[-8:]]
            if not isinstance(values[0], int | float):
                continue
            pairs.append(
                {
                    "kind": "other",
                    "path_group": tokens[0],
                    "from_clock": tokens[1],
                    "to_clock": tokens[2],
                    "wns": _as_float(values[0]),
                    "tns": _as_float(values[1]),
                    "tns_failing_endpoints": _as_int(values[2]),
                    "tns_total_endpoints": _as_int(values[3]),
                    "whs": _as_float(values[4]),
                    "ths": _as_float(values[5]),
                    "ths_failing_endpoints": _as_int(values[6]),
                    "ths_total_endpoints": _as_int(values[7]),
                }
            )
    return pairs


def _parse_check_timing(lines: list[str]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line in lines:
        match = re.match(r"\s*\d+\.\s+checking\s+([^()]+)\((\d+)\)", line)
        if not match:
            continue
        name = match.group(1).strip()
        if name in seen:
            continue
        seen.add(name)
        checks.append({"name": name, "count": int(match.group(2))})
    return checks


def parse_timing_summary_text(text: str, *, source: str | None = None) -> dict[str, Any]:
    lines = text.splitlines()
    metadata: dict[str, str] = {}
    for line in lines:
        meta = _metadata_key_value(line)
        if meta is None:
            continue
        key, value = meta
        normalized = _METADATA_KEYS.get(key)
        if normalized:
            metadata[normalized] = value

    summary = _parse_design_summary(lines)
    if "Timing constraints are not met" in text:
        summary["timing_met"] = False
    elif "Timing constraints are met" in text:
        summary["timing_met"] = True

    clock_pairs = _parse_clock_pair_tables(lines)
    failing_clock_pairs = [
        pair
        for pair in clock_pairs
        if (_as_float(pair.get("wns")) or 0.0) < 0.0 or (_as_float(pair.get("whs")) or 0.0) < 0.0
    ]
    failing_clock_pairs.sort(key=lambda p: _as_float(p.get("wns")) or 0.0)

    return {
        "source": source,
        "metadata": metadata,
        "summary": summary,
        "check_timing": _parse_check_timing(lines),
        "clocks": _parse_clock_summary(lines),
        "clock_pairs": clock_pairs,
        "failing_clock_pairs": failing_clock_pairs,
        "paths": parse_timing_paths_text(text),
    }


def parse_timing_paths_text(text: str) -> list[dict[str, Any]]:
    paths: list[TimingPath] = []
    current: dict[str, Any] | None = None

    for line in text.splitlines():
        slack_match = re.match(r"\s*Slack \(([^)]+)\)\s*:\s*([-+0-9.]+)ns", line)
        if slack_match:
            if current is not None:
                paths.append(_timing_path_from_dict(current))
            current = {"status": slack_match.group(1), "slack": float(slack_match.group(2))}
            continue
        if current is None:
            continue
        source_match = re.match(r"\s*Source:\s*(\S+)", line)
        if source_match:
            current["source"] = source_match.group(1)
            clock = _clock_from_detail(line)
            if clock:
                current["start_clock"] = clock
            continue
        dest_match = re.match(r"\s*Destination:\s*(\S+)", line)
        if dest_match:
            current["destination"] = dest_match.group(1)
            clock = _clock_from_detail(line)
            if clock:
                current["end_clock"] = clock
            continue
        if "clocked by" in line:
            clock = _clock_from_detail(line)
            if clock:
                if current.get("source") and not current.get("start_clock"):
                    current["start_clock"] = clock
                elif current.get("destination") and not current.get("end_clock"):
                    current["end_clock"] = clock
            continue
        group_match = re.match(r"\s*Path Group:\s*(\S+)", line)
        if group_match:
            current["path_group"] = group_match.group(1)
            continue
        type_match = re.match(r"\s*Path Type:\s*(.+?)\s*$", line)
        if type_match:
            current["path_type"] = type_match.group(1).strip()
            continue

    if current is not None:
        paths.append(_timing_path_from_dict(current))
    return [path_to_dict(path) for path in paths]


def _clock_from_detail(line: str) -> str | None:
    match = re.search(r"clocked by\s+(\S+)", line)
    if match:
        return match.group(1)
    return None


def _timing_path_from_dict(data: dict[str, Any]) -> TimingPath:
    return TimingPath(
        slack=_as_float(data.get("slack")),
        status=str(data.get("status")) if data.get("status") is not None else None,
        source=str(data.get("source")) if data.get("source") is not None else None,
        destination=str(data.get("destination")) if data.get("destination") is not None else None,
        path_group=str(data.get("path_group")) if data.get("path_group") is not None else None,
        path_type=str(data.get("path_type")) if data.get("path_type") is not None else None,
        start_clock=str(data.get("start_clock")) if data.get("start_clock") is not None else None,
        end_clock=str(data.get("end_clock")) if data.get("end_clock") is not None else None,
    )


def path_to_dict(path: TimingPath) -> dict[str, Any]:
    return {
        "slack": path.slack,
        "status": path.status,
        "source": path.source,
        "destination": path.destination,
        "path_group": path.path_group,
        "path_type": path.path_type,
        "start_clock": path.start_clock,
        "end_clock": path.end_clock,
    }


def hierarchy_prefix(name: str | None, *, depth: int = 4) -> str:
    if not name:
        return "<unknown>"
    parts = [part for part in name.split("/") if part]
    if not parts:
        return name
    return "/".join(parts[:depth])


def common_hierarchy_prefix(a: str | None, b: str | None, *, depth: int = 6) -> str:
    if not a or not b:
        return "<unknown>"
    left = [part for part in a.split("/") if part]
    right = [part for part in b.split("/") if part]
    common: list[str] = []
    for lpart, rpart in zip(left, right, strict=False):
        if lpart != rpart:
            break
        common.append(lpart)
        if len(common) >= depth:
            break
    return "/".join(common) if common else "<none>"


_BUCKET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("hbm", re.compile(r"hbm|HBM", re.IGNORECASE)),
    ("pcie", re.compile(r"pcie|PCIe|pcie4|pcie_", re.IGNORECASE)),
    ("xdma", re.compile(r"xdma", re.IGNORECASE)),
    ("qdma", re.compile(r"qdma", re.IGNORECASE)),
    ("debug", re.compile(r"debug|dbg|xsdbm|ila", re.IGNORECASE)),
    ("reset", re.compile(r"reset|rst", re.IGNORECASE)),
    ("clocking", re.compile(r"clk|clock|mmcm|pll|bufg", re.IGNORECASE)),
    ("user", re.compile(r"inst_dynamic|user|inst_user|helios", re.IGNORECASE)),
    ("shell", re.compile(r"inst_shell|shell", re.IGNORECASE)),
]


def classify_hierarchy(name: str | None) -> str:
    if not name:
        return "unknown"
    for label, pattern in _BUCKET_PATTERNS:
        if pattern.search(name):
            return label
    return "other"


def group_timing_paths(paths: Iterable[dict[str, Any]], *, depth: int = 4) -> dict[str, Any]:
    failing = [path for path in paths if (_as_float(path.get("slack")) or 0.0) < 0.0]
    by_start = Counter(hierarchy_prefix(cast(str | None, path.get("source")), depth=depth) for path in failing)
    by_end = Counter(hierarchy_prefix(cast(str | None, path.get("destination")), depth=depth) for path in failing)
    by_common = Counter(
        common_hierarchy_prefix(
            cast(str | None, path.get("source")),
            cast(str | None, path.get("destination")),
            depth=depth,
        )
        for path in failing
    )
    by_bucket = Counter(
        classify_hierarchy(
            " ".join(
                str(part)
                for part in [path.get("source"), path.get("destination")]
                if part
            )
        )
        for path in failing
    )
    return {
        "failing_path_count": len(failing),
        "by_startpoint": _counter_rows(by_start),
        "by_endpoint": _counter_rows(by_end),
        "by_common_ancestor": _counter_rows(by_common),
        "by_bucket": _counter_rows(by_bucket),
    }


def group_clock_pairs(paths: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for path in paths:
        if (_as_float(path.get("slack")) or 0.0) >= 0.0:
            continue
        key = (str(path.get("start_clock") or path.get("path_group") or "?"), str(path.get("end_clock") or path.get("path_group") or "?"))
        row = groups.setdefault(
            key,
            {
                "from_clock": key[0],
                "to_clock": key[1],
                "count": 0,
                "worst_slack": None,
                "representative_startpoint": None,
                "representative_endpoint": None,
            },
        )
        row["count"] += 1
        slack = _as_float(path.get("slack"))
        worst = _as_float(row.get("worst_slack"))
        if slack is not None and (worst is None or slack < worst):
            row["worst_slack"] = slack
            row["representative_startpoint"] = path.get("source")
            row["representative_endpoint"] = path.get("destination")
    return sorted(groups.values(), key=lambda row: (_as_float(row.get("worst_slack")) or 0.0, str(row.get("from_clock"))))


def _counter_rows(counter: Counter[str]) -> list[dict[str, Any]]:
    return [{"name": name, "count": count} for name, count in counter.most_common()]


_WARNING_PATTERNS: list[tuple[str, str, re.Pattern[str]]] = [
    ("route_bufg_route_through", "high", re.compile(r"Route 35-586|BUFG route-thru", re.IGNORECASE)),
    ("timing_not_met", "high", re.compile(r"Route 35-39|did not meet timing|timing constraints are not met", re.IGNORECASE)),
    ("clocking", "high", re.compile(r"BUFG|MMCM|PLL|clock region|clocking", re.IGNORECASE)),
    ("hbm", "medium", re.compile(r"\bHBM\b|hbm", re.IGNORECASE)),
    ("pcie_dma", "medium", re.compile(r"PCIe|XDMA|QDMA", re.IGNORECASE)),
    ("bitstream_drc", "medium", re.compile(r"bitstream|DRC|check_drc", re.IGNORECASE)),
    ("debug", "low", re.compile(r"debug hub|xsdbm|ILA", re.IGNORECASE)),
    ("bram_collision", "low", re.compile(r"WRITE_FIRST|READ_FIRST|collision|BRAM|RAMB", re.IGNORECASE)),
]


def parse_critical_warnings_text(text: str, *, source: str | None = None) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        category = "other"
        severity = "low"
        matched_pattern = False
        for candidate_category, candidate_severity, pattern in _WARNING_PATTERNS:
            if pattern.search(stripped):
                category = candidate_category
                severity = candidate_severity
                matched_pattern = True
                break
        warning_like = (
            "CRITICAL WARNING" in stripped
            or "WARNING" in stripped
            or "Timing constraints are not met" in stripped
        )
        if not warning_like:
            continue
        if not matched_pattern and "WARNING" not in stripped:
            continue
        row = grouped.setdefault(
            category,
            {"category": category, "severity": severity, "count": 0, "examples": []},
        )
        row["count"] += 1
        examples = cast(list[dict[str, Any]], row["examples"])
        if len(examples) < 5:
            examples.append({"source": source, "line": line_number, "text": stripped})
    return _sort_warning_groups(grouped.values())


def _sort_warning_groups(groups: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    severity_rank = {"high": 0, "medium": 1, "low": 2}
    return sorted(groups, key=lambda row: (severity_rank.get(str(row["severity"]), 9), str(row["category"])))


def merge_warning_groups(groups: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    severity_rank = {"high": 0, "medium": 1, "low": 2}
    for group in groups:
        category = str(group.get("category") or "other")
        severity = str(group.get("severity") or "low")
        row = merged.setdefault(category, {"category": category, "severity": severity, "count": 0, "examples": []})
        if severity_rank.get(severity, 9) < severity_rank.get(str(row.get("severity")), 9):
            row["severity"] = severity
        row["count"] = int(row.get("count") or 0) + int(group.get("count") or 0)
        examples = cast(list[Any], row.get("examples") or [])
        for example in cast(list[Any], group.get("examples") or []):
            if len(examples) >= 5:
                break
            examples.append(example)
        row["examples"] = examples
    return _sort_warning_groups(merged.values())


def parse_drc_report(path: str | Path) -> dict[str, Any]:
    report = _existing_file(Path(path), "DRC report")
    return parse_drc_text(_read_text(report, "DRC report"), source=str(report))


def parse_drc_text(text: str, *, source: str | None = None) -> dict[str, Any]:
    rules: list[dict[str, Any]] = []
    checks_found: int | None = None
    for line in text.splitlines():
        found = re.search(r"Checks found:\s*(\d+)", line)
        if found:
            checks_found = int(found.group(1))
        row = re.match(r"^\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*(\d+)\s*\|", line)
        if not row:
            continue
        rule, severity, description, count = [part.strip() for part in row.groups()]
        if rule.lower() == "rule":
            continue
        rules.append(
            {
                "rule": rule,
                "severity": severity,
                "description": description,
                "count": int(count),
            }
        )
    by_severity: dict[str, int] = defaultdict(int)
    for rule in rules:
        by_severity[str(rule["severity"])] += int(rule["count"])
    return {
        "source": source,
        "checks_found": checks_found if checks_found is not None else sum(int(rule["count"]) for rule in rules),
        "by_severity": dict(sorted(by_severity.items())),
        "rules": rules,
    }


def run_vivado_timing_summary(dcp: str | Path, *, max_paths: int = 10, timeout: int = 1800) -> dict[str, Any]:
    tcl = r'''
set dcp [lindex $argv 0]
set max_paths [lindex $argv 1]
open_checkpoint $dcp
set report [report_timing_summary -max_paths $max_paths -return_string]
puts "XDB_TEXT_BEGIN"
puts $report
puts "XDB_TEXT_END"
exit 0
'''
    result = _run_vivado_tcl(tcl, [str(dcp), str(max_paths)], timeout=timeout)
    text = _extract_text(result.stdout)
    return parse_timing_summary_text(text, source=str(dcp))


def run_vivado_timing_paths(
    dcp: str | Path,
    *,
    max_paths: int = 20,
    delay_type: str = "max",
    timeout: int = 1800,
) -> dict[str, Any]:
    tcl = r'''
set dcp [lindex $argv 0]
set max_paths [lindex $argv 1]
set delay_type [lindex $argv 2]
open_checkpoint $dcp
set report [report_timing -max_paths $max_paths -delay_type $delay_type -return_string]
puts "XDB_TEXT_BEGIN"
puts $report
puts "XDB_TEXT_END"
exit 0
'''
    result = _run_vivado_tcl(tcl, [str(dcp), str(max_paths), delay_type], timeout=timeout)
    text = _extract_text(result.stdout)
    paths = parse_timing_paths_text(text)
    return {
        "source": str(dcp),
        "delay_type": delay_type,
        "max_paths": max_paths,
        "paths": paths,
        "hierarchy": group_timing_paths(paths),
        "clock_pairs": group_clock_pairs(paths),
    }


def run_vivado_clocks(dcp: str | Path, *, timeout: int = 1800) -> dict[str, Any]:
    tcl = r'''
proc je {s} {
  return [string map {\\ \\\\ \" \\\" \n \\n \r \\r \t \\t} $s]
}
proc prop {obj name} {
  if {[catch {set v [get_property $name $obj]}]} { return "" }
  return $v
}
set dcp [lindex $argv 0]
open_checkpoint $dcp
set out "{\"source\":\"[je $dcp]\",\"clocks\":["
set first 1
foreach c [get_clocks] {
  if {!$first} { append out "," }
  set first 0
  set name [prop $c NAME]
  set period [prop $c PERIOD]
  set waveform [prop $c WAVEFORM]
  append out "{\"name\":\"[je $name]\",\"period\":\"[je $period]\",\"waveform\":\"[je $waveform]\"}"
}
append out "]}"
puts "XDB_JSON_BEGIN"
puts $out
puts "XDB_JSON_END"
exit 0
'''
    result = _run_vivado_tcl(tcl, [str(dcp)], timeout=timeout)
    data = _extract_json(result.stdout)
    for clock in data.get("clocks", []):
        if isinstance(clock, dict):
            clock["period"] = _as_float(clock.get("period"))
    return cast(dict[str, Any], data)


def run_vivado_drc(dcp: str | Path, *, timeout: int = 1800) -> dict[str, Any]:
    tcl = r'''
set dcp [lindex $argv 0]
open_checkpoint $dcp
set report [report_drc -return_string]
puts "XDB_TEXT_BEGIN"
puts $report
puts "XDB_TEXT_END"
exit 0
'''
    result = _run_vivado_tcl(tcl, [str(dcp)], timeout=timeout)
    text = _extract_text(result.stdout)
    return parse_drc_text(text, source=str(dcp))


def run_vivado_net_query(dcp: str | Path, net: str, *, timeout: int = 1800) -> dict[str, Any]:
    tcl = r'''
proc je {s} {
  return [string map {\\ \\\\ \" \\\" \n \\n \r \\r \t \\t} $s]
}
proc prop {obj name} {
  if {[catch {set v [get_property $name $obj]}]} { return "" }
  return $v
}
proc json_list {items} {
  set out "["
  set first 1
  foreach item $items {
    if {!$first} { append out "," }
    set first 0
    append out "\"[je $item]\""
  }
  append out "]"
  return $out
}
set dcp [lindex $argv 0]
set net_name [lindex $argv 1]
open_checkpoint $dcp
set nets [get_nets -quiet $net_name]
if {[llength $nets] == 0} {
  set nets [get_nets -quiet -hierarchical $net_name]
}
set exists [expr {[llength $nets] > 0}]
set out "{\"source\":\"[je $dcp]\",\"query\":\"[je $net_name]\",\"exists\":"
append out [expr {$exists ? "true" : "false"}]
if {$exists} {
  set n [lindex $nets 0]
  set pins [get_pins -quiet -of_objects $n]
  set drivers {}
  set loads {}
  foreach p $pins {
    set dir [prop $p DIRECTION]
    set pn [prop $p NAME]
    if {$dir eq "OUT"} { lappend drivers $pn } else { lappend loads $pn }
  }
  set clocks [get_clocks -quiet -of_objects $n]
  set clock_names {}
  foreach c $clocks { lappend clock_names [prop $c NAME] }
  append out ",\"name\":\"[je [prop $n NAME]]\""
  append out ",\"route_status\":\"[je [prop $n ROUTE_STATUS]]\""
  append out ",\"is_clock\":" [expr {[llength $clocks] > 0 ? "true" : "false"}]
  append out ",\"clock_names\":" [json_list $clock_names]
  append out ",\"drivers\":" [json_list $drivers]
  append out ",\"loads\":" [json_list $loads]
  append out ",\"load_count\":" [llength $loads]
}
append out "}"
puts "XDB_JSON_BEGIN"
puts $out
puts "XDB_JSON_END"
exit 0
'''
    result = _run_vivado_tcl(tcl, [str(dcp), net], timeout=timeout)
    return cast(dict[str, Any], _extract_json(result.stdout))


def _extract_text(stdout: str) -> str:
    start = "XDB_TEXT_BEGIN"
    end = "XDB_TEXT_END"
    i = stdout.find(start)
    j = stdout.find(end)
    if i == -1 or j == -1 or j <= i:
        raise XdbError(f"could not find text markers in Vivado output\n{stdout}")
    return stdout[i + len(start):j].strip()


def timing_summary(
    path: str | Path | None = None,
    *,
    dcp: str | Path | None = None,
    reports: str | Path | None = None,
    max_paths: int = 10,
    timeout: int = 1800,
) -> dict[str, Any]:
    reports_dir = discover_reports_dir(path, reports, dcp)
    report = discover_timing_summary_report(reports_dir)
    if report is not None:
        return parse_timing_summary_report(report)
    checkpoint = discover_checkpoint(path, dcp)
    return run_vivado_timing_summary(checkpoint, max_paths=max_paths, timeout=timeout)


def timing_paths(
    path: str | Path | None = None,
    *,
    dcp: str | Path | None = None,
    max_paths: int = 20,
    delay_type: str = "max",
    timeout: int = 1800,
) -> dict[str, Any]:
    checkpoint = discover_checkpoint(path, dcp)
    return run_vivado_timing_paths(
        checkpoint,
        max_paths=max_paths,
        delay_type=delay_type,
        timeout=timeout,
    )


def timing_clocks(
    path: str | Path | None = None,
    *,
    dcp: str | Path | None = None,
    reports: str | Path | None = None,
    timeout: int = 1800,
) -> dict[str, Any]:
    reports_dir = discover_reports_dir(path, reports, dcp)
    report = discover_timing_summary_report(reports_dir)
    if report is not None:
        parsed = parse_timing_summary_report(report)
        return {"source": parsed.get("source"), "clocks": parsed.get("clocks", [])}
    checkpoint = discover_checkpoint(path, dcp)
    return run_vivado_clocks(checkpoint, timeout=timeout)


def timing_drc(
    path: str | Path | None = None,
    *,
    dcp: str | Path | None = None,
    reports: str | Path | None = None,
    timeout: int = 1800,
) -> dict[str, Any]:
    reports_dir = discover_reports_dir(path, reports, dcp)
    report = discover_drc_report(reports_dir)
    if report is not None:
        return parse_drc_report(report)
    checkpoint = discover_checkpoint(path, dcp)
    return run_vivado_drc(checkpoint, timeout=timeout)


def timing_net(
    path: str | Path | None = None,
    *,
    dcp: str | Path | None = None,
    net: str,
    log: str | Path | None = None,
    reports: str | Path | None = None,
    timeout: int = 1800,
) -> dict[str, Any]:
    checkpoint = discover_checkpoint(path, dcp)
    result = run_vivado_net_query(checkpoint, net, timeout=timeout)
    log_path = discover_log(path, log, checkpoint)
    warnings: list[dict[str, Any]] = []
    if log_path is not None:
        warnings = [
            warning
            for warning in parse_critical_warnings_text(
                _read_text(log_path, "Vivado log"),
                source=str(log_path),
            )
            if net in json.dumps(warning)
        ]
    if not warnings:
        reports_dir = discover_reports_dir(path, reports, checkpoint)
        report = discover_timing_summary_report(reports_dir)
        if report is not None:
            warnings = [
                warning
                for warning in parse_critical_warnings_text(
                    _read_text(report, "timing summary report"),
                    source=str(report),
                )
                if net in json.dumps(warning)
            ]
    result["related_warnings"] = warnings
    return result


def timing_triage(
    path: str | Path | None = None,
    *,
    dcp: str | Path | None = None,
    reports: str | Path | None = None,
    log: str | Path | None = None,
    max_paths: int = 20,
    hierarchy_depth: int = 4,
    timeout: int = 1800,
) -> dict[str, Any]:
    summary_data = timing_summary(path, dcp=dcp, reports=reports, max_paths=max_paths, timeout=timeout)
    paths = list(summary_data.get("paths", []))
    if len(paths) < max_paths and dcp is not None:
        try:
            path_data = timing_paths(path, dcp=dcp, max_paths=max_paths, timeout=timeout)
            paths = list(path_data.get("paths", paths))
        except XdbError:
            pass

    reports_dir = discover_reports_dir(path, reports, dcp)
    drc_data: dict[str, Any] | None = None
    try:
        drc_data = timing_drc(path, dcp=dcp, reports=reports, timeout=timeout)
    except XdbError:
        drc_data = None

    log_path = discover_log(path, log, dcp)
    critical_warnings: list[dict[str, Any]] = []
    if log_path is not None:
        critical_warnings.extend(
            parse_critical_warnings_text(_read_text(log_path, "Vivado log"), source=str(log_path))
        )
    timing_report = discover_timing_summary_report(reports_dir)
    if timing_report is not None:
        critical_warnings.extend(
            parse_critical_warnings_text(
                _read_text(timing_report, "timing summary report"),
                source=str(timing_report),
            )
        )

    utilization: dict[str, Any] | None = None
    if path is not None or reports_dir is not None:
        try:
            util_root: str | Path = path if path is not None else cast(Path, reports_dir).parent
            utilization = parse_utilization_report(discover_utilization_report(util_root))
        except XdbError:
            utilization = None

    return {
        "source": summary_data.get("source"),
        "metadata": summary_data.get("metadata", {}),
        "summary": summary_data.get("summary", {}),
        "check_timing": summary_data.get("check_timing", []),
        "worst_paths": paths[:max_paths],
        "hierarchy": group_timing_paths(paths, depth=hierarchy_depth),
        "clock_pairs": summary_data.get("failing_clock_pairs") or group_clock_pairs(paths),
        "critical_warnings": merge_warning_groups(critical_warnings),
        "drc": drc_data,
        "utilization": _summarize_utilization(utilization),
    }


def _summarize_utilization(parsed: dict[str, Any] | None) -> dict[str, Any] | None:
    if parsed is None:
        return None
    resources = cast(dict[str, Any], parsed.get("resources") if isinstance(parsed.get("resources"), dict) else {})
    keys = ["clb_luts", "registers", "block_ram_tile", "uram", "dsp_slices"]
    return {
        "source": parsed.get("source"),
        "resources": {key: resources[key] for key in keys if key in resources},
    }


def timing_compare(
    old: str | Path,
    new: str | Path,
    *,
    old_name: str = "old",
    new_name: str = "new",
    hierarchy_depth: int = 4,
    timeout: int = 1800,
) -> dict[str, Any]:
    old_triage = timing_triage(old, hierarchy_depth=hierarchy_depth, timeout=timeout)
    new_triage = timing_triage(new, hierarchy_depth=hierarchy_depth, timeout=timeout)
    return compare_triage(old_triage, new_triage, old_name=old_name, new_name=new_name)


def compare_triage(
    old: dict[str, Any],
    new: dict[str, Any],
    *,
    old_name: str = "old",
    new_name: str = "new",
) -> dict[str, Any]:
    old_summary = cast(dict[str, Any], old.get("summary") if isinstance(old.get("summary"), dict) else {})
    new_summary = cast(dict[str, Any], new.get("summary") if isinstance(new.get("summary"), dict) else {})
    summary_delta: dict[str, Any] = {}
    for key in ["wns", "tns", "tns_failing_endpoints", "whs", "ths", "ths_failing_endpoints"]:
        old_value = _as_float(old_summary.get(key))
        new_value = _as_float(new_summary.get(key))
        if old_value is not None or new_value is not None:
            summary_delta[key] = {
                old_name: old_value,
                new_name: new_value,
                "delta": None if old_value is None or new_value is None else new_value - old_value,
            }

    old_pairs = _clock_pair_keys(cast(list[Any], old.get("clock_pairs") or []))
    new_pairs = _clock_pair_keys(cast(list[Any], new.get("clock_pairs") or []))
    old_hier = _hierarchy_names(old)
    new_hier = _hierarchy_names(new)
    old_warnings = _warning_categories(old)
    new_warnings = _warning_categories(new)

    return {
        "old": {"name": old_name, "source": old.get("source"), "summary": old_summary},
        "new": {"name": new_name, "source": new.get("source"), "summary": new_summary},
        "summary_delta": summary_delta,
        "clock_pairs": {
            "added": sorted(new_pairs - old_pairs),
            "removed": sorted(old_pairs - new_pairs),
            "common": sorted(old_pairs & new_pairs),
        },
        "hierarchy_buckets": {
            "added": sorted(new_hier - old_hier),
            "removed": sorted(old_hier - new_hier),
            "common": sorted(old_hier & new_hier),
        },
        "critical_warnings": {
            "added": sorted(new_warnings - old_warnings),
            "removed": sorted(old_warnings - new_warnings),
            "common": sorted(old_warnings & new_warnings),
        },
        "drc": _compare_drc(cast(dict[str, Any] | None, old.get("drc")), cast(dict[str, Any] | None, new.get("drc")), old_name, new_name),
    }


def _clock_pair_keys(pairs: list[Any]) -> set[str]:
    keys: set[str] = set()
    for pair in pairs:
        if not isinstance(pair, dict):
            continue
        keys.add(f"{pair.get('from_clock', '?')}->{pair.get('to_clock', '?')}")
    return keys


def _hierarchy_names(data: dict[str, Any]) -> set[str]:
    hierarchy = cast(dict[str, Any], data.get("hierarchy") if isinstance(data.get("hierarchy"), dict) else {})
    rows = cast(list[Any], hierarchy.get("by_common_ancestor") or [])
    return {str(row.get("name")) for row in rows if isinstance(row, dict) and row.get("name")}


def _warning_categories(data: dict[str, Any]) -> set[str]:
    warnings = cast(list[Any], data.get("critical_warnings") if isinstance(data.get("critical_warnings"), list) else [])
    return {str(row.get("category")) for row in warnings if isinstance(row, dict) and row.get("category")}


def _compare_drc(
    old: dict[str, Any] | None,
    new: dict[str, Any] | None,
    old_name: str,
    new_name: str,
) -> dict[str, Any] | None:
    if old is None and new is None:
        return None
    old_count = _as_int(old.get("checks_found")) if old else None
    new_count = _as_int(new.get("checks_found")) if new else None
    return {
        "checks_found": {
            old_name: old_count,
            new_name: new_count,
            "delta": None if old_count is None or new_count is None else new_count - old_count,
        },
        "severity": {
            old_name: old.get("by_severity", {}) if old else {},
            new_name: new.get("by_severity", {}) if new else {},
        },
    }


def _fmt_value(value: object, *, suffix: str = "") -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.3f}{suffix}"
    return f"{value}{suffix}"


def _summary_line(summary: dict[str, Any]) -> list[str]:
    return [
        f"WNS: {_fmt_value(summary.get('wns'), suffix=' ns')}",
        f"TNS: {_fmt_value(summary.get('tns'), suffix=' ns')}",
        f"failing endpoints: {_fmt_value(summary.get('tns_failing_endpoints'))}",
        f"WHS: {_fmt_value(summary.get('whs'), suffix=' ns')}",
        f"THS: {_fmt_value(summary.get('ths'), suffix=' ns')}",
    ]


def format_timing_summary(data: dict[str, Any]) -> str:
    summary = cast(dict[str, Any], data.get("summary") if isinstance(data.get("summary"), dict) else {})
    metadata = cast(dict[str, Any], data.get("metadata") if isinstance(data.get("metadata"), dict) else {})
    lines = ["Timing summary:"]
    if metadata.get("design") or metadata.get("device"):
        lines.append(f"  design: {metadata.get('design', 'n/a')}  device: {metadata.get('device', 'n/a')}")
    for line in _summary_line(summary):
        lines.append(f"  {line}")
    if summary.get("timing_met") is not None:
        lines.append(f"  timing met: {'yes' if summary.get('timing_met') else 'no'}")
    pairs = cast(list[Any], data.get("failing_clock_pairs") or [])
    if pairs:
        lines.append("")
        lines.append("Failing clock pairs:")
        lines.extend(_format_clock_pair_rows(pairs[:10]))
    return "\n".join(lines)


def _format_clock_pair_rows(pairs: list[Any]) -> list[str]:
    lines: list[str] = []
    for pair in pairs:
        if not isinstance(pair, dict):
            continue
        count = pair.get("tns_failing_endpoints", pair.get("count", "?"))
        worst = pair.get("wns", pair.get("worst_slack"))
        lines.append(
            f"  {pair.get('from_clock', '?')} -> {pair.get('to_clock', '?')}: "
            f"WNS {_fmt_value(worst, suffix=' ns')}, paths {count}"
        )
    return lines


def format_clock_pairs(pairs: list[dict[str, Any]]) -> str:
    return "\n".join(_format_clock_pair_rows(cast(list[Any], pairs)))


def format_timing_paths(data: dict[str, Any]) -> str:
    paths = [path for path in cast(list[Any], data.get("paths") or []) if isinstance(path, dict)]
    lines = [f"Timing paths ({len(paths)}):"]
    for index, path in enumerate(paths, start=1):
        lines.append(
            f"  {index}. slack {_fmt_value(path.get('slack'), suffix=' ns')} "
            f"{path.get('source', '?')} -> {path.get('destination', '?')}"
        )
        if path.get("path_group") or path.get("path_type"):
            lines.append(f"     group: {path.get('path_group', 'n/a')}  type: {path.get('path_type', 'n/a')}")
    hierarchy = data.get("hierarchy") if isinstance(data.get("hierarchy"), dict) else {}
    buckets = hierarchy.get("by_common_ancestor") if isinstance(hierarchy, dict) else []
    if buckets:
        lines.append("")
        lines.append("Failing paths by common hierarchy:")
        for row in cast(list[Any], buckets)[:10]:
            if isinstance(row, dict):
                lines.append(f"  {row.get('name', '?')}: {row.get('count', 0)}")
    return "\n".join(lines)


def format_timing_clocks(data: dict[str, Any]) -> str:
    clocks = [clock for clock in cast(list[Any], data.get("clocks") or []) if isinstance(clock, dict)]
    lines = [f"Clocks ({len(clocks)}):"]
    for clock in clocks:
        period = _fmt_value(clock.get("period"), suffix=" ns")
        frequency = _fmt_value(clock.get("frequency_mhz"), suffix=" MHz")
        lines.append(f"  {clock.get('name', '?')}: period {period}, frequency {frequency}")
    return "\n".join(lines)


def format_timing_drc(data: dict[str, Any]) -> str:
    lines = ["DRC summary:", f"  checks found: {_fmt_value(data.get('checks_found'))}"]
    by_severity = data.get("by_severity") if isinstance(data.get("by_severity"), dict) else {}
    if by_severity:
        lines.append("  by severity:")
        for severity, count in by_severity.items():
            lines.append(f"    {severity}: {count}")
    rules = [rule for rule in cast(list[Any], data.get("rules") or []) if isinstance(rule, dict)]
    if rules:
        lines.append("  rules:")
        for rule in rules[:20]:
            lines.append(
                f"    {rule.get('rule', '?')} {rule.get('severity', '?')} "
                f"{rule.get('description', '')}: {rule.get('count', 0)}"
            )
    return "\n".join(lines)


def format_timing_net(data: dict[str, Any]) -> str:
    lines = [f"Net query: {data.get('query', '?')}", f"  exists: {'yes' if data.get('exists') else 'no'}"]
    if data.get("exists"):
        lines.extend(
            [
                f"  name: {data.get('name', '?')}",
                f"  route status: {data.get('route_status') or 'n/a'}",
                f"  is clock: {'yes' if data.get('is_clock') else 'no'}",
                f"  clocks: {', '.join(str(x) for x in cast(list[Any], data.get('clock_names') or [])) or 'n/a'}",
                f"  load count: {data.get('load_count', 'n/a')}",
            ]
        )
        drivers = cast(list[Any], data.get("drivers") or [])
        if drivers:
            lines.append(f"  drivers: {', '.join(str(driver) for driver in drivers[:5])}")
    warnings = [warning for warning in cast(list[Any], data.get("related_warnings") or []) if isinstance(warning, dict)]
    if warnings:
        lines.append("  related warnings:")
        for warning in warnings:
            lines.append(f"    {warning.get('severity', '?')}: {warning.get('category', '?')} ({warning.get('count', 0)})")
    return "\n".join(lines)


def format_timing_triage(data: dict[str, Any]) -> str:
    summary = cast(dict[str, Any], data.get("summary") if isinstance(data.get("summary"), dict) else {})
    lines = ["Timing triage:"]
    for line in _summary_line(summary):
        lines.append(f"  {line}")
    hierarchy = data.get("hierarchy") if isinstance(data.get("hierarchy"), dict) else {}
    buckets = hierarchy.get("by_common_ancestor") if isinstance(hierarchy, dict) else []
    if buckets:
        lines.append("")
        lines.append("Failing paths by hierarchy:")
        for row in cast(list[Any], buckets)[:10]:
            if isinstance(row, dict):
                lines.append(f"  {row.get('name', '?'):<60} {row.get('count', 0)}")
    pairs = cast(list[Any], data.get("clock_pairs") or [])
    if pairs:
        lines.append("")
        lines.append("Failing paths by clock pair:")
        lines.extend(_format_clock_pair_rows(pairs[:10]))
    warnings = [warning for warning in cast(list[Any], data.get("critical_warnings") or []) if isinstance(warning, dict)]
    if warnings:
        lines.append("")
        lines.append("Critical warnings:")
        for warning in warnings[:10]:
            lines.append(f"  {warning.get('severity', '?')}: {warning.get('category', '?')} ({warning.get('count', 0)})")
            examples = [e for e in cast(list[Any], warning.get("examples") or []) if isinstance(e, dict)]
            if examples:
                lines.append(f"    {examples[0].get('text', '')}")
    drc = data.get("drc") if isinstance(data.get("drc"), dict) else None
    if drc is not None:
        lines.append("")
        lines.append(f"DRC checks found: {drc.get('checks_found', 'n/a')}")
    return "\n".join(lines)


def format_timing_compare(data: dict[str, Any]) -> str:
    old = cast(dict[str, Any], data.get("old") if isinstance(data.get("old"), dict) else {})
    new = cast(dict[str, Any], data.get("new") if isinstance(data.get("new"), dict) else {})
    lines = [f"Timing compare: {old.get('name', 'old')} -> {new.get('name', 'new')}"]
    delta = data.get("summary_delta") if isinstance(data.get("summary_delta"), dict) else {}
    if delta:
        lines.append("Summary deltas:")
        for key, row in delta.items():
            if isinstance(row, dict):
                lines.append(f"  {key}: {row.get('delta', 'n/a')} ({row})")
    for section_name, title in [
        ("clock_pairs", "Clock pairs"),
        ("hierarchy_buckets", "Hierarchy buckets"),
        ("critical_warnings", "Critical warnings"),
    ]:
        section = cast(dict[str, Any], data.get(section_name) if isinstance(data.get(section_name), dict) else {})
        added = cast(list[Any], section.get("added") or [])
        removed = cast(list[Any], section.get("removed") or [])
        if added or removed:
            lines.append(f"{title}:")
            if added:
                lines.append(f"  added: {', '.join(str(x) for x in added)}")
            if removed:
                lines.append(f"  removed: {', '.join(str(x) for x in removed)}")
    drc = data.get("drc") if isinstance(data.get("drc"), dict) else None
    if drc is not None:
        lines.append(f"DRC: {drc.get('checks_found')}")
    return "\n".join(lines)
