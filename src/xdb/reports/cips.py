from __future__ import annotations

from pathlib import Path
import hashlib
import re
from typing import Any, cast

from xdb.backend.vivado import _run_vivado_tcl
from xdb.errors import XdbError


_SCHEMA = "xdb-cips-inspection-v1"
_PROCESSOR_PATTERNS = {
    "a72": re.compile(
        r"(?:cortex[_-]?a72|(?<![a-z0-9])a72(?![a-z0-9])|(?<![a-z0-9])apu(?![a-z0-9]))",
        re.IGNORECASE,
    ),
    "r5": re.compile(
        r"(?:cortex[_-]?r5f?|(?<![a-z0-9])r5f?(?![a-z0-9])|(?<![a-z0-9])rpu(?![a-z0-9]))",
        re.IGNORECASE,
    ),
}
_PIN_CATEGORIES = {
    "axi_noc": re.compile(r"(?:axi|noc)", re.IGNORECASE),
    "interrupts": re.compile(r"(?:irq|intr|interrupt)", re.IGNORECASE),
    "clocks": re.compile(r"(?:clk|clock)", re.IGNORECASE),
    "resets": re.compile(r"(?:rst|reset)", re.IGNORECASE),
}
_FALSE_VALUES = {"", "0", "false", "no", "none", "disabled", "off"}


_VIVADO_CIPS_TCL = r"""
proc xdb_field {value} {
  return [string map [list "\t" " " "\n" " " "\r" " "] $value]
}
proc xdb_prop {object name} {
  if {[catch {set value [get_property $name $object]}]} { return "" }
  return $value
}
set dcp [lindex $argv 0]
open_checkpoint $dcp
puts "XDB_CIPS_BEGIN"
puts "META\tdesign\t[xdb_field [current_design]]"
puts "META\tdevice\t[xdb_field [get_property PART [current_design]]]"
foreach cell [get_cells -hierarchical -quiet] {
  set name [xdb_prop $cell NAME]
  set ref_name [xdb_prop $cell REF_NAME]
  set orig_ref_name [xdb_prop $cell ORIG_REF_NAME]
  set primitive_type [xdb_prop $cell PRIMITIVE_TYPE]
  set identity [string tolower "$name $ref_name $orig_ref_name $primitive_type"]
  if {[string first "cips" $identity] < 0} { continue }

  puts "CELL\t[xdb_field $name]\t[xdb_field $ref_name]\t[xdb_field $orig_ref_name]\t[xdb_field $primitive_type]"
  foreach property [list_property $cell] {
    if {![string match "CONFIG.*" $property] &&
        ![regexp -nocase {(a72|r5|apu|rpu|axi|noc|irq|intr|clk|clock|rst|reset|boot)} $property]} {
      continue
    }
    set value [xdb_prop $cell $property]
    puts "PROP\t[xdb_field $name]\t[xdb_field $property]\t[xdb_field $value]"
  }
  foreach pin [get_pins -quiet -of_objects $cell] {
    set pin_name [xdb_prop $pin NAME]
    set direction [xdb_prop $pin DIRECTION]
    set nets {}
    foreach net [get_nets -quiet -of_objects $pin] {
      lappend nets [xdb_prop $net NAME]
    }
    puts "PIN\t[xdb_field $name]\t[xdb_field $pin_name]\t[xdb_field $direction]\t[xdb_field [join $nets ,]]"
  }
}
puts "XDB_CIPS_END"
exit 0
"""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as e:
        raise XdbError(f"failed to read artifact: {path}") from e
    return digest.hexdigest()


def _existing_file(path: Path, description: str) -> Path:
    resolved = path.expanduser()
    if not resolved.is_file():
        raise XdbError(f"{description} not found: {path}")
    return resolved


def _resolve_relative(root: Path | None, value: str | Path, description: str) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute() and root is not None:
        candidate = root / candidate
    return _existing_file(candidate, description)


def _discover_checkpoint(root: Path) -> Path | None:
    candidates = [
        root / "checkpoints" / "shell_routed.dcp",
        root / "checkpoints" / "routed.dcp",
        root / "checkpoints" / "shell_synthed.dcp",
        root / "checkpoints" / "static.dcp",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    checkpoint_root = root / "checkpoints"
    if not checkpoint_root.is_dir():
        return None
    for pattern in ("*routed*.dcp", "*synthed*.dcp", "*.dcp"):
        matches = sorted(checkpoint_root.rglob(pattern))
        if matches:
            return matches[0]
    return None


def _discover_bif(root: Path) -> Path | None:
    candidates = [
        root / "bitstreams" / "cyt_top.bif",
        root / "cyt_top.bif",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    bitstream_root = root / "bitstreams"
    if not bitstream_root.is_dir():
        return None
    matches = sorted(bitstream_root.glob("*.bif"))
    return matches[0] if matches else None


def discover_cips_artifacts(
    path: str | Path,
    *,
    dcp: str | Path | None = None,
    bif: str | Path | None = None,
) -> dict[str, Path | None]:
    selected = Path(path).expanduser()
    if not selected.exists():
        raise XdbError(f"path not found: {selected}")

    root = selected if selected.is_dir() else selected.parent
    checkpoint: Path | None = None
    boot_image: Path | None = None

    if dcp is not None:
        checkpoint = _resolve_relative(root, dcp, "checkpoint")
    if bif is not None:
        boot_image = _resolve_relative(root, bif, "BIF")

    if selected.is_file():
        suffix = selected.suffix.lower()
        if suffix == ".dcp" and checkpoint is None:
            checkpoint = selected
        elif suffix == ".bif" and boot_image is None:
            boot_image = selected
        elif suffix not in {".dcp", ".bif"}:
            raise XdbError(f"unsupported CIPS inspection artifact: {selected}")
    elif selected.is_dir():
        if checkpoint is None:
            checkpoint = _discover_checkpoint(selected)
        if boot_image is None:
            boot_image = _discover_bif(selected)
    else:
        raise XdbError(f"not a file or directory: {selected}")

    if checkpoint is None and boot_image is None:
        raise XdbError(f"no DCP checkpoint or BIF boot image found under: {selected}")
    return {"checkpoint": checkpoint, "bif": boot_image}


def _extract_vivado_records(stdout: str, source: Path) -> dict[str, Any]:
    begin = "XDB_CIPS_BEGIN"
    end = "XDB_CIPS_END"
    start = stdout.find(begin)
    finish = stdout.find(end)
    if start < 0 or finish < 0 or finish <= start:
        raise XdbError(f"could not find CIPS inspection markers in Vivado output\n{stdout}")

    metadata: dict[str, str] = {}
    cells: list[dict[str, Any]] = []
    properties: list[dict[str, str]] = []
    pins: list[dict[str, Any]] = []
    for raw_line in stdout[start + len(begin) : finish].splitlines():
        line = raw_line.strip("\r")
        if not line.strip():
            continue
        fields = line.split("\t")
        kind = fields[0]
        if kind == "META" and len(fields) >= 3:
            metadata[fields[1]] = fields[2]
        elif kind == "CELL" and len(fields) >= 4:
            fields.extend([""] * (5 - len(fields)))
            cells.append(
                {
                    "name": fields[1],
                    "ref_name": fields[2] or None,
                    "original_ref_name": fields[3] or None,
                    "primitive_type": fields[4] or None,
                }
            )
        elif kind == "PROP" and len(fields) >= 4:
            properties.append({"cell": fields[1], "name": fields[2], "value": fields[3]})
        elif kind == "PIN" and len(fields) >= 5:
            nets = [net for net in fields[4].split(",") if net]
            pins.append(
                {
                    "cell": fields[1],
                    "name": fields[2],
                    "direction": fields[3] or None,
                    "connected": bool(nets),
                    "nets": nets,
                }
            )

    if not cells:
        raise XdbError(f"no CIPS cells found in checkpoint: {source}")
    return {
        "source": str(source),
        "sha256": _sha256(source),
        "design": metadata.get("design"),
        "device": metadata.get("device"),
        "cells": cells,
        "properties": properties,
        "pins": pins,
    }


def inspect_cips_checkpoint(path: str | Path, *, timeout: int = 1800) -> dict[str, Any]:
    checkpoint = _existing_file(Path(path), "checkpoint")
    result = _run_vivado_tcl(_VIVADO_CIPS_TCL, [str(checkpoint)], timeout=timeout)
    return _extract_vivado_records(result.stdout, checkpoint)


def _strip_bif_comment(line: str) -> str:
    return re.split(r"//|#", line, maxsplit=1)[0].strip()


def parse_bif(path: str | Path) -> dict[str, Any]:
    bif = _existing_file(Path(path), "BIF")
    try:
        text = bif.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        raise XdbError(f"failed to read BIF: {bif}") from e

    boot_devices = [match.strip() for match in re.findall(r"boot_device\s*\{\s*([^}]+)\s*\}", text)]
    sections: list[dict[str, Any]] = []
    current_section: dict[str, Any] | None = None
    current_image: dict[str, Any] | None = None
    current_partition: dict[str, Any] | None = None
    stack: list[str] = []
    pending_block: str | None = None

    for raw_line in text.splitlines():
        line = _strip_bif_comment(raw_line)
        if not line:
            continue
        section_match = re.match(r"^([A-Za-z0-9_.-]+)\s*:\s*$", line)
        if section_match:
            current_section = {"name": section_match.group(1), "images": []}
            sections.append(current_section)
            continue
        if line in {"image", "partition"}:
            pending_block = line
            continue
        if line == "{" or line.endswith("{"):
            block = pending_block
            if block is None:
                prefix = line[:-1].strip()
                block = prefix if prefix in {"image", "partition"} else "other"
            pending_block = None
            stack.append(block)
            if block == "image":
                current_image = {"partitions": []}
            elif block == "partition":
                current_partition = {}
            continue
        if line.startswith("}"):
            if not stack:
                continue
            block = stack.pop()
            if block == "partition" and current_partition is not None:
                if current_image is not None:
                    cast(list[dict[str, Any]], current_image["partitions"]).append(
                        current_partition
                    )
                current_partition = None
            elif block == "image" and current_image is not None:
                if current_section is None:
                    current_section = {"name": "default", "images": []}
                    sections.append(current_section)
                cast(list[dict[str, Any]], current_section["images"]).append(current_image)
                current_image = None
            continue

        assignment = re.match(r"^([A-Za-z0-9_.-]+)\s*=\s*(.*?)\s*$", line)
        if assignment:
            key, value = assignment.groups()
            target: dict[str, Any] | None
            if current_partition is not None:
                target = current_partition
            elif current_image is not None:
                target = current_image
            else:
                target = current_section
            if target is not None:
                target[key] = value.strip().rstrip(",")

    images = [
        image for section in sections for image in cast(list[dict[str, Any]], section["images"])
    ]
    partitions = [
        partition
        for image in images
        for partition in cast(list[dict[str, Any]], image.get("partitions", []))
    ]
    for partition in partitions:
        core = str(partition.get("core", ""))
        partition["processor"] = _processor_name(core)
        file_name = str(partition.get("file", ""))
        partition["management_firmware"] = bool(
            re.search(r"(?:^|/)(?:plm|psm_fw)\.elf$", file_name, re.IGNORECASE)
            or core.lower() in {"psm", "pmc"}
        )

    return {
        "source": str(bif),
        "sha256": _sha256(bif),
        "boot_devices": boot_devices,
        "sections": sections,
        "partitions": partitions,
    }


def _processor_name(value: str) -> str | None:
    for name, pattern in _PROCESSOR_PATTERNS.items():
        if pattern.search(value):
            return name
    return None


def _processor_findings(checkpoint: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    findings: dict[str, dict[str, Any]] = {}
    properties = checkpoint.get("properties", []) if checkpoint else []
    pins = checkpoint.get("pins", []) if checkpoint else []
    for processor, pattern in _PROCESSOR_PATTERNS.items():
        property_evidence = [
            item
            for item in properties
            if isinstance(item, dict)
            and pattern.search(f"{item.get('name', '')} {item.get('value', '')}")
        ]
        pin_evidence = [
            item
            for item in pins
            if isinstance(item, dict) and pattern.search(str(item.get("name", "")))
        ]
        connected = [item for item in pin_evidence if item.get("connected")]
        configured = [
            item
            for item in property_evidence
            if str(item.get("value", "")).strip().lower() not in _FALSE_VALUES
        ]
        if connected:
            status = "connected"
        elif configured:
            status = "configured"
        elif property_evidence or pin_evidence:
            status = "observed"
        else:
            status = "not_observed"
        findings[processor] = {
            "status": status,
            "property_evidence": property_evidence,
            "pin_evidence": pin_evidence,
        }
    return findings


def _connection_findings(checkpoint: dict[str, Any] | None) -> dict[str, list[dict[str, Any]]]:
    pins = checkpoint.get("pins", []) if checkpoint else []
    findings: dict[str, list[dict[str, Any]]] = {}
    for category, pattern in _PIN_CATEGORIES.items():
        findings[category] = [
            pin
            for pin in pins
            if isinstance(pin, dict)
            and pin.get("connected")
            and pattern.search(str(pin.get("name", "")))
        ]
    return findings


def inspect_cips(
    path: str | Path,
    *,
    dcp: str | Path | None = None,
    bif: str | Path | None = None,
    timeout: int = 1800,
) -> dict[str, Any]:
    artifacts = discover_cips_artifacts(path, dcp=dcp, bif=bif)
    checkpoint_path = artifacts["checkpoint"]
    bif_path = artifacts["bif"]
    checkpoint = (
        inspect_cips_checkpoint(checkpoint_path, timeout=timeout)
        if checkpoint_path is not None
        else None
    )
    boot_image = parse_bif(bif_path) if bif_path is not None else None
    partitions = boot_image.get("partitions", []) if boot_image else []
    processor_partitions = [
        partition
        for partition in partitions
        if isinstance(partition, dict) and partition.get("processor") is not None
    ]
    management_partitions = [
        partition
        for partition in partitions
        if isinstance(partition, dict) and partition.get("management_firmware")
    ]
    return {
        "schema": _SCHEMA,
        "input": str(Path(path).expanduser()),
        "checkpoint": checkpoint,
        "boot_image": boot_image,
        "findings": {
            "cips_present": None if checkpoint is None else True,
            "processors": _processor_findings(checkpoint),
            "connections": _connection_findings(checkpoint),
            "processor_boot_partitions": processor_partitions,
            "management_firmware_partitions": management_partitions,
        },
        "limitations": [
            "not_observed means no evidence was visible in the selected artifacts; it does not prove that the silicon or card lacks the feature",
            "checkpoint inspection reports implemented connectivity and retained properties, not board wiring or runtime accessibility",
            "BIF inspection reports packaged boot partitions, not successful processor execution",
        ],
    }


def _short_path(value: object) -> str:
    text = str(value or "-")
    return text if len(text) <= 100 else f"...{text[-97:]}"


def format_cips_report(data: dict[str, Any]) -> str:
    checkpoint = data.get("checkpoint")
    boot_image = data.get("boot_image")
    findings = cast(dict[str, Any], data.get("findings", {}))
    lines = ["CIPS inspection"]
    if isinstance(checkpoint, dict):
        lines.extend(
            [
                f"Checkpoint: {_short_path(checkpoint.get('source'))}",
                f"Design:     {checkpoint.get('design') or '-'}",
                f"Device:     {checkpoint.get('device') or '-'}",
                f"CIPS cells: {len(checkpoint.get('cells', []))}",
            ]
        )
    else:
        lines.append("Checkpoint: not inspected")

    lines.append("Processors:")
    processors = cast(dict[str, dict[str, Any]], findings.get("processors", {}))
    for processor in ("a72", "r5"):
        item = processors.get(processor, {})
        status = item.get("status", "not_observed")
        lines.append(f"  {processor.upper():<3} {status}")

    connections = cast(dict[str, list[dict[str, Any]]], findings.get("connections", {}))
    lines.append("Connected CIPS pins:")
    for category, label in (
        ("axi_noc", "AXI/NoC"),
        ("interrupts", "Interrupt"),
        ("clocks", "Clock"),
        ("resets", "Reset"),
    ):
        pins = connections.get(category, [])
        lines.append(f"  {label:<9} {len(pins)}")
        for pin in pins[:5]:
            nets = ", ".join(str(net) for net in pin.get("nets", [])) or "-"
            lines.append(f"    {pin.get('name', '-')} -> {nets}")
        if len(pins) > 5:
            lines.append(f"    ... {len(pins) - 5} more")

    if isinstance(boot_image, dict):
        boot_devices = ", ".join(boot_image.get("boot_devices", [])) or "not declared"
        processor_partitions = findings.get("processor_boot_partitions", [])
        management_partitions = findings.get("management_firmware_partitions", [])
        lines.extend(
            [
                f"BIF:        {_short_path(boot_image.get('source'))}",
                f"Boot device: {boot_devices}",
                f"Processor application partitions: {len(processor_partitions)}",
                f"Management firmware partitions:   {len(management_partitions)}",
            ]
        )
        for partition in processor_partitions:
            lines.append(
                f"  {str(partition.get('processor')).upper()} "
                f"core={partition.get('core', '-')} file={partition.get('file', '-')}"
            )
    else:
        lines.append("BIF:        not inspected")

    lines.append(
        "Note: not_observed is absence of artifact evidence, not proof of unsupported hardware."
    )
    return "\n".join(lines)
