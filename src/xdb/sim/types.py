from __future__ import annotations

from typing import Any, TypedDict


class SessionMeta(TypedDict, total=False):
    session_id: str
    session_name: str
    pid: int
    socket_path: str
    session_dir: str
    daemon_log: str
    vivado_log: str
    anchor_dir: str
    xdb_root: str
    cache_root: str
    cwd: str
    launch_kind: str
    project: str
    simset: str
    mode: str
    top: str
    package_runtime: str
    runtime_root: str
    workspace: str
    work_dir: str
    compile_script: str
    elaborate_script: str
    simulate_script: str
    created_at: str
    updated_at: str
    state: str
    last_error: str


class SimRequest(TypedDict, total=False):
    op: str
    args: dict[str, Any]


class SimResponse(TypedDict, total=False):
    ok: bool
    error: str
    result: dict[str, Any]
