from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import csv
import io
import re
from typing import Any

from xdb.errors import XdbError


DEFAULT_SUMMARY_RESOURCES = [
    "clb_luts",
    "registers",
    "block_ram_tile",
    "uram",
    "dsp_slices",
]

RESOURCE_LABELS = {
    "clb_luts": "CLB LUTs",
    "registers": "Registers",
    "block_ram_tile": "Block RAM Tile",
    "uram": "URAM",
    "dsp_slices": "DSP Slices",
}

RESOURCE_ALIASES = {
    "Registers": "registers",
    "Register as Flip Flop": "register_as_flip_flop",
    "Register as Latch": "register_as_latch",
    "CLB LUTs": "clb_luts",
    "LUT as Logic": "lut_as_logic",
    "LUT as Memory": "lut_as_memory",
    "LUT as Distributed RAM": "lut_as_distributed_ram",
    "LUT as Shift Register": "lut_as_shift_register",
    "CLB Registers": "clb_registers",
    "Block RAM Tile": "block_ram_tile",
    "RAMB36E5": "ramb36",
    "RAMB18E5*": "ramb18",
    "RAMB18E2*": "ramb18",
    "URAM": "uram",
    "DSP Slices": "dsp_slices",
}

REPORT_ALIASES = {
    "shell": "reports/shell_utilization.rpt",
    "user": "reports/config_0/user_synthed_c0_0.rpt",
}

_METADATA_KEYS = {
    "Tool Version": "tool_version",
    "Date": "date",
    "Design": "design",
    "Device": "device",
    "Design State": "design_state",
}


@dataclass
class UtilizationRow:
    key: str
    label: str
    raw_label: str
    used: int | float | None
    fixed: int | float | None
    prohibited: int | float | None
    available: int | float | None
    util_percent: float | None
    section: str | None


def _resolve_existing_file(path: Path, description: str) -> Path:
    resolved = path.expanduser()
    if not resolved.is_file():
        raise XdbError(f"{description} not found: {path}")
    return resolved


def discover_utilization_report(path: str | Path, report: str | None = None) -> Path:
    """Resolve a Vivado utilization report from a file or build/package directory."""

    root = Path(path).expanduser()
    if report is None and root.is_file():
        return root
    if not root.exists():
        raise XdbError(f"path not found: {root}")
    if root.is_file():
        if report is not None:
            raise XdbError("--report can only be used when <path> is a directory")
        return root
    if not root.is_dir():
        raise XdbError(f"not a file or directory: {root}")

    if report:
        alias = REPORT_ALIASES.get(report)
        if alias is not None:
            return _resolve_existing_file(root / alias, f"report alias {report!r}")
        candidate = Path(report).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        return _resolve_existing_file(candidate, "report")

    candidates = [
        root / "reports" / "shell_utilization.rpt",
        root / "reports" / "utilization.rpt",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate

    reports_dir = root / "reports"
    if reports_dir.is_dir():
        utilization_reports = sorted(reports_dir.glob("*utilization*.rpt"))
        for candidate in utilization_reports:
            if candidate.is_file():
                return candidate
        user_report = root / REPORT_ALIASES["user"]
        if user_report.is_file():
            return user_report

    raise XdbError(f"no Vivado utilization report found under: {root}")


def _metadata_key_value(line: str) -> tuple[str, str] | None:
    match = re.match(r"^\|\s*([^:|]+?)\s*:\s*(.*?)\s*\|?\s*$", line)
    if not match:
        return None
    return match.group(1).strip(), match.group(2).strip()


def _section_title(line: str) -> str | None:
    match = re.match(r"^\s*\d+\.\s+(.+?)\s*$", line)
    if not match:
        return None
    return match.group(1).strip()


def _split_table_row(line: str) -> list[str] | None:
    stripped = line.rstrip("\n")
    if not stripped.lstrip().startswith("|"):
        return None
    body = stripped.strip()
    if not body.startswith("|") or not body.endswith("|"):
        return None
    return body[1:-1].split("|")


def _parse_number(value: str) -> int | float | None:
    text = value.strip().replace(",", "")
    if text in {"", "-", "N/A", "n/a"}:
        return None
    text = text.rstrip("%")
    try:
        if "." in text:
            return float(text)
        return int(text)
    except ValueError:
        return None


def _normalize_key(label: str) -> str:
    if label in RESOURCE_ALIASES:
        return RESOURCE_ALIASES[label]
    text = label.strip().lower()
    text = re.sub(r"\*+$", "", text)
    text = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    return text or "unknown"


def _column_index(header: list[str], name: str) -> int | None:
    lname = name.lower()
    for index, cell in enumerate(header):
        if cell.strip().lower() == lname:
            return index
    return None


def _cell(cells: list[str], index: int | None) -> str:
    if index is None or index >= len(cells):
        return ""
    return cells[index]


def parse_utilization_report(path: str | Path) -> dict[str, Any]:
    report_path = Path(path).expanduser()
    if not report_path.is_file():
        raise XdbError(f"utilization report not found: {report_path}")

    try:
        lines = report_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as e:
        raise XdbError(f"failed to read utilization report: {report_path}") from e

    metadata: dict[str, str] = {}
    rows: list[UtilizationRow] = []
    resources: dict[str, dict[str, Any]] = {}
    section: str | None = None
    header: list[str] | None = None
    indices: dict[str, int | None] = {}

    for line in lines:
        meta = _metadata_key_value(line)
        if meta is not None:
            key, value = meta
            normalized = _METADATA_KEYS.get(key)
            if normalized:
                metadata[normalized] = value

        title = _section_title(line)
        if title is not None:
            section = title
            header = None
            indices = {}
            continue

        if line.lstrip().startswith("+"):
            continue

        cells = _split_table_row(line)
        if cells is None:
            continue
        stripped_cells = [cell.strip() for cell in cells]
        lower_cells = {cell.lower() for cell in stripped_cells}
        if {"site type", "used", "available"}.issubset(lower_cells) and "util%" in lower_cells:
            header = stripped_cells
            indices = {
                "label": _column_index(header, "Site Type"),
                "used": _column_index(header, "Used"),
                "fixed": _column_index(header, "Fixed"),
                "prohibited": _column_index(header, "Prohibited"),
                "available": _column_index(header, "Available"),
                "util_percent": _column_index(header, "Util%"),
            }
            continue
        if header is None:
            continue

        label_index = indices.get("label")
        if label_index is None or label_index >= len(cells):
            continue
        raw_label = cells[label_index].rstrip()
        label = raw_label.strip()
        if not label or label.lower() == "site type":
            continue

        row = UtilizationRow(
            key=_normalize_key(label),
            label=label,
            raw_label=raw_label,
            used=_parse_number(_cell(cells, indices.get("used"))),
            fixed=_parse_number(_cell(cells, indices.get("fixed"))),
            prohibited=_parse_number(_cell(cells, indices.get("prohibited"))),
            available=_parse_number(_cell(cells, indices.get("available"))),
            util_percent=_parse_number(_cell(cells, indices.get("util_percent"))),
            section=section,
        )
        rows.append(row)
        resources.setdefault(row.key, asdict(row))

    if not rows:
        raise XdbError(f"no utilization table rows found in report: {report_path}")

    return {
        "path": str(report_path),
        "report": str(report_path),
        **metadata,
        "resources": resources,
        "rows": [asdict(row) for row in rows],
    }


def parse_utilization_path(path: str | Path, report: str | None = None) -> dict[str, Any]:
    return parse_utilization_report(discover_utilization_report(path, report=report))


def _resource_map(parsed: dict[str, Any]) -> dict[str, Any]:
    resources = parsed.get("resources")
    if isinstance(resources, dict):
        return resources
    return {}


def _resource_rows(
    parsed: dict[str, Any],
    resources: list[str] | None = None,
    *,
    all_rows: bool = False,
) -> list[dict[str, Any]]:
    if all_rows:
        return [row for row in list(parsed.get("rows") or []) if isinstance(row, dict)]
    resource_map = _resource_map(parsed)
    keys = resources or DEFAULT_SUMMARY_RESOURCES
    rows: list[dict[str, Any]] = []
    for key in keys:
        row = resource_map.get(key)
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _format_value(value: object) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def _format_table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [len(header) for header in headers]
    for row in rows:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(value))
    lines = ["  ".join(header.ljust(widths[index]) for index, header in enumerate(headers))]
    for row in rows:
        lines.append("  ".join(value.ljust(widths[index]) for index, value in enumerate(row)))
    return "\n".join(lines)


def format_utilization_table(
    parsed: dict[str, Any],
    resources: list[str] | None = None,
    *,
    all_rows: bool = False,
) -> str:
    rows = _resource_rows(parsed, resources, all_rows=all_rows)
    table_rows = [
        [
            str(row.get("label") or row.get("key") or "?"),
            _format_value(row.get("used")),
            _format_value(row.get("available")),
            _format_value(row.get("util_percent")),
        ]
        for row in rows
    ]
    lines = [f"report: {parsed.get('report') or parsed.get('path') or '?'}"]
    if parsed.get("design"):
        lines.append(f"design: {parsed['design']}")
    if parsed.get("device"):
        lines.append(f"device: {parsed['device']}")
    if parsed.get("design_state"):
        lines.append(f"state:  {parsed['design_state']}")
    lines.append("")
    if table_rows:
        lines.append(_format_table(["Resource", "Used", "Available", "Util%"], table_rows))
    else:
        lines.append("no requested resources found")
    return "\n".join(lines)


def _report_name(parsed: dict[str, Any], name: str | None = None) -> str:
    if name:
        return name
    path = str(parsed.get("path") or parsed.get("report") or "report")
    parent = Path(path).parent
    if parent.name == "reports":
        return parent.parent.name or Path(path).name
    return Path(path).stem or path


def format_utilization_comparison(
    parsed_reports: list[dict[str, Any]],
    names: list[str] | None = None,
    resources: list[str] | None = None,
) -> str:
    keys = resources or DEFAULT_SUMMARY_RESOURCES
    headers = ["Build", *[RESOURCE_LABELS.get(key, key) for key in keys]]
    table_rows: list[list[str]] = []
    for index, parsed in enumerate(parsed_reports):
        resource_map = _resource_map(parsed)
        row = [_report_name(parsed, None if names is None or index >= len(names) else names[index])]
        for key in keys:
            resource = resource_map.get(key)
            if not isinstance(resource, dict):
                row.append("-")
                continue
            used = _format_value(resource.get("used"))
            percent = _format_value(resource.get("util_percent"))
            row.append(f"{used} {percent}%")
        table_rows.append(row)
    return _format_table(headers, table_rows)


def format_utilization_csv(
    parsed_reports: list[dict[str, Any]],
    names: list[str] | None = None,
    resources: list[str] | None = None,
) -> str:
    keys = resources or DEFAULT_SUMMARY_RESOURCES
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["build", "resource", "used", "available", "util_percent"])
    for index, parsed in enumerate(parsed_reports):
        name = _report_name(parsed, None if names is None or index >= len(names) else names[index])
        resource_map = _resource_map(parsed)
        for key in keys:
            resource = resource_map.get(key)
            if not isinstance(resource, dict):
                continue
            writer.writerow(
                [
                    name,
                    key,
                    resource.get("used"),
                    resource.get("available"),
                    resource.get("util_percent"),
                ]
            )
    return out.getvalue().rstrip("\r\n")


def _numeric_delta(old: object, new: object) -> int | float | None:
    if not isinstance(old, (int, float)) or not isinstance(new, (int, float)):
        return None
    delta = new - old
    if isinstance(old, int) and isinstance(new, int):
        return int(delta)
    return float(delta)


def _relative_delta_percent(old: object, new: object) -> float | None:
    if not isinstance(old, (int, float)) or not isinstance(new, (int, float)):
        return None
    if old == 0:
        return None
    return float((new - old) / old * 100.0)


def _format_signed_number(value: object, *, suffix: str = "") -> str:
    if value is None:
        return "-"
    if not isinstance(value, (int, float)):
        return str(value)
    sign = "+" if value > 0 else ""
    if isinstance(value, float):
        return f"{sign}{value:.2f}{suffix}"
    return f"{sign}{value}{suffix}"


def utilization_compare_data(
    old: dict[str, Any],
    new: dict[str, Any],
    *,
    old_name: str = "old",
    new_name: str = "new",
    resources: list[str] | None = None,
) -> dict[str, Any]:
    keys = resources or DEFAULT_SUMMARY_RESOURCES
    old_resources = _resource_map(old)
    new_resources = _resource_map(new)
    rows: list[dict[str, Any]] = []
    for key in keys:
        old_resource = old_resources.get(key)
        new_resource = new_resources.get(key)
        if not isinstance(old_resource, dict) and not isinstance(new_resource, dict):
            continue
        label = RESOURCE_LABELS.get(key, key)
        if isinstance(new_resource, dict):
            label = str(new_resource.get("label") or label)
        elif isinstance(old_resource, dict):
            label = str(old_resource.get("label") or label)

        old_used = old_resource.get("used") if isinstance(old_resource, dict) else None
        new_used = new_resource.get("used") if isinstance(new_resource, dict) else None
        old_util = old_resource.get("util_percent") if isinstance(old_resource, dict) else None
        new_util = new_resource.get("util_percent") if isinstance(new_resource, dict) else None
        rows.append(
            {
                "resource": key,
                "label": label,
                "old_used": old_used,
                "new_used": new_used,
                "delta_used": _numeric_delta(old_used, new_used),
                "delta_used_percent": _relative_delta_percent(old_used, new_used),
                "old_util_percent": old_util,
                "new_util_percent": new_util,
                "delta_util_percent_points": _numeric_delta(old_util, new_util),
            }
        )
    return {
        "old": {"name": old_name, "report": old.get("report") or old.get("path")},
        "new": {"name": new_name, "report": new.get("report") or new.get("path")},
        "rows": rows,
    }


def format_utilization_delta_comparison(data: dict[str, Any]) -> str:
    raw_rows = data.get("rows")
    rows: list[Any] = raw_rows if isinstance(raw_rows, list) else []
    old_info_raw = data.get("old")
    new_info_raw = data.get("new")
    old_info: dict[str, Any] = old_info_raw if isinstance(old_info_raw, dict) else {}
    new_info: dict[str, Any] = new_info_raw if isinstance(new_info_raw, dict) else {}
    old_name = str(old_info.get("name") or "old")
    new_name = str(new_info.get("name") or "new")

    table_rows: list[list[str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        table_rows.append(
            [
                str(row.get("label") or row.get("resource") or "?"),
                _format_value(row.get("old_used")),
                _format_value(row.get("new_used")),
                _format_signed_number(row.get("delta_used")),
                _format_signed_number(row.get("delta_used_percent"), suffix="%"),
                f"{_format_value(row.get('old_util_percent'))}%",
                f"{_format_value(row.get('new_util_percent'))}%",
                _format_signed_number(row.get("delta_util_percent_points"), suffix=" pp"),
            ]
        )

    lines = [
        f"baseline: {old_name}",
        f"new:      {new_name}",
        "",
    ]
    if table_rows:
        lines.append(
            _format_table(
                [
                    "Resource",
                    f"{old_name} Used",
                    f"{new_name} Used",
                    "Δ Used",
                    "Δ Used%",
                    f"{old_name} Util",
                    f"{new_name} Util",
                    "Δ Util",
                ],
                table_rows,
            )
        )
    else:
        lines.append("no requested resources found")
    return "\n".join(lines)


def format_utilization_delta_csv(data: dict[str, Any]) -> str:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(
        [
            "resource",
            "old_used",
            "new_used",
            "delta_used",
            "delta_used_percent",
            "old_util_percent",
            "new_util_percent",
            "delta_util_percent_points",
        ]
    )
    for row in data.get("rows") or []:
        if not isinstance(row, dict):
            continue
        writer.writerow(
            [
                row.get("resource"),
                row.get("old_used"),
                row.get("new_used"),
                row.get("delta_used"),
                row.get("delta_used_percent"),
                row.get("old_util_percent"),
                row.get("new_util_percent"),
                row.get("delta_util_percent_points"),
            ]
        )
    return out.getvalue().rstrip("\r\n")
