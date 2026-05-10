from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, cast

from xdb.errors import XdbError

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised only on Python 3.10
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ImportError as e:  # pragma: no cover
        raise ImportError("TOML config support on Python <3.11 requires tomli") from e

_CONFIG_ENV = "XDB_CONFIG_FILE"

_config_file_override: str | None = None


def set_config_file(path: str | None) -> None:
    global _config_file_override
    _config_file_override = path.strip() if path and path.strip() else None


def configured_config_file() -> str | None:
    return _config_file_override or os.environ.get(_CONFIG_ENV)


def resolve_config_file(path: str | None = None) -> Path | None:
    value = path or configured_config_file()
    if not value:
        return None
    resolved = Path(value).expanduser()
    if not resolved.is_absolute():
        resolved = Path.cwd() / resolved
    resolved = resolved.resolve()
    if not resolved.is_file():
        raise XdbError(f"xdb config file not found: {resolved}")
    return resolved


def load_config(path: str | None = None) -> dict[str, Any]:
    config_path = resolve_config_file(path)
    if config_path is None:
        return {"source": None, "config": {}}
    try:
        with config_path.open("rb") as f:
            data = tomllib.load(f)
    except Exception as e:
        raise XdbError(f"failed to read xdb config file: {config_path}") from e
    if not isinstance(data, dict):
        raise XdbError(f"invalid xdb config file: {config_path}: top-level value must be a table")
    return {"source": str(config_path), "config": cast(dict[str, Any], data)}


def get_config_value(*keys: str, path: str | None = None) -> Any | None:
    loaded = load_config(path)
    value: Any = loaded.get("config") or {}
    for key in keys:
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return value


def resolve_config_path(value: str, *, config_source: str | None) -> str:
    path = Path(value).expanduser()
    if path.is_absolute():
        return str(path.resolve())
    if config_source:
        return str((Path(config_source).parent / path).resolve())
    return str((Path.cwd() / path).resolve())


def config_path_value(*keys: str, path: str | None = None) -> str | None:
    loaded = load_config(path)
    value: Any = loaded.get("config") or {}
    for key in keys:
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    if value is None:
        return None
    if not isinstance(value, str):
        dotted = ".".join(keys)
        raise XdbError(f"xdb config field {dotted!r} must be a string path")
    return resolve_config_path(value, config_source=cast(str | None, loaded.get("source")))
