from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
import colorsys
import hashlib
import json
import os
import re
import stat
import tempfile
from typing import Any
from xml.sax.saxutils import escape as xml_escape
from xml.sax.saxutils import quoteattr

from xdb.backend.vivado import _run_vivado_tcl
from xdb.errors import XdbError


_SCHEMA = "xdb-floorplan-render-v1"
_RECORD_SCHEMA = "xdb-floorplan-records-v1"
_RECORD_BEGIN = "XDB_FLOORPLAN_BEGIN"
_RECORD_END = "XDB_FLOORPLAN_END"
_INVALID_XML_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_REQUIRED_METADATA = {"schema", "design", "device", "tool_version"}
_REQUIRED_STATS = {
    "primitive_cells",
    "placed_cells",
    "unplaced_cells",
    "sites_with_coordinates",
    "sites_without_coordinates",
    "routing_errors",
}

_CHECKPOINT_CANDIDATES = (
    "checkpoints/shell_routed.dcp",
    "checkpoints/shell_routed_locked.dcp",
    "checkpoints/config_0/shell_routed_c0.dcp",
    "checkpoints/static_routed_locked.dcp",
    "checkpoints/routed.dcp",
    "shell_routed.dcp",
    "routed.dcp",
)

_RESOURCE_ORDER = (
    "logic",
    "bram",
    "uram",
    "dsp",
    "io",
    "transceiver",
    "clock",
    "hard",
    "other",
)
_RESOURCE_LABELS = {
    "logic": "CLB / logic",
    "bram": "block RAM",
    "uram": "UltraRAM",
    "dsp": "DSP",
    "io": "I/O",
    "transceiver": "transceiver",
    "clock": "clocking",
    "hard": "hard IP / NoC",
    "other": "other placed site",
}
_RESOURCE_COLORS = {
    "logic": "#e2e8f0",
    "bram": "#bfdbfe",
    "uram": "#ddd6fe",
    "dsp": "#fecaca",
    "io": "#bbf7d0",
    "transceiver": "#fde68a",
    "clock": "#e5e7eb",
    "hard": "#cbd5e1",
    "other": "#f1f5f9",
}
_GROUP_COLORS = (
    "#0072b2",
    "#d55e00",
    "#009e73",
    "#cc79a7",
    "#e69f00",
    "#56b4e9",
    "#6f4e7c",
    "#8c564b",
    "#2f4b7c",
    "#b03a2e",
    "#1b7f79",
    "#7a5195",
    "#4e79a7",
    "#f28e2b",
    "#e15759",
    "#76b7b2",
    "#59a14f",
    "#edc948",
    "#b07aa1",
    "#ff9da7",
)
_MAX_PATH_RECTANGLES = 1024
_MIN_DOCUMENT_WIDTH = 1100.0
_MARK_SIZE = {
    "logic": (1.25, 1.25),
    "bram": (2.2, 3.4),
    "uram": (2.5, 4.2),
    "dsp": (2.2, 3.2),
    "io": (2.0, 2.0),
    "transceiver": (3.2, 5.0),
    "clock": (1.8, 1.8),
    "hard": (4.0, 5.5),
    "other": (1.8, 1.8),
}


_VIVADO_FLOORPLAN_TCL = r"""
proc xdb_field {value} {
  return [string map [list "\t" " " "\n" " " "\r" " "] $value]
}
proc xdb_prop {object name} {
  if {[catch {set value [get_property $name $object]}]} { return "" }
  return $value
}
proc xdb_group {name depth} {
  set parts [split $name "/"]
  if {[llength $parts] <= 1} { return "<top>" }
  return [join [lrange $parts 0 [expr {$depth - 1}]] "/"]
}

set dcp [lindex $argv 0]
set output [lindex $argv 1]
set hierarchy_depth [lindex $argv 2]
if {![string is integer -strict $hierarchy_depth] || $hierarchy_depth < 1} {
  error "hierarchy depth must be a positive integer"
}

open_checkpoint $dcp
set stream [open $output "w"]
fconfigure $stream -encoding utf-8 -translation lf
puts $stream "XDB_FLOORPLAN_BEGIN"
puts $stream "META\tschema\txdb-floorplan-records-v1"
puts $stream "META\tdesign\t[xdb_field [current_design]]"
puts $stream "META\tdevice\t[xdb_field [xdb_prop [current_design] PART]]"
puts $stream "META\ttool_version\t[xdb_field [version -short]]"

array set occupancy {}
set primitive_count 0
set placed_count 0
set unplaced_count 0
set cells [get_cells -hierarchical -quiet -filter {IS_PRIMITIVE == 1}]
set cell_names [get_property NAME $cells]
set cell_locs [get_property LOC $cells]
foreach cell $cells name $cell_names loc $cell_locs {
  incr primitive_count
  if {$loc eq ""} {
    incr unplaced_count
    continue
  }
  incr placed_count
  set group [xdb_group $name $hierarchy_depth]
  set key "$loc\u001f$group"
  if {[info exists occupancy($key)]} {
    incr occupancy($key)
  } else {
    set occupancy($key) 1
  }
}

set sites [get_sites -quiet]
set site_names [get_property NAME $sites]
set site_types [get_property SITE_TYPE $sites]
set site_xs [get_property RPM_X $sites]
set site_ys [get_property RPM_Y $sites]
set coordinate_count 0
set missing_coordinate_count 0
foreach site $sites name $site_names type $site_types x $site_xs y $site_ys {
  if {$x eq "" || $y eq ""} {
    incr missing_coordinate_count
    continue
  }
  incr coordinate_count
  puts $stream "SITE\t[xdb_field $name]\t[xdb_field $type]\t$x\t$y"
}

foreach key [lsort [array names occupancy]] {
  set fields [split $key "\u001f"]
  set site [lindex $fields 0]
  set group [join [lrange $fields 1 end] "\u001f"]
  puts $stream "OCC\t[xdb_field $site]\t[xdb_field $group]\t$occupancy($key)"
}

set route_report [report_route_status -return_string]
if {![regexp -nocase {# of nets with routing errors[^0-9]*([0-9,]+)} $route_report _ routing_errors]} {
  error "could not determine routing-error count from report_route_status"
}
set routing_errors [string map {, ""} $routing_errors]

foreach pblock [lsort [get_pblocks -quiet]] {
  set name [xdb_prop $pblock NAME]
  set ranges [xdb_prop $pblock GRID_RANGES]
  puts $stream "PBLOCK\t[xdb_field $name]\t[xdb_field $ranges]"
}
puts $stream "STAT\tprimitive_cells\t$primitive_count"
puts $stream "STAT\tplaced_cells\t$placed_count"
puts $stream "STAT\tunplaced_cells\t$unplaced_count"
puts $stream "STAT\tsites_with_coordinates\t$coordinate_count"
puts $stream "STAT\tsites_without_coordinates\t$missing_coordinate_count"
puts $stream "STAT\trouting_errors\t$routing_errors"
puts $stream "XDB_FLOORPLAN_END"
close $stream
close_design
exit 0
"""


@dataclass(frozen=True)
class FloorplanSite:
    name: str
    site_type: str
    x: int
    y: int
    resource: str


@dataclass(frozen=True)
class FloorplanOccupancy:
    site: str
    group: str
    cells: int


@dataclass(frozen=True)
class FloorplanPblock:
    name: str
    ranges: tuple[str, ...]


@dataclass(frozen=True)
class FloorplanDesign:
    source: Path
    design: str | None
    device: str | None
    tool_version: str | None
    sites: dict[str, FloorplanSite]
    occupancy: tuple[FloorplanOccupancy, ...]
    pblocks: tuple[FloorplanPblock, ...]
    stats: dict[str, int]


def _existing_checkpoint(path: Path, description: str = "routed checkpoint") -> Path:
    checkpoint = path.expanduser()
    if not checkpoint.is_file():
        raise XdbError(f"{description} not found: {path}")
    if checkpoint.suffix.lower() != ".dcp":
        raise XdbError(f"expected a Vivado DCP checkpoint: {checkpoint}")
    return checkpoint


def discover_floorplan_checkpoint(
    path: str | Path,
    *,
    dcp: str | Path | None = None,
) -> Path:
    selected = Path(path).expanduser()
    if not selected.exists():
        raise XdbError(f"path not found: {selected}")

    if selected.is_file():
        if dcp is not None:
            raise XdbError("--dcp can only be used when <path> is a directory")
        return _existing_checkpoint(selected)
    if not selected.is_dir():
        raise XdbError(f"not a file or directory: {selected}")

    if dcp is not None:
        checkpoint = Path(dcp).expanduser()
        if not checkpoint.is_absolute():
            checkpoint = selected / checkpoint
        return _existing_checkpoint(checkpoint)

    for relative in _CHECKPOINT_CANDIDATES:
        candidate = selected / relative
        if candidate.is_file():
            return candidate

    matches = sorted(
        candidate
        for candidate in selected.rglob("*routed*.dcp")
        if candidate.is_file() and "unrouted" not in candidate.name.lower()
    )
    if not matches:
        raise XdbError(f"no routed Vivado DCP checkpoint found under: {selected}")
    if len(matches) > 1:
        listed = ", ".join(str(item.relative_to(selected)) for item in matches[:5])
        suffix = " ..." if len(matches) > 5 else ""
        raise XdbError(
            f"multiple routed checkpoints found; select one with --dcp: {listed}{suffix}"
        )
    return matches[0]


def classify_site_resource(name: str, site_type: str) -> str:
    identity = f"{name} {site_type}".upper()
    if name.upper().startswith("SLICE_") or site_type.upper().startswith(("SLICE", "CLE")):
        return "logic"
    if "RAMB" in identity or "BRAM" in identity:
        return "bram"
    if "URAM" in identity:
        return "uram"
    if re.search(r"(?:^|[ _])DSP(?:48|58|_)", identity):
        return "dsp"
    if re.search(r"(?:^|[ _])(?:GTY|GTH|GTM|GTYP|GTME|GTF|GTP)", identity):
        return "transceiver"
    if re.search(r"(?:^|[ _])(?:HPIO|XPIO|HDIO|IOB|BITSLICE)", identity):
        return "io"
    if re.search(r"(?:^|[ _])(?:BUFG|BUFH|MMCM|PLL|DPLL|XPLL)", identity):
        return "clock"
    if re.search(
        r"(?:^|[ _])(?:PCIE|CMAC|MRMAC|DCMAC|ILKN|HBM|NOC|DDRMC|CIPS|AIE|SYSMON|CONFIG)",
        identity,
    ):
        return "hard"
    return "other"


def _record_payload(text: str, source: Path) -> str:
    start = text.find(_RECORD_BEGIN)
    finish = text.find(_RECORD_END)
    if start < 0 or finish < 0 or finish <= start:
        raise XdbError(f"invalid Vivado floorplan records for {source}: missing record markers")
    return text[start + len(_RECORD_BEGIN) : finish]


def parse_floorplan_records(text: str, source: str | Path) -> FloorplanDesign:
    checkpoint = Path(source)
    metadata: dict[str, str] = {}
    sites: dict[str, FloorplanSite] = {}
    occupancy_counts: Counter[tuple[str, str]] = Counter()
    pblocks: list[FloorplanPblock] = []
    pblock_names: set[str] = set()
    stats: dict[str, int] = {}

    for line_number, raw_line in enumerate(_record_payload(text, checkpoint).splitlines(), start=1):
        line = raw_line.rstrip("\r")
        if not line:
            continue
        fields = line.split("\t")
        kind = fields[0]
        try:
            if kind == "META" and len(fields) == 3:
                if fields[1] in metadata:
                    raise ValueError(f"duplicate metadata field {fields[1]!r}")
                metadata[fields[1]] = fields[2]
            elif kind == "SITE" and len(fields) == 5:
                name, site_type = fields[1], fields[2]
                site = FloorplanSite(
                    name=name,
                    site_type=site_type,
                    x=int(fields[3]),
                    y=int(fields[4]),
                    resource=classify_site_resource(name, site_type),
                )
                if name in sites:
                    raise ValueError(f"duplicate site {name!r}")
                sites[name] = site
            elif kind == "OCC" and len(fields) == 4:
                count = int(fields[3])
                if count <= 0:
                    raise ValueError("occupancy count must be positive")
                key = (fields[1], fields[2])
                if key in occupancy_counts:
                    raise ValueError(f"duplicate occupancy record for {key!r}")
                occupancy_counts[key] = count
            elif kind == "PBLOCK" and len(fields) == 3:
                if fields[1] in pblock_names:
                    raise ValueError(f"duplicate pblock {fields[1]!r}")
                ranges = tuple(item for item in fields[2].split() if item)
                pblocks.append(FloorplanPblock(name=fields[1], ranges=ranges))
                pblock_names.add(fields[1])
            elif kind == "STAT" and len(fields) == 3:
                value = int(fields[2])
                if value < 0:
                    raise ValueError("statistic must not be negative")
                if fields[1] in stats:
                    raise ValueError(f"duplicate statistic {fields[1]!r}")
                stats[fields[1]] = value
            else:
                raise ValueError(f"unsupported or malformed {kind!r} record")
        except ValueError as error:
            raise XdbError(
                f"invalid Vivado floorplan record at line {line_number} for {checkpoint}: {error}"
            ) from error

    if metadata.get("schema") != _RECORD_SCHEMA:
        observed = metadata.get("schema") or "missing"
        raise XdbError(f"unsupported Vivado floorplan record schema for {checkpoint}: {observed}")
    missing_metadata = sorted(_REQUIRED_METADATA - metadata.keys())
    if missing_metadata:
        raise XdbError(
            f"incomplete Vivado floorplan metadata for {checkpoint}: "
            f"missing {', '.join(missing_metadata)}"
        )
    empty_metadata = sorted(key for key in _REQUIRED_METADATA if not metadata[key])
    if empty_metadata:
        raise XdbError(
            f"incomplete Vivado floorplan metadata for {checkpoint}: "
            f"empty {', '.join(empty_metadata)}"
        )
    missing_stats = sorted(_REQUIRED_STATS - stats.keys())
    if missing_stats:
        raise XdbError(
            f"incomplete Vivado floorplan statistics for {checkpoint}: "
            f"missing {', '.join(missing_stats)}"
        )
    if not sites:
        raise XdbError(f"Vivado returned no physical site coordinates for: {checkpoint}")

    occupied_cells = sum(occupancy_counts.values())
    placed_cells = stats.get("placed_cells")
    if placed_cells is not None and occupied_cells != placed_cells:
        raise XdbError(
            f"inconsistent Vivado floorplan records for {checkpoint}: "
            f"occupancy accounts for {occupied_cells} cells, expected {placed_cells}"
        )
    primitive_cells = stats.get("primitive_cells")
    unplaced_cells = stats.get("unplaced_cells")
    if (
        primitive_cells is not None
        and placed_cells is not None
        and unplaced_cells is not None
        and primitive_cells != placed_cells + unplaced_cells
    ):
        raise XdbError(
            f"inconsistent Vivado floorplan records for {checkpoint}: "
            "primitive count does not equal placed plus unplaced cells"
        )
    coordinate_count = stats.get("sites_with_coordinates")
    if coordinate_count is not None and coordinate_count != len(sites):
        raise XdbError(
            f"inconsistent Vivado floorplan records for {checkpoint}: "
            f"received {len(sites)} site coordinates, expected {coordinate_count}"
        )

    occupancy = tuple(
        FloorplanOccupancy(site=site, group=group, cells=count)
        for (site, group), count in sorted(occupancy_counts.items())
    )
    return FloorplanDesign(
        source=checkpoint,
        design=metadata.get("design") or None,
        device=metadata.get("device") or None,
        tool_version=metadata.get("tool_version") or None,
        sites=sites,
        occupancy=occupancy,
        pblocks=tuple(sorted(pblocks, key=lambda item: item.name)),
        stats=stats,
    )


def inspect_floorplan_checkpoint(
    path: str | Path,
    *,
    hierarchy_depth: int = 1,
    timeout: int = 1800,
) -> FloorplanDesign:
    checkpoint = _existing_checkpoint(Path(path))
    if hierarchy_depth <= 0:
        raise XdbError("--hierarchy-depth must be > 0")
    if timeout <= 0:
        raise XdbError("--timeout must be > 0")

    with tempfile.TemporaryDirectory(prefix="xdb-floorplan-") as tmp:
        records = Path(tmp) / "floorplan.tsv"
        result = _run_vivado_tcl(
            _VIVADO_FLOORPLAN_TCL,
            [str(checkpoint), str(records), str(hierarchy_depth)],
            timeout=timeout,
        )
        try:
            text = records.read_text(encoding="utf-8", errors="replace")
        except OSError as error:
            detail = result.stdout.strip()
            suffix = f"\nVivado output:\n{detail}" if detail else ""
            raise XdbError(
                f"Vivado did not produce floorplan records: {records}{suffix}"
            ) from error
    design = parse_floorplan_records(text, checkpoint)
    if not design.occupancy or design.stats.get("placed_cells", 0) <= 0:
        raise XdbError(
            f"checkpoint contains no placed primitives; use a routed DCP: {checkpoint}"
        )
    if design.stats["routing_errors"]:
        raise XdbError(
            f"checkpoint contains {design.stats['routing_errors']} routing errors; "
            f"use a fully routed DCP: {checkpoint}"
        )
    return design


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise XdbError(f"failed to read checkpoint: {path}") from error
    return digest.hexdigest()


def _fmt(value: float) -> str:
    rounded = round(value, 2)
    if rounded == int(rounded):
        return str(int(rounded))
    return f"{rounded:.2f}".rstrip("0").rstrip(".")


def _rectangle_path_chunks(
    rectangles: Iterable[tuple[float, float, float, float]],
) -> Iterable[tuple[str, int]]:
    ordered = sorted(
        rectangles,
        key=lambda item: (item[1], item[0], item[2], item[3]),
    )
    for start in range(0, len(ordered), _MAX_PATH_RECTANGLES):
        chunk = ordered[start : start + _MAX_PATH_RECTANGLES]
        commands = [
            f"M{_fmt(x)} {_fmt(y)}h{_fmt(width)}v{_fmt(height)}h-{_fmt(width)}z"
            for x, y, width, height in chunk
        ]
        yield "".join(commands), len(chunk)


def _mark_path_chunks(
    points: Iterable[tuple[float, float]],
    width: float,
    height: float,
) -> Iterable[tuple[str, int]]:
    half_width = width / 2.0
    half_height = height / 2.0
    return _rectangle_path_chunks(
        (x - half_width, y - half_height, width, height) for x, y in points
    )


def _site_occupancy(
    design: FloorplanDesign,
) -> tuple[dict[str, Counter[str]], Counter[str], Counter[str], int]:
    by_site: dict[str, Counter[str]] = defaultdict(Counter)
    missing_cells = 0
    for item in design.occupancy:
        if item.site not in design.sites:
            missing_cells += item.cells
            continue
        by_site[item.site][item.group] += item.cells

    group_cells: Counter[str] = Counter()
    group_sites: Counter[str] = Counter()
    for counts in by_site.values():
        for group, count in counts.items():
            group_cells[group] += count
            group_sites[group] += 1
    return by_site, group_cells, group_sites, missing_cells


def _pblock_raw_rectangles(
    design: FloorplanDesign,
    pblock: FloorplanPblock,
) -> list[tuple[float, float, float, float]]:
    rectangles: list[tuple[float, float, float, float]] = []
    for item in pblock.ranges:
        if ":" not in item:
            continue
        first_name, second_name = item.split(":", 1)
        first = design.sites.get(first_name)
        second = design.sites.get(second_name)
        if first is None or second is None:
            continue
        rectangles.append(
            (
                float(min(first.x, second.x)),
                float(min(first.y, second.y)),
                float(max(first.x, second.x)),
                float(max(first.y, second.y)),
            )
        )
    return rectangles


def _merge_pblock_rectangles(
    rectangles: list[tuple[float, float, float, float]],
) -> list[tuple[float, float, float, float]]:
    tolerance = 0.0

    def contains(
        outer: tuple[float, float, float, float],
        inner: tuple[float, float, float, float],
    ) -> bool:
        ox0, oy0, ox1, oy1 = outer
        ix0, iy0, ix1, iy1 = inner
        return ox0 <= ix0 and oy0 <= iy0 and ox1 >= ix1 and oy1 >= iy1

    def can_merge(
        first: tuple[float, float, float, float],
        second: tuple[float, float, float, float],
    ) -> bool:
        if contains(first, second) or contains(second, first):
            return True
        x0, y0, x1, y1 = first
        sx0, sy0, sx1, sy1 = second
        x_gap = max(0.0, max(x0, sx0) - min(x1, sx1))
        y_gap = max(0.0, max(y0, sy0) - min(y1, sy1))
        same_y_span = abs(y0 - sy0) <= tolerance and abs(y1 - sy1) <= tolerance
        same_x_span = abs(x0 - sx0) <= tolerance and abs(x1 - sx1) <= tolerance
        return (same_y_span and x_gap <= tolerance) or (same_x_span and y_gap <= tolerance)

    merged = sorted(rectangles, key=lambda item: (item[1], item[0], item[3], item[2]))
    changed = True
    while changed:
        changed = False
        for first_index, first in enumerate(merged):
            for second_index in range(first_index + 1, len(merged)):
                second = merged[second_index]
                if not can_merge(first, second):
                    continue
                merged[first_index] = (
                    min(first[0], second[0]),
                    min(first[1], second[1]),
                    max(first[2], second[2]),
                    max(first[3], second[3]),
                )
                del merged[second_index]
                changed = True
                break
            if changed:
                break
    return merged


def _validate_title(title: str | None) -> None:
    if title is not None and _INVALID_XML_CONTROL.search(title):
        raise XdbError("figure title contains a character that is invalid in XML")


def _group_color(index: int) -> str:
    if index < len(_GROUP_COLORS):
        return _GROUP_COLORS[index]
    hue = (index * 0.6180339887498949) % 1.0
    red, green, blue = colorsys.hls_to_rgb(hue, 0.43, 0.64)
    return f"#{round(red * 255):02x}{round(green * 255):02x}{round(blue * 255):02x}"


def _short_label(value: str, limit: int = 48) -> str:
    if len(value) <= limit:
        return value
    return f"…{value[-(limit - 1) :]}"


def _svg_document(
    design: FloorplanDesign,
    *,
    title: str | None,
    hierarchy_depth: int,
    checkpoint_sha256: str,
    show_pblocks: bool,
    max_groups: int,
) -> tuple[str, dict[str, Any]]:
    site_occupancy, group_cells, group_sites, missing_cells = _site_occupancy(design)
    drawable_sites = [
        site
        for site in design.sites.values()
        if site.resource != "other" or site.name in site_occupancy
    ]
    if not drawable_sites:
        raise XdbError(f"no drawable FPGA resources found in checkpoint: {design.source}")

    min_x = min(site.x for site in drawable_sites)
    max_x = max(site.x for site in drawable_sites)
    min_y = min(site.y for site in drawable_sites)
    max_y = max(site.y for site in drawable_sites)
    raw_width = max(1.0, float(max_x - min_x))
    raw_height = max(1.0, float(max_y - min_y))
    scale = min(1050.0 / raw_width, 1020.0 / raw_height)
    plot_width = raw_width * scale
    plot_height = raw_height * scale
    left = 42.0
    top = 92.0
    legend_gap = 48.0
    legend_width = 390.0
    document_width = max(
        _MIN_DOCUMENT_WIDTH,
        left + plot_width + legend_gap + legend_width + 36.0,
    )

    groups = sorted(group for group, count in group_sites.items() if count)
    if len(groups) > max_groups:
        raise XdbError(
            f"hierarchy depth {hierarchy_depth} produced {len(groups)} color groups; "
            f"lower --hierarchy-depth or increase --max-groups (current: {max_groups})"
        )
    resource_counts = Counter(site.resource for site in design.sites.values())
    occupied_resource_counts = Counter(design.sites[name].resource for name in site_occupancy)
    resource_keys = [
        key
        for key in _RESOURCE_ORDER
        if key != "other" and (resource_counts[key] or occupied_resource_counts[key])
    ]
    if occupied_resource_counts["other"]:
        resource_keys.append("other")
    metadata_resource_keys = [
        key for key in _RESOURCE_ORDER if resource_counts[key] or occupied_resource_counts[key]
    ]

    requested_pblocks = list(design.pblocks) if show_pblocks else []
    pblock_regions: list[tuple[FloorplanPblock, list[tuple[float, float, float, float]], str]] = []
    for index, pblock in enumerate(requested_pblocks):
        regions = _merge_pblock_rectangles(_pblock_raw_rectangles(design, pblock))
        if not regions:
            continue
        stroke = _group_color(index + len(groups))
        pblock_regions.append((pblock, regions, stroke))

    resource_legend_height = 38.0 + len(resource_keys) * 24.0
    if pblock_regions:
        resource_legend_height += 42.0 + len(pblock_regions) * 24.0
    resource_legend_height += 28.0
    main_height = max(plot_height + 16.0, resource_legend_height)

    hierarchy_columns = min(3, max(1, (len(groups) + 7) // 8))
    hierarchy_rows = (len(groups) + hierarchy_columns - 1) // hierarchy_columns
    hierarchy_top = top + main_height + 38.0
    hierarchy_height = 34.0 + max(1, hierarchy_rows) * 42.0
    document_height = hierarchy_top + hierarchy_height + 25.0
    output_width = 1600
    output_height = max(600, round(output_width * document_height / document_width))

    def transform(site: FloorplanSite) -> tuple[float, float]:
        return (
            left + (site.x - min_x) * scale,
            top + (max_y - site.y) * scale,
        )

    background_points: dict[str, set[tuple[float, float]]] = defaultdict(set)
    occupied_rectangles: dict[tuple[str, str], list[tuple[float, float, float, float]]] = (
        defaultdict(list)
    )
    mixed_sites = 0
    for site in drawable_sites:
        point = transform(site)
        background_points[site.resource].add(point)
        counts = site_occupancy.get(site.name)
        if not counts:
            continue
        groups_at_site = sorted(counts)
        if len(groups_at_site) > 1:
            mixed_sites += 1
        base_width, base_height = _MARK_SIZE[site.resource]
        width = max(1.8, base_width)
        height = max(1.8, base_height)
        segment_width = width / len(groups_at_site)
        x = point[0] - width / 2.0
        y = point[1] - height / 2.0
        for index, group in enumerate(groups_at_site):
            current_width = (
                width - segment_width * index if index == len(groups_at_site) - 1 else segment_width
            )
            occupied_rectangles[(group, site.resource)].append(
                (x + segment_width * index, y, current_width, height)
            )

    color_by_group = {group: _group_color(index) for index, group in enumerate(groups)}
    display_title = title or f"FPGA placement — {design.design or design.source.stem}"
    subtitle_parts = [
        part for part in (design.device, f"hierarchy depth {hierarchy_depth}") if part
    ]
    subtitle = " · ".join(subtitle_parts)

    metadata = {
        "schema": _SCHEMA,
        "checkpoint": design.source.name,
        "checkpoint_sha256": checkpoint_sha256,
        "design": design.design,
        "device": design.device,
        "vivado_version": design.tool_version,
        "hierarchy_depth": hierarchy_depth,
        "max_groups": max_groups,
        "primitive_cells": design.stats.get("primitive_cells"),
        "placed_cells": design.stats.get("placed_cells"),
        "unplaced_cells": design.stats.get("unplaced_cells"),
        "routing_errors": design.stats.get("routing_errors"),
        "rendered_cells": sum(group_cells.values()),
        "unmapped_placed_cells": missing_cells,
        "occupied_sites": len(site_occupancy),
        "mixed_hierarchy_sites": mixed_sites,
        "groups": [
            {
                "name": group,
                "color": color_by_group[group],
                "cells": group_cells[group],
                "occupied_sites": group_sites[group],
            }
            for group in groups
        ],
        "resources": {
            key: {
                "sites": resource_counts[key],
                "occupied_sites": occupied_resource_counts[key],
            }
            for key in metadata_resource_keys
        },
        "pblocks": [item.name for item in requested_pblocks],
        "rendered_pblocks": [item.name for item, _regions, _stroke in pblock_regions],
    }

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{output_width}" '
            f'height="{output_height}" viewBox="0 0 {_fmt(document_width)} '
            f'{_fmt(document_height)}" role="img">'
        ),
        f"  <title>{xml_escape(display_title)}</title>",
        (
            "  <desc>Physical FPGA resources from a routed Vivado checkpoint; "
            "occupied sites are colored by hierarchy.</desc>"
        ),
        (
            "  <metadata>"
            f"{xml_escape(json.dumps(metadata, sort_keys=True, separators=(',', ':')))}"
            "</metadata>"
        ),
        "  <style>",
        "    text { font-family: Inter, 'DejaVu Sans', sans-serif; fill: #172033; }",
        "    .title { font-size: 26px; font-weight: 700; }",
        "    .subtitle { font-size: 14px; fill: #526071; }",
        "    .legend-title { font-size: 16px; font-weight: 700; }",
        "    .legend-label { font-size: 13px; }",
        "    .hierarchy-name { font-size: 13px; font-weight: 600; }",
        "    .legend-detail { font-size: 12px; fill: #64748b; }",
        "    .pblock-label { font-size: 11px; font-weight: 600; fill: #334155; }",
        "  </style>",
        f'  <rect width="{_fmt(document_width)}" height="{_fmt(document_height)}" fill="#ffffff"/>',
        f'  <text class="title" x="{_fmt(left)}" y="38">{xml_escape(display_title)}</text>',
        f'  <text class="subtitle" x="{_fmt(left)}" y="64">{xml_escape(subtitle)}</text>',
        (
            f'  <rect x="{_fmt(left - 8)}" y="{_fmt(top - 8)}" '
            f'width="{_fmt(plot_width + 16)}" height="{_fmt(plot_height + 16)}" '
            'rx="4" fill="#f8fafc" stroke="#94a3b8" stroke-width="1"/>'
        ),
        '  <g id="device-resources">',
    ]

    for resource in resource_keys:
        points = background_points.get(resource, set())
        if not points:
            continue
        width, height = _MARK_SIZE[resource]
        lines.extend(
            [
                (
                    f'    <g id="resource-{resource}" '
                    f'fill="{_RESOURCE_COLORS[resource]}" opacity="0.86">'
                ),
                (
                    f"      <title>{xml_escape(_RESOURCE_LABELS[resource])}: "
                    f"{resource_counts[resource]} sites</title>"
                ),
            ]
        )
        for path, _count in _mark_path_chunks(points, width, height):
            lines.append(f'      <path d="{path}"/>')
        lines.append("    </g>")
    lines.append("  </g>")

    lines.append('  <g id="placed-hierarchies">')
    for group in groups:
        for resource in _RESOURCE_ORDER:
            rectangles = occupied_rectangles.get((group, resource), [])
            if not rectangles:
                continue
            for path, site_count in _rectangle_path_chunks(rectangles):
                lines.extend(
                    [
                        (
                            f'    <path d="{path}" fill="{color_by_group[group]}" '
                            f'opacity="0.94" data-hierarchy={quoteattr(group)} '
                            f'data-resource="{resource}">'
                        ),
                        (
                            f"      <title>{xml_escape(group)} — {_RESOURCE_LABELS[resource]}: "
                            f"{site_count} occupied sites</title>"
                        ),
                        "    </path>",
                    ]
                )
    lines.append("  </g>")

    if pblock_regions:
        lines.append('  <g id="pblocks">')
        for pblock, raw_regions, stroke in pblock_regions:
            for region_index, (x0, y0, x1, y1) in enumerate(raw_regions):
                px0 = left + (x0 - min_x) * scale - 4.0
                px1 = left + (x1 - min_x) * scale + 4.0
                py0 = top + (max_y - y1) * scale - 4.0
                py1 = top + (max_y - y0) * scale + 4.0
                lines.append(
                    f'    <rect x="{_fmt(px0)}" y="{_fmt(py0)}" width="{_fmt(px1 - px0)}" '
                    f'height="{_fmt(py1 - py0)}" fill="none" stroke="{stroke}" '
                    'stroke-width="2" stroke-dasharray="8 5" opacity="0.9"/>'
                )
                if region_index == 0:
                    lines.append(
                        f'    <text class="pblock-label" x="{_fmt(px0 + 5)}" '
                        f'y="{_fmt(py0 + 15)}">{xml_escape(pblock.name)}</text>'
                    )
        lines.append("  </g>")

    legend_x = document_width - legend_width - 36.0
    legend_y = top
    lines.extend(
        [
            (f'  <g id="device-legend" transform="translate({_fmt(legend_x)} {_fmt(legend_y)})">'),
            '    <text class="legend-title" x="0" y="0">Device resources</text>',
        ]
    )
    row = 25.0
    for resource in resource_keys:
        lines.append(
            f'    <rect x="0" y="{_fmt(row - 12)}" width="15" height="15" rx="2" '
            f'fill="{_RESOURCE_COLORS[resource]}" stroke="#94a3b8" stroke-width="0.5"/>'
        )
        label = (
            f"{_RESOURCE_LABELS[resource]} — {resource_counts[resource]:,} sites, "
            f"{occupied_resource_counts[resource]:,} occupied"
        )
        lines.append(
            f'    <text class="legend-label" x="24" y="{_fmt(row)}">{xml_escape(label)}</text>'
        )
        row += 24.0

    if pblock_regions:
        row += 15.0
        lines.append(f'    <text class="legend-title" x="0" y="{_fmt(row)}">Pblocks</text>')
        row += 25.0
        for pblock, _regions, stroke in pblock_regions:
            lines.append(
                f'    <line x1="0" y1="{_fmt(row - 5)}" x2="16" y2="{_fmt(row - 5)}" '
                f'stroke="{stroke}" stroke-width="2" stroke-dasharray="6 4"/>'
            )
            lines.append(
                f'    <text class="legend-label" x="24" y="{_fmt(row)}">'
                f"{xml_escape(pblock.name)}</text>"
            )
            row += 24.0

    lines.extend(
        [
            f'    <text class="legend-detail" x="0" y="{_fmt(row + 18)}">'
            "Coordinates: Vivado RPM site grid</text>",
            "  </g>",
            '  <g id="hierarchy-legend">',
            (
                f'    <text class="legend-title" x="{_fmt(left)}" '
                f'y="{_fmt(hierarchy_top)}">Placed hierarchy</text>'
            ),
        ]
    )

    hierarchy_column_width = (document_width - 2.0 * left) / hierarchy_columns
    if groups:
        for index, group in enumerate(groups):
            column = index // hierarchy_rows
            item_row = index % hierarchy_rows
            x = left + column * hierarchy_column_width
            y = hierarchy_top + 30.0 + item_row * 42.0
            detail = f"{group_cells[group]:,} cells · {group_sites[group]:,} occupied sites"
            lines.extend(
                [
                    "    <g>",
                    (f"      <title>{xml_escape(group)} — {xml_escape(detail)}</title>"),
                    (
                        f'      <rect x="{_fmt(x)}" y="{_fmt(y - 12)}" width="15" '
                        f'height="15" rx="2" fill="{color_by_group[group]}"/>'
                    ),
                    (
                        f'      <text class="hierarchy-name" x="{_fmt(x + 24)}" '
                        f'y="{_fmt(y)}">{xml_escape(_short_label(group))}</text>'
                    ),
                    (
                        f'      <text class="legend-detail" x="{_fmt(x + 24)}" '
                        f'y="{_fmt(y + 17)}">{xml_escape(detail)}</text>'
                    ),
                    "    </g>",
                ]
            )
    else:
        lines.append(
            f'    <text class="legend-detail" x="{_fmt(left)}" '
            f'y="{_fmt(hierarchy_top + 28)}">No placed primitives</text>'
        )

    lines.extend(["  </g>", "</svg>", ""])
    return "\n".join(lines), metadata


def render_floorplan_svg(
    design: FloorplanDesign,
    *,
    title: str | None = None,
    hierarchy_depth: int = 1,
    checkpoint_sha256: str | None = None,
    show_pblocks: bool = True,
    max_groups: int = 32,
) -> tuple[str, dict[str, Any]]:
    if hierarchy_depth <= 0:
        raise XdbError("hierarchy depth must be > 0")
    if max_groups <= 0:
        raise XdbError("max groups must be > 0")
    _validate_title(title)
    digest = checkpoint_sha256 or _sha256(design.source)
    return _svg_document(
        design,
        title=title,
        hierarchy_depth=hierarchy_depth,
        checkpoint_sha256=digest,
        show_pblocks=show_pblocks,
        max_groups=max_groups,
    )


def _validate_svg_output(path: Path, *, force: bool) -> None:
    if path.suffix.lower() != ".svg":
        raise XdbError(f"floorplan output must use the .svg extension: {path}")
    if os.path.lexists(path):
        if not force:
            raise XdbError(f"output already exists (pass --force to replace it): {path}")
        if path.is_dir():
            raise XdbError(f"floorplan output is a directory: {path}")

    ancestor = path.parent
    while not os.path.lexists(ancestor):
        if ancestor == ancestor.parent:
            break
        ancestor = ancestor.parent
    if not ancestor.is_dir():
        raise XdbError(f"output parent is not a directory: {ancestor}")
    if not os.access(ancestor, os.W_OK | os.X_OK):
        raise XdbError(f"output parent is not writable: {ancestor}")


def _default_output_mode() -> int:
    current_umask = os.umask(0)
    os.umask(current_umask)
    return 0o666 & ~current_umask


def _write_svg(path: Path, svg: str, *, force: bool) -> None:
    _validate_svg_output(path, force=force)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise XdbError(f"failed to create output directory: {path.parent}") from error

    output_mode = _default_output_mode()
    if force and os.path.lexists(path):
        try:
            existing_mode = os.lstat(path).st_mode
            if stat.S_ISREG(existing_mode):
                output_mode = stat.S_IMODE(existing_mode)
        except OSError:
            pass

    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(svg)
        os.chmod(temporary, output_mode)
        if force:
            os.replace(temporary, path)
            temporary = None
        else:
            try:
                os.link(temporary, path)
            except FileExistsError as error:
                raise XdbError(
                    f"output already exists (pass --force to replace it): {path}"
                ) from error
            temporary.unlink()
            temporary = None
    except XdbError:
        raise
    except OSError as error:
        raise XdbError(f"failed to write floorplan SVG: {path}") from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def generate_floorplan_svg(
    path: str | Path,
    *,
    output: str | Path,
    dcp: str | Path | None = None,
    hierarchy_depth: int = 1,
    title: str | None = None,
    show_pblocks: bool = True,
    max_groups: int = 32,
    force: bool = False,
    timeout: int = 1800,
) -> dict[str, Any]:
    if max_groups <= 0:
        raise XdbError("--max-groups must be > 0")
    _validate_title(title)
    output_path = Path(output).expanduser()
    _validate_svg_output(output_path, force=force)
    checkpoint = discover_floorplan_checkpoint(path, dcp=dcp)
    digest = _sha256(checkpoint)
    design = inspect_floorplan_checkpoint(
        checkpoint,
        hierarchy_depth=hierarchy_depth,
        timeout=timeout,
    )
    if _sha256(checkpoint) != digest:
        raise XdbError(f"checkpoint changed while Vivado was inspecting it: {checkpoint}")
    svg, metadata = render_floorplan_svg(
        design,
        title=title,
        hierarchy_depth=hierarchy_depth,
        checkpoint_sha256=digest,
        show_pblocks=show_pblocks,
        max_groups=max_groups,
    )
    _write_svg(output_path, svg, force=force)
    return {
        **metadata,
        "source": str(checkpoint),
        "output": str(output_path),
    }


def format_floorplan_report(data: dict[str, Any]) -> str:
    groups = data.get("groups")
    group_count = len(groups) if isinstance(groups, list) else 0
    lines = [
        f"Floorplan SVG: {data.get('output')}",
        f"Checkpoint: {data.get('source')}",
        f"Device: {data.get('device') or 'unknown'}",
        (
            f"Placement: {data.get('placed_cells') or 0:,} placed primitives, "
            f"{data.get('occupied_sites') or 0:,} occupied sites, {group_count} hierarchy groups"
        ),
    ]
    unmapped = data.get("unmapped_placed_cells")
    if isinstance(unmapped, int) and unmapped:
        lines.append(
            f"Warning: {unmapped:,} placed primitives used sites without render coordinates"
        )
    return "\n".join(lines)
