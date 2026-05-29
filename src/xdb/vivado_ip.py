from __future__ import annotations

from fnmatch import fnmatchcase
import json
from pathlib import Path
import xml.etree.ElementTree as ET
from typing import Any, Iterable

from xdb.errors import XdbError

_DEFAULT_PARAMETER_NAMES = {
    "Component_Name",
    "DEVICE",
    "PACKAGE",
    "SPEEDGRADE",
    "SWVERSION",
    "SYNTHESISFLOW",
    "IPREVISION",
    "OUTPUTDIR",
}
_DEFAULT_PARAMETER_PREFIXES = (
    "CONFIG.",
    "GT_",
    "C_GT_",
    "LINE_RATE",
    "DATA_WIDTH",
    "TDATA",
    "TUSER",
    "TDEST",
    "TID",
    "HAS_",
    "NUM_",
    "REG_",
    "C_",
)

_PARAMETER_GROUP_NAMES = {
    "component_parameters": "component",
    "model_parameters": "model",
    "project_parameters": "project",
    "runtime_parameters": "runtime",
}


def _read_text(path: Path, description: str) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        raise XdbError(f"failed to read {description}: {path}") from e


def _existing_path(path: str | Path) -> Path:
    resolved = Path(path).expanduser()
    if not resolved.exists():
        raise XdbError(f"path not found: {path}")
    return resolved


def _first_value(entries: object) -> dict[str, Any]:
    if isinstance(entries, list) and entries and isinstance(entries[0], dict):
        return dict(entries[0])
    if isinstance(entries, dict):
        return dict(entries)
    return {"value": entries}


def _param_value(parameters: dict[str, dict[str, dict[str, Any]]], *names: str) -> str | None:
    for group in parameters.values():
        for name in names:
            entry = group.get(name)
            if entry is None:
                continue
            value = entry.get("value")
            if value not in {None, ""}:
                return str(value)
    return None


def _port_width(port: dict[str, Any]) -> int | None:
    left = port.get("left")
    right = port.get("right")
    try:
        if left is None or right is None:
            return None
        return abs(int(str(left)) - int(str(right))) + 1
    except ValueError:
        return None


def _parse_json_xci(path: Path, text: str) -> dict[str, Any]:
    try:
        root = json.loads(text)
    except json.JSONDecodeError as e:
        raise XdbError(f"invalid JSON XCI: {path}") from e
    if not isinstance(root, dict):
        raise XdbError(f"invalid JSON XCI root: {path}")
    ip = root.get("ip_inst")
    if not isinstance(ip, dict):
        raise XdbError(f"JSON file does not look like a Vivado XCI: {path}")

    parameters: dict[str, dict[str, dict[str, Any]]] = {}
    raw_parameters = ip.get("parameters") if isinstance(ip.get("parameters"), dict) else {}
    for raw_group, friendly_group in _PARAMETER_GROUP_NAMES.items():
        raw_values = raw_parameters.get(raw_group) if isinstance(raw_parameters, dict) else None
        group: dict[str, dict[str, Any]] = {}
        if isinstance(raw_values, dict):
            for name, entries in raw_values.items():
                group[str(name)] = _first_value(entries)
        parameters[friendly_group] = group

    ports: list[dict[str, Any]] = []
    boundary = ip.get("boundary") if isinstance(ip.get("boundary"), dict) else {}
    raw_ports = boundary.get("ports") if isinstance(boundary, dict) else None
    if isinstance(raw_ports, dict):
        for name, entries in raw_ports.items():
            entry = _first_value(entries)
            port = {
                "name": str(name),
                "direction": entry.get("direction"),
                "left": entry.get("size_left"),
                "right": entry.get("size_right"),
            }
            port["width"] = _port_width(port) or 1
            ports.append(port)

    interfaces: list[dict[str, Any]] = []
    raw_interfaces = boundary.get("interfaces") if isinstance(boundary, dict) else None
    if isinstance(raw_interfaces, dict):
        for name, iface in raw_interfaces.items():
            if not isinstance(iface, dict):
                continue
            interfaces.append(
                {
                    "name": str(name),
                    "mode": iface.get("mode"),
                    "vlnv": iface.get("vlnv"),
                    "abstraction_type": iface.get("abstraction_type"),
                }
            )

    device = _param_value(parameters, "DEVICE")
    package = _param_value(parameters, "PACKAGE")
    speedgrade = _param_value(parameters, "SPEEDGRADE")
    if device and package and speedgrade:
        part = f"{device}-{package}{speedgrade}"
    elif device and package:
        part = f"{device}-{package}"
    else:
        part = device or None

    return {
        "path": str(path),
        "format": "json",
        "schema": root.get("schema"),
        "name": ip.get("xci_name") or _param_value(parameters, "Component_Name") or path.stem,
        "vlnv": ip.get("component_reference"),
        "ip_revision": ip.get("ip_revision") or _param_value(parameters, "IPREVISION"),
        "part": part,
        "sw_version": _param_value(parameters, "SWVERSION"),
        "gen_directory": ip.get("gen_directory") or _param_value(parameters, "OUTPUTDIR"),
        "parameters": parameters,
        "ports": sorted(ports, key=lambda item: str(item.get("name") or "")),
        "interfaces": sorted(interfaces, key=lambda item: str(item.get("name") or "")),
    }


def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _child_text(element: ET.Element, local_name: str) -> str | None:
    for child in element.iter():
        if _strip_ns(child.tag) == local_name and child.text:
            return child.text.strip()
    return None


def _parse_xml_xci(path: Path, text: str) -> dict[str, Any]:
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise XdbError(f"invalid XML XCI: {path}") from e

    parameters: dict[str, dict[str, dict[str, Any]]] = {"component": {}, "model": {}, "project": {}, "runtime": {}}
    ports: list[dict[str, Any]] = []

    for element in root.iter():
        local = _strip_ns(element.tag)
        if local == "configurableElementValue":
            ref = element.attrib.get("referenceId") or element.attrib.get("{http://www.spiritconsortium.org/XMLSchema/SPIRIT/1685-2009}referenceId")
            if not ref:
                continue
            name = ref.rsplit(".", 1)[-1]
            parameters["component"][name] = {"value": (element.text or "").strip()}
        elif local == "port":
            name = _child_text(element, "name")
            if not name:
                continue
            direction = _child_text(element, "direction")
            left = _child_text(element, "left")
            right = _child_text(element, "right")
            port = {"name": name, "direction": direction, "left": left, "right": right}
            port["width"] = _port_width(port) or 1
            ports.append(port)

    vendor = _child_text(root, "vendor")
    library = _child_text(root, "library")
    name = _child_text(root, "name")
    version = _child_text(root, "version")
    vlnv = ":".join(part for part in [vendor, library, name, version] if part) or None

    return {
        "path": str(path),
        "format": "xml",
        "schema": None,
        "name": _param_value(parameters, "Component_Name") or name or path.stem,
        "vlnv": vlnv,
        "ip_revision": _param_value(parameters, "IPREVISION", "CORE_REVISION"),
        "part": _param_value(parameters, "PART", "DEVICE"),
        "sw_version": _param_value(parameters, "SWVERSION"),
        "gen_directory": _param_value(parameters, "OUTPUTDIR"),
        "parameters": parameters,
        "ports": sorted(ports, key=lambda item: str(item.get("name") or "")),
        "interfaces": [],
    }


def parse_xci(path: str | Path) -> dict[str, Any]:
    xci = _existing_path(path)
    if not xci.is_file():
        raise XdbError(f"XCI path is not a file: {path}")
    text = _read_text(xci, "XCI")
    stripped = text.lstrip()
    if stripped.startswith("{"):
        return _parse_json_xci(xci, text)
    if stripped.startswith("<"):
        return _parse_xml_xci(xci, text)
    raise XdbError(f"file does not look like a JSON or XML XCI: {path}")


def discover_xci_files(path: str | Path) -> list[Path]:
    root = _existing_path(path)
    if root.is_file():
        if root.suffix.lower() != ".xci":
            raise XdbError(f"not an XCI file: {path}")
        return [root]
    matches = sorted(root.rglob("*.xci"))
    if not matches:
        raise XdbError(f"no .xci files found under: {path}")
    return matches


def _matches_any(name: str, patterns: Iterable[str]) -> bool:
    lowered = name.lower()
    for pattern in patterns:
        p = pattern.lower()
        if fnmatchcase(lowered, p) or p in lowered:
            return True
    return False


def _default_parameter_selected(name: str, entry: dict[str, Any]) -> bool:
    if name in _DEFAULT_PARAMETER_NAMES:
        return True
    if any(name.startswith(prefix) for prefix in _DEFAULT_PARAMETER_PREFIXES):
        return True
    if entry.get("value_src") == "user" or entry.get("resolve_type") == "user":
        return True
    return False


def _filter_parameters(
    parameters: dict[str, dict[str, dict[str, Any]]],
    *,
    include_all: bool = False,
    patterns: list[str] | None = None,
) -> dict[str, dict[str, dict[str, Any]]]:
    out: dict[str, dict[str, dict[str, Any]]] = {}
    pattern_values = list(patterns or [])
    for group_name, group in parameters.items():
        selected: dict[str, dict[str, Any]] = {}
        for name, entry in group.items():
            if pattern_values:
                keep = _matches_any(name, pattern_values)
            elif include_all:
                keep = True
            else:
                keep = _default_parameter_selected(name, entry)
            if keep:
                selected[name] = entry
        if selected:
            out[group_name] = dict(sorted(selected.items()))
    return out


def vivado_ip_info(
    path: str | Path,
    *,
    include_all: bool = False,
    param_patterns: list[str] | None = None,
) -> dict[str, Any]:
    files = discover_xci_files(path)
    ips = []
    for xci in files:
        parsed = parse_xci(xci)
        raw_parameters = parsed.get("parameters")
        parameters = raw_parameters if isinstance(raw_parameters, dict) else {}
        parsed["parameters"] = _filter_parameters(
            parameters,
            include_all=include_all,
            patterns=param_patterns,
        )
        ips.append(parsed)
    return {"path": str(Path(path).expanduser()), "count": len(ips), "ips": ips}


def _format_param_value(entry: dict[str, Any]) -> str:
    value = entry.get("value")
    text = "" if value is None else str(value)
    if len(text) > 80:
        return text[:37] + "..." + text[-40:]
    return text


def _format_single_ip(ip: dict[str, Any]) -> list[str]:
    lines = [
        f"ip: {ip.get('name', '?')}",
        f"path: {ip.get('path', '?')}",
    ]
    for label, key in [
        ("vlnv", "vlnv"),
        ("revision", "ip_revision"),
        ("part", "part"),
        ("sw", "sw_version"),
        ("generated", "gen_directory"),
    ]:
        value = ip.get(key)
        if value not in {None, ""}:
            lines.append(f"{label}: {value}")

    parameters = ip.get("parameters") if isinstance(ip.get("parameters"), dict) else {}
    if parameters:
        lines.append("parameters:")
        for group_name, group in parameters.items():
            if not isinstance(group, dict) or not group:
                continue
            lines.append(f"  [{group_name}]")
            for name, entry in group.items():
                if isinstance(entry, dict):
                    suffix = ""
                    source = entry.get("value_src") or entry.get("resolve_type")
                    if source:
                        suffix = f" ({source})"
                    lines.append(f"    {name}: {_format_param_value(entry)}{suffix}")

    ports = [port for port in list(ip.get("ports") or []) if isinstance(port, dict)]
    if ports:
        lines.append(f"ports ({len(ports)}):")
        for port in ports:
            width = port.get("width")
            width_text = "" if width in {None, 1} else f"[{width}]"
            direction = port.get("direction") or "?"
            lines.append(f"  {port.get('name', '?'):<32} {direction}{width_text}")

    interfaces = [iface for iface in list(ip.get("interfaces") or []) if isinstance(iface, dict)]
    if interfaces:
        lines.append(f"interfaces ({len(interfaces)}):")
        for iface in interfaces:
            mode = iface.get("mode") or "?"
            vlnv = iface.get("vlnv") or ""
            lines.append(f"  {iface.get('name', '?'):<24} {mode:<8} {vlnv}")
    return lines


def format_vivado_ip_info(info: dict[str, Any]) -> str:
    ips = [ip for ip in list(info.get("ips") or []) if isinstance(ip, dict)]
    if not ips:
        return "no XCI files found"
    sections: list[str] = []
    for ip in ips:
        sections.append("\n".join(_format_single_ip(ip)))
    return "\n\n".join(sections)
