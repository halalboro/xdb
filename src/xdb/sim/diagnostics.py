from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any, cast
from collections.abc import Mapping

from xdb.errors import XdbError
from xdb.sim.protocol import OP_STATUS, make_request
from xdb.sim.session_status import config_matches, launch_spec_summary
from xdb.sim.session_store import (
    cleanup_stale_session,
    is_live_session,
    load_meta,
    pid_is_alive,
    read_runtime_stage_stamp,
    resolve_launch_spec,
    resolve_mode_arg,
    resolve_simset_arg,
    resolve_top_arg,
    session_paths,
    tree_fingerprint,
)
from xdb.sim.types import SessionMeta


def _recv_all(sock: socket.socket) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


def _doctor_check(
    name: str,
    ok: bool,
    *,
    severity: str = "error",
    detail: str = "",
    data: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {"name": name, "ok": ok, "severity": severity}
    if detail:
        result["detail"] = detail
    if data is not None:
        result["data"] = dict(data)
    return result


def _read_meta_for_doctor(paths) -> tuple[SessionMeta | None, dict[str, Any]]:
    if not paths.meta_path.exists():
        return None, {"exists": False, "valid_json": None, "error": "metadata file does not exist"}
    try:
        with paths.meta_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return None, {"exists": True, "valid_json": False, "error": str(e)}
    if not isinstance(data, dict):
        return None, {"exists": True, "valid_json": False, "error": "metadata is not an object"}
    return cast(SessionMeta, data), {"exists": True, "valid_json": True}


def _tail_text(path: Path, *, max_lines: int = 20) -> list[str]:
    if not path.is_file():
        return []
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[-max_lines:]
    except OSError:
        return []


def _probe_daemon_status(meta: SessionMeta, timeout_seconds: float) -> tuple[bool, str, dict[str, Any] | None]:
    sock_path = str(meta.get("socket_path") or "")
    if not sock_path:
        return False, "session metadata does not contain a socket path", None
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout_seconds)
        try:
            sock.connect(sock_path)
            sock.sendall(json.dumps(make_request(OP_STATUS)).encode("utf-8"))
            sock.shutdown(socket.SHUT_WR)
            response = _recv_all(sock)
        except Exception as e:
            return False, str(e), None
    if not response:
        return False, "daemon returned an empty response", None
    try:
        data = cast(dict[str, Any], json.loads(response.decode("utf-8")))
    except Exception as e:
        return False, f"daemon returned invalid JSON: {e}", None
    if not data.get("ok", False):
        return False, str(data.get("error", "status request failed")), data
    return True, "", data


def doctor_session(session_name: str | None, *, timeout_seconds: float = 1.0) -> dict[str, Any]:
    paths = session_paths(session_name)
    checks: list[dict[str, Any]] = []
    suggestions: list[str] = []
    meta, meta_state = _read_meta_for_doctor(paths)

    checks.append(
        _doctor_check(
            "session_metadata",
            bool(meta_state.get("valid_json")),
            severity="error",
            detail=str(meta_state.get("error") or ""),
            data={
                "path": str(paths.meta_path),
                "exists": bool(meta_state.get("exists")),
                "valid_json": meta_state.get("valid_json"),
            },
        )
    )
    if meta_state.get("exists") and not meta_state.get("valid_json"):
        suggestions.append("run: xdb sim close --force")
    if not meta_state.get("exists"):
        suggestions.append("run: xdb sim launch")

    pid = int((meta or {}).get("pid", 0) or 0)
    pid_alive = pid_is_alive(pid)
    if meta is not None:
        checks.append(
            _doctor_check(
                "daemon_pid_alive",
                pid_alive,
                detail="daemon process is not alive" if not pid_alive else "",
                data={"pid": pid},
            )
        )
        socket_path_text = str(meta.get("socket_path") or "")
        socket_path = Path(socket_path_text) if socket_path_text else Path("/__xdb_missing_socket__")
        socket_exists = bool(socket_path_text) and socket_path.exists()
        checks.append(
            _doctor_check(
                "control_socket_exists",
                socket_exists,
                detail="control socket is missing" if not socket_exists else "",
                data={"socket_path": str(socket_path)},
            )
        )
        if not pid_alive or not socket_exists:
            suggestions.append("run: xdb sim close --force")
        if pid_alive and socket_exists:
            responsive, error, status = _probe_daemon_status(meta, timeout_seconds)
            checks.append(
                _doctor_check(
                    "daemon_responsive",
                    responsive,
                    detail=error,
                    data={"timeout_seconds": timeout_seconds},
                )
            )
            if status is not None:
                checks[-1]["status"] = status.get("result")
            if not responsive:
                suggestions.append("run: xdb sim close --force")
                suggestions.append("run: xdb sim relaunch --fresh")

    try:
        launch_spec = resolve_launch_spec(stage=False)
    except XdbError as e:
        checks.append(
            _doctor_check(
                "runtime_configuration",
                False,
                severity="warning",
                detail=str(e),
            )
        )
        suggestions.append(
            "set XDB_SIM_PACKAGE_RUNTIME and XDB_SIM_WORKSPACE, or enter the project simulation shell"
        )
        launch_spec = None

    runtime: dict[str, Any] = {"available": launch_spec is not None}
    if launch_spec is not None:
        package_runtime = str(launch_spec.get("package_runtime") or "")
        workspace = str(launch_spec.get("workspace") or "")
        package_path = Path(package_runtime)
        workspace_path = Path(workspace)
        package_fingerprint = tree_fingerprint(package_runtime)
        stage_stamp = read_runtime_stage_stamp(workspace) if workspace else None
        needs_stage = bool(launch_spec.get("needs_stage"))
        runtime.update(
            {
                **launch_spec_summary(launch_spec),
                "package_exists": package_path.is_dir(),
                "workspace_exists": workspace_path.exists(),
                "package_fingerprint": package_fingerprint,
                "staged_at": None if stage_stamp is None else stage_stamp.get("updated_at"),
                "stage_source_matches_package": None
                if stage_stamp is None
                else stage_stamp.get("source_root") == package_runtime,
                "stage_fingerprint_matches_package": None
                if stage_stamp is None
                else stage_stamp.get("source_fingerprint") == package_fingerprint,
            }
        )
        checks.append(
            _doctor_check(
                "runtime_package_exists",
                package_path.is_dir(),
                detail="runtime package path does not exist" if not package_path.is_dir() else "",
                data={"package_runtime": package_runtime},
            )
        )
        checks.append(
            _doctor_check(
                "workspace_fresh",
                not needs_stage,
                severity="warning",
                detail="workspace needs staging/restaging" if needs_stage else "",
                data={"workspace": workspace, "needs_stage": needs_stage},
            )
        )
        if needs_stage:
            suggestions.append("run: xdb sim restage")
            suggestions.append("run: xdb sim relaunch --fresh")
        if meta is not None:
            requested_simset = resolve_simset_arg(None)
            requested_mode = resolve_mode_arg(None)
            requested_top = resolve_top_arg(None, meta)
            matches = config_matches(meta, launch_spec, requested_simset, requested_mode, requested_top)
            checks.append(
                _doctor_check(
                    "live_session_matches_request",
                    matches,
                    severity="warning",
                    detail="cached/live session metadata differs from requested runtime inputs"
                    if not matches
                    else "",
                )
            )
            if not matches:
                suggestions.append("run: xdb sim relaunch --fresh")

    log_info: dict[str, Any] = {}
    for name, path in (
        ("daemon_log", paths.daemon_log_path),
        ("vivado_log", paths.vivado_log_path),
    ):
        exists = path.is_file()
        tail = _tail_text(path)
        log_info[name] = {"path": str(path), "exists": exists, "tail": tail}
        checks.append(
            _doctor_check(
                f"{name}_exists",
                exists,
                severity="warning",
                detail=f"{name.replace('_', ' ')} does not exist" if not exists else "",
                data={"path": str(path)},
            )
        )

    seen_suggestions: list[str] = []
    for suggestion in suggestions:
        if suggestion not in seen_suggestions:
            seen_suggestions.append(suggestion)
    ok = all(check.get("ok", False) or check.get("severity") != "error" for check in checks)
    return {
        "ok": ok,
        "session": paths.session_name,
        "session_id": paths.session_id,
        "anchor_dir": str(paths.anchor_dir),
        "paths": {
            "xdb_root": str(paths.xdb_root),
            "cache_root": str(paths.cache_root),
            "session_dir": str(paths.session_dir),
            "meta": str(paths.meta_path),
            "socket": str(paths.socket_path),
            "daemon_log": str(paths.daemon_log_path),
            "vivado_log": str(paths.vivado_log_path),
        },
        "checks": checks,
        "runtime": runtime,
        "metadata": meta or None,
        "logs": log_info,
        "suggestions": seen_suggestions,
    }


def provenance_session(session_name: str | None) -> dict[str, Any]:
    paths = session_paths(session_name)
    cleanup_stale_session(paths)
    meta = load_meta(paths)
    live_meta = meta if is_live_session(meta) else None

    requested_simset = resolve_simset_arg(None)
    requested_mode = resolve_mode_arg(None)
    requested_top = resolve_top_arg(None, meta)

    result: dict[str, Any] = {
        "session": paths.session_name,
        "session_id": paths.session_id,
        "anchor_dir": str(paths.anchor_dir),
        "requested": {
            "simset": requested_simset,
            "mode": requested_mode,
            "top": requested_top,
        },
        "live_session": {
            "present": live_meta is not None,
            "state": None if meta is None else meta.get("state"),
            "pid": None if live_meta is None else live_meta.get("pid"),
            "launched_at": None if meta is None else meta.get("created_at"),
            "updated_at": None if meta is None else meta.get("updated_at"),
            "package_runtime": None if meta is None else meta.get("package_runtime"),
            "runtime_root": None if meta is None else meta.get("runtime_root"),
            "socket_path": None if meta is None else meta.get("socket_path"),
        },
    }

    try:
        launch_spec = resolve_launch_spec(stage=False)
    except XdbError as e:
        result["runtime"] = {
            "available": False,
            "error": str(e),
        }
        return result

    package_runtime = str(launch_spec.get("package_runtime") or "")
    workspace = str(launch_spec.get("workspace") or "")
    runtime_root = str(launch_spec.get("runtime_root") or "")
    package_fingerprint = tree_fingerprint(package_runtime)
    workspace_fingerprint = tree_fingerprint(workspace)
    stage_stamp = read_runtime_stage_stamp(workspace)

    result["runtime"] = {
        "available": True,
        **launch_spec_summary(launch_spec),
        "package_fingerprint": package_fingerprint,
        "workspace_fingerprint": workspace_fingerprint,
        "workspace_exists": Path(workspace).exists(),
        "staged_at": None if stage_stamp is None else stage_stamp.get("updated_at"),
        "stage_source_root": None if stage_stamp is None else stage_stamp.get("source_root"),
        "stage_source_matches_package": None
        if stage_stamp is None
        else stage_stamp.get("source_root") == package_runtime,
        "stage_fingerprint_matches_package": None
        if stage_stamp is None
        else stage_stamp.get("source_fingerprint") == package_fingerprint,
    }
    result["comparisons"] = {
        "live_session_matches_request": None
        if live_meta is None
        else config_matches(
            live_meta,
            launch_spec,
            requested_simset,
            requested_mode,
            requested_top,
        ),
        "live_session_uses_workspace": None
        if live_meta is None
        else str(live_meta.get("runtime_root") or "") == workspace,
        "runtime_root_matches_workspace": runtime_root == workspace,
    }
    return result
