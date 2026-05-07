from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from ..errors import XdbError
from .protocol import (
    OP_BREAKPOINT_ADD,
    OP_BREAKPOINT_CLEAR,
    OP_CLOSE,
    OP_GET,
    OP_GET_MANY,
    OP_OBJECTS,
    OP_RESTART,
    OP_RUN,
    OP_SCOPES,
    OP_STATUS,
    OP_STEP,
    OP_TIME,
    OP_TOP,
    OP_WAVE_ADD,
    make_request,
)
from .session_store import (
    cleanup_stale_session,
    load_meta,
    pid_is_alive,
    remove_session,
    require_live_meta,
    resolve_project_arg,
    session_paths,
    terminate_session,
)


def _send_request(session_name: str | None, request: dict[str, Any]) -> dict[str, Any]:
    paths = session_paths(session_name)
    meta = require_live_meta(paths)
    sock_path = str(meta["socket_path"])
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        try:
            sock.connect(sock_path)
        except FileNotFoundError as e:
            raise XdbError(
                f"simulation session socket missing for {paths.session_name!r}; run 'xdb sim launch' again"
            ) from e
        sock.sendall(json.dumps(request).encode("utf-8"))
        sock.shutdown(socket.SHUT_WR)
        response = _recv_all(sock)

    data = json.loads(response.decode("utf-8"))
    if not data.get("ok", False):
        raise XdbError(str(data.get("error", "simulation request failed")))
    result = dict(data.get("result") or {})
    result.pop("_shutdown", None)
    return result


def _recv_all(sock: socket.socket) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


def _config_matches(meta: dict[str, Any], project: str, simset: str, mode: str, top: str) -> bool:
    return (
        str(meta.get("project") or "") == project
        and str(meta.get("simset") or "") == simset
        and str(meta.get("mode") or "") == mode
        and str(meta.get("top") or "") == top
    )


def _spawn_daemon(
    *,
    session_name: str | None,
    anchor_dir: str,
    project: str,
    simset: str,
    mode: str,
    top: str,
    daemon_log_path: str,
) -> subprocess.Popen[str]:
    Path(daemon_log_path).parent.mkdir(parents=True, exist_ok=True)
    log_file = open(daemon_log_path, "a", encoding="utf-8")
    cmd = [
        sys.executable,
        "-m",
        "xdb.cli",
        "_simd",
        "--anchor-dir",
        anchor_dir,
        "--project",
        project,
        "--simset",
        simset,
        "--mode",
        mode,
        "--top",
        top,
    ]
    if session_name:
        cmd.extend(["--session", session_name])
    try:
        return subprocess.Popen(
            cmd,
            cwd=anchor_dir,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
    finally:
        log_file.close()


def _wait_for_session(session_name: str | None, timeout: int) -> dict[str, Any]:
    paths = session_paths(session_name)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        meta = load_meta(paths)
        if meta and meta.get("state") == "error":
            raise XdbError(str(meta.get("last_error") or "simulation daemon failed to start"))
        if meta and pid_is_alive(int(meta.get("pid", 0) or 0)) and Path(str(meta.get("socket_path", ""))).exists():
            return _send_request(session_name, make_request(OP_STATUS))
        time.sleep(0.2)
    raise XdbError("timed out waiting for simulation daemon to start")


def _wait_until_stopped(session_name: str | None, timeout: float = 5.0) -> None:
    paths = session_paths(session_name)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        meta = load_meta(paths)
        if not meta:
            return
        if not Path(str(meta.get("socket_path", ""))).exists() and not pid_is_alive(
            int(meta.get("pid", 0) or 0)
        ):
            return
        time.sleep(0.1)


def launch_session(
    *,
    project: str | None,
    simset: str,
    mode: str,
    top: str | None,
    session_name: str | None,
    replace: bool,
    timeout: int,
) -> dict[str, Any]:
    paths = session_paths(session_name)
    cleanup_stale_session(paths)
    live_meta = load_meta(paths)
    effective_project = resolve_project_arg(project, paths)
    effective_top = top or str((live_meta or {}).get("top") or "")

    if live_meta and Path(str(live_meta.get("socket_path", ""))).exists() and pid_is_alive(int(live_meta.get("pid", 0) or 0)):
        if replace:
            try:
                _send_request(session_name, make_request(OP_CLOSE))
            except XdbError:
                terminate_session(live_meta, force=False)
            _wait_until_stopped(session_name, timeout=5.0)
            cleanup_stale_session(paths)
            remaining = load_meta(paths)
            if remaining and pid_is_alive(int(remaining.get("pid", 0) or 0)):
                terminate_session(remaining, force=True)
                _wait_until_stopped(session_name, timeout=2.0)
            cleanup_stale_session(paths)
        else:
            if _config_matches(live_meta, effective_project, simset, mode, effective_top):
                status = _send_request(session_name, make_request(OP_STATUS))
                status["reused"] = True
                return status
            raise XdbError(
                "a live simulation session already exists with different configuration; "
                "use --replace or choose another --session"
            )

    remove_session(paths)
    proc = _spawn_daemon(
        session_name=session_name,
        anchor_dir=str(paths.anchor_dir),
        project=effective_project,
        simset=simset,
        mode=mode,
        top=effective_top,
        daemon_log_path=str(paths.daemon_log_path),
    )
    try:
        status = _wait_for_session(session_name, timeout=timeout)
    except Exception:
        if proc.poll() is None:
            terminate_session({"pid": proc.pid, "socket_path": ""}, force=True)
        raise
    status["reused"] = False
    return status


def run_session(session_name: str | None, tokens: list[str]) -> dict[str, Any]:
    return _send_request(session_name, make_request(OP_RUN, tokens=tokens))


def restart_session(session_name: str | None) -> dict[str, Any]:
    return _send_request(session_name, make_request(OP_RESTART))


def close_session(session_name: str | None) -> dict[str, Any]:
    result = _send_request(session_name, make_request(OP_CLOSE))
    time.sleep(0.1)
    return result


def time_session(session_name: str | None) -> dict[str, Any]:
    return _send_request(session_name, make_request(OP_TIME))


def get_signal(session_name: str | None, signal: str) -> dict[str, Any]:
    return _send_request(session_name, make_request(OP_GET, signal=signal))


def get_many_signals(session_name: str | None, pattern: str) -> dict[str, Any]:
    return _send_request(session_name, make_request(OP_GET_MANY, pattern=pattern))


def get_scopes(session_name: str | None, scope: str | None) -> dict[str, Any]:
    return _send_request(session_name, make_request(OP_SCOPES, scope=scope or ""))


def get_objects(session_name: str | None, scope: str) -> dict[str, Any]:
    return _send_request(session_name, make_request(OP_OBJECTS, scope=scope))


def set_top(session_name: str | None, top: str) -> dict[str, Any]:
    return _send_request(session_name, make_request(OP_TOP, top=top))


def add_wave(session_name: str | None, pattern: str) -> dict[str, Any]:
    return _send_request(session_name, make_request(OP_WAVE_ADD, pattern=pattern))


def step_session(session_name: str | None, arg: str | None) -> dict[str, Any]:
    if arg is None or arg == "":
        return _send_request(session_name, make_request(OP_STEP, count=1))
    if arg.isdigit():
        count = int(arg)
        if count <= 0:
            raise XdbError("step count must be > 0")
        return _send_request(session_name, make_request(OP_STEP, count=count))
    tokens = arg.split()
    if not tokens:
        raise XdbError("missing step argument")
    return _send_request(session_name, make_request(OP_STEP, time_tokens=tokens))


def add_breakpoint(session_name: str | None, condition: str) -> dict[str, Any]:
    return _send_request(session_name, make_request(OP_BREAKPOINT_ADD, condition=condition))


def clear_breakpoints(session_name: str | None) -> dict[str, Any]:
    return _send_request(session_name, make_request(OP_BREAKPOINT_CLEAR))
