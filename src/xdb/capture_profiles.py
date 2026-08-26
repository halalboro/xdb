from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any

from xdb.errors import XdbError

_SCHEMA = "xdb.ila-capture-profiles/v1"
_ALLOWED = {
    "ila",
    "ilas",
    "source_ila",
    "samples",
    "windows",
    "trigger_position",
    "triggers",
    "capture_values",
    "trigger_condition",
    "capture_condition",
    "tsm_path",
    "trig_in",
    "trig_out",
    "export_format",
}


def _load_document(path: Path) -> dict[str, Any]:
    try:
        if path.suffix.lower() == ".json":
            value = json.loads(path.read_text(encoding="utf-8"))
        elif path.suffix.lower() in {".toml", ".tml"}:
            try:
                tomllib = importlib.import_module("tomllib")
            except ImportError as error:
                raise XdbError("TOML capture profiles require Python 3.11 or newer") from error
            with path.open("rb") as stream:
                value = tomllib.load(stream)
        else:
            raise XdbError("capture profile must use .json or .toml")
    except (OSError, json.JSONDecodeError) as error:
        raise XdbError(f"invalid capture profile file: {path}") from error
    if not isinstance(value, dict) or value.get("schema") != _SCHEMA:
        raise XdbError(f"unsupported capture profile document: {path}")
    return value


def load_capture_profile(path_value: str, name: str) -> dict[str, Any]:
    path = Path(path_value).expanduser().resolve()
    document = _load_document(path)
    profiles = document.get("profiles")
    if not isinstance(profiles, dict) or name not in profiles:
        raise XdbError(f"capture profile not found: {name}")
    selected = profiles[name]
    if not isinstance(selected, dict):
        raise XdbError(f"capture profile must be an object: {name}")
    unknown = sorted(set(selected) - _ALLOWED)
    if unknown:
        raise XdbError(f"capture profile {name} has unsupported fields: {', '.join(unknown)}")
    result = dict(selected)
    if "tsm_path" in result:
        tsm = Path(str(result["tsm_path"])).expanduser()
        if not tsm.is_absolute():
            tsm = path.parent / tsm
        result["tsm_path"] = str(tsm.resolve())
    return result


def profile_value(args, profile: dict[str, Any], name: str, default: Any = None) -> Any:
    cli_value = getattr(args, name, None)
    return profile.get(name, default) if cli_value is None else cli_value
