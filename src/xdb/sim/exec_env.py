from __future__ import annotations

from pathlib import Path
from typing import Mapping

from xdb.errors import XdbError


def normalize_remainder_command(command: list[str]) -> list[str]:
    normalized = list(command)
    if normalized and normalized[0] == "--":
        normalized = normalized[1:]
    if not normalized:
        raise XdbError("missing command after '--'")
    return normalized


def parse_env_overrides(values: list[str]) -> dict[str, str]:
    overrides: dict[str, str] = {}
    for item in values:
        if "=" not in item:
            raise XdbError(f"environment override must be KEY=VALUE: {item!r}")
        key, value = item.split("=", 1)
        key = key.strip()
        if not key:
            raise XdbError(f"environment override has empty key: {item!r}")
        overrides[key] = value
    return overrides


def derive_sim_exec_env(meta: Mapping[str, object], session_name: str) -> dict[str, str]:
    runtime_root = str(meta.get("runtime_root") or "")
    work_dir = str(meta.get("work_dir") or "")
    env: dict[str, str] = {
        "XDB_SIM_SESSION": session_name,
    }
    for key, value in (
        ("XDB_SIM_RUNTIME_ROOT", runtime_root),
        ("XDB_SIM_WORKSPACE", runtime_root),
        ("XDB_SIM_WORK_DIR", work_dir),
        ("XDB_SIM_SOCKET", str(meta.get("socket_path") or "")),
        ("XDB_SIM_PACKAGE_RUNTIME", str(meta.get("package_runtime") or "")),
        ("XDB_SIM_PROJECT", str(meta.get("project") or "")),
        ("XDB_SIM_SIMSET", str(meta.get("simset") or "")),
        ("XDB_SIM_TOP", str(meta.get("top") or "")),
        ("XDB_SIM_MODE", str(meta.get("mode") or "")),
    ):
        if value:
            env[key] = value
    if runtime_root:
        # Coyote's simulation cThread appends /sim internally before opening input.bin/output.bin.
        env["COYOTE_SIM_DIR"] = runtime_root
    return env


def resolve_exec_cwd(cwd: str | None, meta: Mapping[str, object], fallback: Path) -> str:
    return str(Path(cwd).expanduser().resolve()) if cwd else str(meta.get("anchor_dir") or fallback)
