from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from xdb.errors import XdbError
from xdb.sim.session_store import session_paths

_PROFILE_FILENAMES = (".xdb-trace.json", "xdb-trace.json")


def _profile_search_root() -> Path:
    return session_paths(None).anchor_dir


def find_trace_profile_file(profile_file: str | None = None) -> Path | None:
    if profile_file:
        path = Path(profile_file).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        path = path.resolve()
        if not path.is_file():
            raise XdbError(f"trace profile file not found: {path}")
        return path
    root = _profile_search_root()
    for filename in _PROFILE_FILENAMES:
        candidate = root / filename
        if candidate.is_file():
            return candidate
    return None


def load_trace_profiles(profile_file: str | None = None) -> dict[str, Any]:
    path = find_trace_profile_file(profile_file)
    if path is None:
        return {"source": None, "profiles": {}}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        raise XdbError(f"failed to read trace profile file: {path}") from e
    if not isinstance(data, dict):
        raise XdbError(f"invalid trace profile file: {path}: top-level value must be an object")
    profiles = data.get("profiles", data)
    if not isinstance(profiles, dict):
        raise XdbError(f"invalid trace profile file: {path}: 'profiles' must be an object")
    normalized: dict[str, dict[str, Any]] = {}
    for name, profile in profiles.items():
        if not isinstance(name, str) or not name:
            raise XdbError(f"invalid trace profile file: {path}: profile names must be non-empty strings")
        if not isinstance(profile, dict):
            raise XdbError(f"invalid trace profile {name!r}: profile value must be an object")
        normalized[name] = dict(cast(dict[str, Any], profile))
    return {"source": str(path), "profiles": normalized}


def get_trace_profile(name: str | None, profile_file: str | None = None) -> dict[str, Any]:
    loaded = load_trace_profiles(profile_file)
    if not name:
        return {"name": None, "source": loaded.get("source"), "config": {}}
    profiles = cast(dict[str, dict[str, Any]], loaded.get("profiles") or {})
    if name not in profiles:
        available = ", ".join(sorted(profiles)) or "<none>"
        raise XdbError(f"trace profile not found: {name!r} (available: {available})")
    return {"name": name, "source": loaded.get("source"), "config": dict(profiles[name])}


def list_trace_profiles(profile_file: str | None = None) -> dict[str, Any]:
    loaded = load_trace_profiles(profile_file)
    profiles = cast(dict[str, dict[str, Any]], loaded.get("profiles") or {})
    return {
        "source": loaded.get("source"),
        "count": len(profiles),
        "profiles": [
            {"name": name, "config": profiles[name]}
            for name in sorted(profiles)
        ],
    }
