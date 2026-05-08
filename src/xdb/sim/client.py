from __future__ import annotations

import json
import os
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
    OP_FORCE,
    OP_RELEASE,
    OP_SOURCE,
    OP_STEP,
    OP_TIME,
    OP_TCL,
    OP_TOP,
    OP_UNTIL,
    OP_UNTIL_SIGNAL,
    OP_WAVE_ADD,
    make_request,
)
from .session_store import (
    cleanup_stale_session,
    load_meta,
    pid_is_alive,
    remove_session,
    require_live_meta,
    resolve_launch_spec,
    resolve_mode_arg,
    resolve_simset_arg,
    resolve_top_arg,
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


def _config_matches(meta: dict[str, Any], launch_spec: dict[str, Any], simset: str, mode: str, top: str) -> bool:
    return (
        str(meta.get("launch_kind") or "") == "runtime"
        and str(meta.get("package_runtime") or "") == str(launch_spec.get("package_runtime") or "")
        and str(meta.get("simset") or "") == simset
        and str(meta.get("mode") or "") == mode
        and str(meta.get("top") or "") == top
    )


def _spawn_daemon(
    *,
    session_name: str | None,
    anchor_dir: str,
    launch_spec: dict[str, Any],
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
        str(launch_spec.get("project") or ""),
        "--simset",
        simset,
        "--mode",
        mode,
        "--top",
        top,
    ]
    if session_name:
        cmd.extend(["--session", session_name])
    for name in ("package_runtime", "runtime_root", "work_dir", "compile_script", "elaborate_script", "simulate_script"):
        value = launch_spec.get(name)
        if value:
            cmd.extend([f"--{name.replace('_', '-')}", str(value)])
    debug_env = os.environ.get("XDB_DEBUG") or os.environ.get("XDB_VERBOSE")
    if debug_env and debug_env.strip().lower() not in {"", "0", "false", "no", "off"}:
        cmd.append("--debug")
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
    simset: str | None,
    mode: str | None,
    top: str | None,
    session_name: str | None,
    replace: bool,
    timeout: int,
) -> dict[str, Any]:
    paths = session_paths(session_name)
    cleanup_stale_session(paths)
    live_meta = load_meta(paths)
    effective_simset = resolve_simset_arg(simset)
    effective_mode = resolve_mode_arg(mode)
    effective_top = resolve_top_arg(top, live_meta)
    launch_spec = resolve_launch_spec(stage=False)

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
            if bool(launch_spec.get("needs_stage")):
                raise XdbError(
                    "the packaged simulation input changed and the writable workspace needs "
                    "to be refreshed; use --replace or close the current session first"
                )
            if _config_matches(live_meta, launch_spec, effective_simset, effective_mode, effective_top):
                status = _send_request(session_name, make_request(OP_STATUS))
                status["reused"] = True
                for key in (
                    "launch_kind",
                    "project",
                    "package_runtime",
                    "runtime_root",
                    "workspace",
                    "work_dir",
                    "compile_script",
                    "elaborate_script",
                    "simulate_script",
                    "staged",
                    "workspace_reused",
                ):
                    if key in launch_spec:
                        status[key] = launch_spec[key]
                return status
            raise XdbError(
                "a live simulation session already exists with different configuration; "
                "use --replace or choose another --session"
            )

    launch_spec = resolve_launch_spec(stage=True)

    remove_session(paths)
    proc = _spawn_daemon(
        session_name=session_name,
        anchor_dir=str(paths.anchor_dir),
        launch_spec=launch_spec,
        simset=effective_simset,
        mode=effective_mode,
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
    for key in (
        "launch_kind",
        "project",
        "package_runtime",
        "runtime_root",
        "workspace",
        "work_dir",
        "compile_script",
        "elaborate_script",
        "simulate_script",
        "staged",
        "workspace_reused",
    ):
        if key in launch_spec:
            status[key] = launch_spec[key]
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


def wait_until_session(
    session_name: str | None,
    expr: str,
    step_tokens: list[str],
    *,
    timeout_seconds: float | None = None,
    max_iterations: int | None = None,
) -> dict[str, Any]:
    return _send_request(
        session_name,
        make_request(
            OP_UNTIL,
            expr=expr,
            step_tokens=step_tokens,
            timeout_seconds=timeout_seconds,
            max_iterations=max_iterations,
        ),
    )


def wait_until_signal_session(
    session_name: str | None,
    signal: str,
    value: str,
    step_tokens: list[str],
    *,
    timeout_seconds: float | None = None,
    max_iterations: int | None = None,
) -> dict[str, Any]:
    return _send_request(
        session_name,
        make_request(
            OP_UNTIL_SIGNAL,
            signal=signal,
            value=value,
            step_tokens=step_tokens,
            timeout_seconds=timeout_seconds,
            max_iterations=max_iterations,
        ),
    )


def add_breakpoint(session_name: str | None, condition: str) -> dict[str, Any]:
    return _send_request(session_name, make_request(OP_BREAKPOINT_ADD, condition=condition))


def clear_breakpoints(session_name: str | None) -> dict[str, Any]:
    return _send_request(session_name, make_request(OP_BREAKPOINT_CLEAR))


def tcl_session(session_name: str | None, script: str) -> dict[str, Any]:
    return _send_request(session_name, make_request(OP_TCL, script=script))


def source_session(session_name: str | None, path: str) -> dict[str, Any]:
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise XdbError(f"Tcl script not found: {resolved}")
    return _send_request(session_name, make_request(OP_SOURCE, path=str(resolved)))


def force_session(
    session_name: str | None,
    signal: str,
    values: list[str],
    *,
    radix: str | None = None,
    repeat_every: str | None = None,
    cancel_after: str | None = None,
) -> dict[str, Any]:
    if not signal:
        raise XdbError("missing signal")
    if not values:
        raise XdbError("missing force value")
    return _send_request(
        session_name,
        make_request(
            OP_FORCE,
            signal=signal,
            values=values,
            radix=radix or "",
            repeat_every=repeat_every or "",
            cancel_after=cancel_after or "",
        ),
    )


def release_session(
    session_name: str | None,
    signal: str | None,
    *,
    all_forces: bool = False,
) -> dict[str, Any]:
    if all_forces:
        return _send_request(session_name, make_request(OP_RELEASE, all=True))
    if not signal:
        raise XdbError("missing signal; pass a signal path or --all")
    return _send_request(session_name, make_request(OP_RELEASE, signal=signal, all=False))
