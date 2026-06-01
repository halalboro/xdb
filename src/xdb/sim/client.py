from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, cast

from xdb.errors import XdbError
from xdb.sim.axis_trace import collect_axis_trace
from xdb.sim.diagnostics import doctor_session as doctor_session
from xdb.sim.diagnostics import provenance_session as provenance_session
from xdb.sim.session_status import config_matches, launch_spec_summary
from xdb.sim.with_trace_client import parse_with_trace_command
from xdb.sim.exec_env import (
    derive_sim_exec_env,
    normalize_remainder_command,
    parse_env_overrides,
    resolve_exec_cwd,
)
from xdb.sim.protocol import (
    OP_ASSERT_SIGNAL,
    OP_ASSERT_TCL,
    OP_BREAKPOINT_ADD,
    OP_BREAKPOINT_CLEAR,
    OP_BREAKPOINT_LIST,
    OP_BREAKPOINT_REMOVE,
    OP_CLEAR_COMPLETED,
    OP_CLOSE,
    OP_COMPLETED,
    OP_COYOTE_STATUS,
    OP_CSR_READ,
    OP_CSR_WRITE,
    OP_DESCRIBE,
    OP_EXPECT_CHANGE,
    OP_EXPECT_CONDITION,
    OP_EXPECT_SIGNAL,
    OP_FORCE,
    OP_GET,
    OP_GET_MANY,
    OP_INVOKE,
    OP_IRQ_WAIT,
    OP_MEM_LIST,
    OP_MEM_MAP,
    OP_MEM_READ,
    OP_MEM_RESET,
    OP_MEM_UNMAP,
    OP_MEM_WRITE,
    OP_OBJECTS,
    OP_READ_SIGNALS,
    OP_RELEASE,
    OP_RESTART,
    OP_RUN,
    OP_SCOPES,
    OP_SOURCE,
    OP_SNAPSHOT,
    OP_STATUS,
    OP_STEP,
    OP_TCL,
    OP_TIME,
    OP_TOP,
    OP_TRACE_EVENTS_CLEAR,
    OP_TRACE_EVENTS_GET,
    OP_TRACE_TRANSACTIONS,
    OP_UNTIL,
    OP_WITH_TRACE,
    OP_UNTIL_SIGNAL,
    OP_VCD_START,
    OP_VCD_STATUS,
    OP_VCD_STOP,
    OP_WATCH_CHANGES,
    OP_WAVE_ADD,
    OP_DIFF_SNAPSHOT,
    make_request,
)
from xdb.sim.session_store import (
    cleanup_stale_session,
    is_live_session,
    load_meta,
    pid_is_alive,
    remove_session,
    remove_workspace_tree,
    require_live_meta,
    resolve_launch_spec,
    resolve_mode_arg,
    resolve_simset_arg,
    resolve_top_arg,
    session_paths,
    terminate_session,
)
from xdb.sim.types import SessionMeta, SimRequest


def _send_request(
    session_name: str | None,
    request: SimRequest,
    *,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    paths = session_paths(session_name)
    meta = require_live_meta(paths)
    sock_path = str(meta.get("socket_path") or "")
    if not sock_path:
        raise XdbError(
            f"simulation session socket missing for {paths.session_name!r}; run 'xdb sim launch' again"
        )
    op = str(request.get("op") or "request")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        if timeout_seconds is not None:
            if timeout_seconds <= 0:
                raise XdbError("request timeout must be > 0")
            sock.settimeout(timeout_seconds)
        try:
            sock.connect(sock_path)
            sock.sendall(json.dumps(request).encode("utf-8"))
            sock.shutdown(socket.SHUT_WR)
            response = _recv_all(sock)
        except FileNotFoundError as e:
            raise XdbError(
                f"simulation session socket missing for {paths.session_name!r}; run 'xdb sim launch' again"
            ) from e
        except TimeoutError as e:
            raise XdbError(_request_timeout_message(paths.session_name, op, timeout_seconds)) from e
    if not response:
        raise XdbError(f"simulation daemon returned an empty response for {op!r}")
    data = cast(dict[str, Any], json.loads(response.decode("utf-8")))
    if not data.get("ok", False):
        raise XdbError(str(data.get("error", "simulation request failed")))
    result = dict(data.get("result") or {})
    result.pop("_shutdown", None)
    return result


def _emit_stream_line(stream_name: str, data: str) -> None:
    prefix = f"[host {stream_name}] "
    lines = data.splitlines(keepends=True) or [""]
    for line in lines:
        sys.stderr.write(prefix + line)
        if line and not line.endswith("\n"):
            sys.stderr.write("\n")
    sys.stderr.flush()


def _request_timeout_message(session_name: str, op: str, timeout_seconds: float | None) -> str:
    timeout_text = "the configured timeout" if timeout_seconds is None else f"{timeout_seconds:g}s"
    return (
        f"timed out after {timeout_text} waiting for simulation daemon response to {op!r} "
        f"in session {session_name!r}. The daemon may still be busy or stuck in Vivado; "
        "try 'xdb sim time' to check responsiveness, or recover with "
        "'xdb sim close' / 'xdb sim relaunch --fresh'."
    )


def _send_streaming_request(session_name: str | None, request: SimRequest) -> dict[str, Any]:
    paths = session_paths(session_name)
    meta = require_live_meta(paths)
    sock_path = str(meta.get("socket_path") or "")
    if not sock_path:
        raise XdbError(
            f"simulation session socket missing for {paths.session_name!r}; run 'xdb sim launch' again"
        )
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        try:
            sock.connect(sock_path)
            sock.sendall(json.dumps(request).encode("utf-8"))
            sock.shutdown(socket.SHUT_WR)
            buffer = ""
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buffer += chunk.decode("utf-8")
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    if not line:
                        continue
                    frame = cast(dict[str, Any], json.loads(line))
                    frame_type = str(frame.get("type") or "")
                    if frame_type == "stream":
                        _emit_stream_line(str(frame.get("stream") or "stdout"), str(frame.get("data") or ""))
                        continue
                    if frame_type == "response":
                        response = cast(dict[str, Any], frame.get("response") or {})
                        if not response.get("ok", False):
                            raise XdbError(str(response.get("error", "simulation request failed")))
                        result = dict(response.get("result") or {})
                        result.pop("_shutdown", None)
                        return result
            if buffer.strip():
                frame = cast(dict[str, Any], json.loads(buffer))
                if str(frame.get("type") or "") == "response":
                    response = cast(dict[str, Any], frame.get("response") or {})
                    if not response.get("ok", False):
                        raise XdbError(str(response.get("error", "simulation request failed")))
                    result = dict(response.get("result") or {})
                    result.pop("_shutdown", None)
                    return result
        except FileNotFoundError as e:
            raise XdbError(
                f"simulation session socket missing for {paths.session_name!r}; run 'xdb sim launch' again"
            ) from e
    raise XdbError("simulation daemon closed streaming connection without a final response")


def _recv_all(sock: socket.socket) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _spawn_daemon(
    *,
    session_name: str | None,
    anchor_dir: str,
    launch_spec: Mapping[str, object],
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
        if meta and pid_is_alive(int(meta.get("pid", 0) or 0)) and Path(
            str(meta.get("socket_path") or "")
        ).exists():
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


def _get_live_meta(session_name: str | None) -> SessionMeta | None:
    paths = session_paths(session_name)
    cleanup_stale_session(paths)
    meta = load_meta(paths)
    if not is_live_session(meta):
        return None
    return meta


def _terminate_cached_session(
    session_name: str | None,
    meta: SessionMeta,
    *,
    wait_seconds: float = 5.0,
) -> dict[str, Any]:
    paths = session_paths(session_name)
    pid = int(meta.get("pid", 0) or 0)
    was_alive = pid_is_alive(pid)
    if was_alive:
        terminate_session(meta, force=False)
        _wait_until_stopped(session_name, timeout=wait_seconds)
    cleanup_stale_session(paths)
    remaining = load_meta(paths)
    force_killed = False
    if remaining and pid_is_alive(int(remaining.get("pid", 0) or 0)):
        terminate_session(remaining, force=True)
        force_killed = True
        _wait_until_stopped(session_name, timeout=2.0)
    remove_session(paths)
    return {
        "pid": pid or None,
        "was_alive": was_alive,
        "force_killed": force_killed,
        "session_removed": not paths.session_dir.exists(),
    }


def _close_live_session(session_name: str | None, meta: SessionMeta) -> None:
    try:
        _send_request(session_name, make_request(OP_CLOSE), timeout_seconds=5.0)
    except XdbError:
        _terminate_cached_session(session_name, meta)
        return
    _wait_until_stopped(session_name, timeout=5.0)
    paths = session_paths(session_name)
    cleanup_stale_session(paths)
    remaining = load_meta(paths)
    if remaining and pid_is_alive(int(remaining.get("pid", 0) or 0)):
        _terminate_cached_session(session_name, remaining)
    cleanup_stale_session(paths)


def launch_session(
    *,
    simset: str | None,
    mode: str | None,
    top: str | None,
    session_name: str | None,
    replace: bool,
    timeout: int,
    package_runtime: str | None = None,
) -> dict[str, Any]:
    paths = session_paths(session_name)
    cleanup_stale_session(paths)
    live_meta = load_meta(paths)
    effective_simset = resolve_simset_arg(simset)
    effective_mode = resolve_mode_arg(mode)
    effective_top = resolve_top_arg(top, live_meta)
    launch_spec = resolve_launch_spec(stage=False, package_runtime=package_runtime)

    if live_meta and Path(str(live_meta.get("socket_path", ""))).exists() and pid_is_alive(int(live_meta.get("pid", 0) or 0)):
        if replace:
            _close_live_session(session_name, live_meta)
        else:
            if bool(launch_spec.get("needs_stage")):
                raise XdbError(
                    "the packaged simulation input changed and the writable workspace needs "
                    "to be refreshed; use --replace or close the current session first"
                )
            if config_matches(live_meta, launch_spec, effective_simset, effective_mode, effective_top):
                status = _send_request(session_name, make_request(OP_STATUS))
                status["reused"] = True
                status.update(launch_spec_summary(launch_spec))
                return status
            raise XdbError(
                "a live simulation session already exists with different configuration; "
                "use --replace or choose another --session"
            )

    launch_spec = resolve_launch_spec(stage=True, package_runtime=package_runtime)

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
    status.update(launch_spec_summary(launch_spec))
    return status


def relaunch_session(
    *,
    simset: str | None,
    mode: str | None,
    top: str | None,
    session_name: str | None,
    timeout: int,
    fresh: bool = True,
) -> dict[str, Any]:
    live_meta = _get_live_meta(session_name)
    if live_meta is not None:
        _close_live_session(session_name, live_meta)
    removed_workspace = False
    if fresh:
        launch_spec = resolve_launch_spec(stage=False)
        removed_workspace = remove_workspace_tree(str(launch_spec.get("workspace") or ""))
    status = launch_session(
        simset=simset,
        mode=mode,
        top=top,
        session_name=session_name,
        replace=True,
        timeout=timeout,
    )
    status["fresh"] = bool(fresh)
    status["workspace_removed"] = removed_workspace
    return status


def restage_session(session_name: str | None) -> dict[str, Any]:
    if _get_live_meta(session_name) is not None:
        raise XdbError(
            "cannot restage while a live simulation session is running; "
            "close it first or use 'xdb sim relaunch --fresh'"
        )
    launch_spec = resolve_launch_spec(stage=False)
    removed_workspace = remove_workspace_tree(str(launch_spec.get("workspace") or ""))
    staged_spec = resolve_launch_spec(stage=True)
    provenance = provenance_session(session_name)
    return {
        "restaged": True,
        "workspace_removed": removed_workspace,
        **launch_spec_summary(staged_spec),
        "provenance": provenance,
    }


def axis_trace_session(
    session_name: str | None,
    interface_paths: list[str],
    duration_tokens: list[str],
    *,
    step_tokens: list[str],
    decode_bytes: bool = False,
    lane_order: str = "low-to-high",
    include_idle: bool = False,
    only_handshakes: bool = False,
) -> dict[str, Any]:
    class _ClientAxisTraceDriver:
        def objects(self, scope: str) -> dict[str, Any]:
            return get_objects(session_name, scope)

        def read_signals(self, signals: list[str]) -> dict[str, Any]:
            return read_signals(session_name, signals)

        def time(self) -> dict[str, Any]:
            return time_session(session_name)

        def run(self, tokens: list[str]) -> dict[str, Any]:
            return run_session(session_name, tokens)

    return collect_axis_trace(
        _ClientAxisTraceDriver(),
        interface_paths,
        duration_tokens,
        step_tokens=step_tokens,
        decode_bytes=decode_bytes,
        lane_order=lane_order,
        include_idle=include_idle,
        only_handshakes=only_handshakes,
    )


def run_session(
    session_name: str | None,
    tokens: list[str],
    *,
    timeout_seconds: float | None = 30.0,
) -> dict[str, Any]:
    client_timeout = None if timeout_seconds is None else timeout_seconds + 5.0
    return _send_request(
        session_name,
        make_request(OP_RUN, tokens=tokens, timeout_seconds=timeout_seconds),
        timeout_seconds=client_timeout,
    )


def restart_session(session_name: str | None) -> dict[str, Any]:
    return _send_request(session_name, make_request(OP_RESTART))


def close_session(
    session_name: str | None,
    *,
    force: bool = False,
    timeout_seconds: float | None = 5.0,
) -> dict[str, Any]:
    paths = session_paths(session_name)
    meta = load_meta(paths)
    if force:
        if meta is None:
            remove_session(paths)
            return {
                "closed": True,
                "force": True,
                "session": paths.session_name,
                "session_id": paths.session_id,
                "pid": None,
                "was_alive": False,
                "force_killed": False,
                "session_removed": not paths.session_dir.exists(),
            }
        termination = _terminate_cached_session(session_name, meta, wait_seconds=1.0)
        return {
            "closed": True,
            "force": True,
            "session": paths.session_name,
            "session_id": paths.session_id,
            **termination,
        }

    result = _send_request(session_name, make_request(OP_CLOSE), timeout_seconds=timeout_seconds)
    _wait_until_stopped(session_name, timeout=timeout_seconds or 5.0)
    cleanup_stale_session(paths)
    remaining = load_meta(paths)
    terminated = None
    if remaining and pid_is_alive(int(remaining.get("pid", 0) or 0)):
        terminated = _terminate_cached_session(session_name, remaining)
    time.sleep(0.1)
    result["force"] = False
    result["session_removed"] = not paths.session_dir.exists()
    if terminated is not None:
        result["terminated_after_close"] = terminated
    return result


def time_session(session_name: str | None) -> dict[str, Any]:
    return _send_request(session_name, make_request(OP_TIME))


def status_session(session_name: str | None) -> dict[str, Any]:
    return _send_request(session_name, make_request(OP_STATUS))


def describe_session(session_name: str | None) -> dict[str, Any]:
    return _send_request(session_name, make_request(OP_DESCRIBE))


def get_signal(session_name: str | None, signal: str) -> dict[str, Any]:
    return _send_request(session_name, make_request(OP_GET, signal=signal))


def get_many_signals(session_name: str | None, pattern: str) -> dict[str, Any]:
    return _send_request(session_name, make_request(OP_GET_MANY, pattern=pattern))


def read_signals(session_name: str | None, signals: list[str]) -> dict[str, Any]:
    return _send_request(session_name, make_request(OP_READ_SIGNALS, signals=signals))


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


def assert_signal_session(
    session_name: str | None,
    signal: str,
    value: str,
) -> dict[str, Any]:
    return _send_request(
        session_name,
        make_request(OP_ASSERT_SIGNAL, signal=signal, value=value),
    )


def assert_tcl_session(session_name: str | None, expr: str) -> dict[str, Any]:
    return _send_request(session_name, make_request(OP_ASSERT_TCL, expr=expr))


def expect_signal_session(
    session_name: str | None,
    signal: str,
    value: str,
    within_tokens: list[str],
) -> dict[str, Any]:
    return _send_request(
        session_name,
        make_request(
            OP_EXPECT_SIGNAL,
            signal=signal,
            value=value,
            within_tokens=within_tokens,
        ),
    )


def expect_change_session(
    session_name: str | None,
    signal: str,
    within_tokens: list[str],
) -> dict[str, Any]:
    return _send_request(
        session_name,
        make_request(OP_EXPECT_CHANGE, signal=signal, within_tokens=within_tokens),
    )


def expect_condition_session(
    session_name: str | None,
    expr: str,
    within_tokens: list[str],
) -> dict[str, Any]:
    if not expr:
        raise XdbError("missing Tcl expression")
    return _send_request(
        session_name,
        make_request(OP_EXPECT_CONDITION, expr=expr, within_tokens=within_tokens),
    )


def expect_stream_output_session(
    session_name: str | None,
    interface_path: str,
    within_tokens: list[str],
    *,
    step_tokens: list[str],
    decode_bytes: bool = False,
    lane_order: str = "low-to-high",
) -> dict[str, Any]:
    result = axis_trace_session(
        session_name,
        [interface_path],
        within_tokens,
        step_tokens=step_tokens,
        decode_bytes=decode_bytes,
        lane_order=lane_order,
        include_idle=False,
        only_handshakes=True,
    )
    records = [record for record in list(result.get("records") or []) if isinstance(record, dict)]
    if not records:
        raise XdbError(
            f"expect-stream-output failed: no AXIS handshake observed on {interface_path} "
            f"within {' '.join(within_tokens)}"
        )
    return {
        "passed": True,
        "kind": "expect-stream-output",
        "interface": interface_path,
        "within": " ".join(within_tokens),
        "step": " ".join(step_tokens),
        "record_count": len(records),
        "first_record": records[0],
        "trace": result,
    }


def add_breakpoint(
    session_name: str | None,
    condition: str,
    *,
    poll_step_tokens: list[str] | None = None,
) -> dict[str, Any]:
    return _send_request(
        session_name,
        make_request(OP_BREAKPOINT_ADD, condition=condition, poll_step_tokens=list(poll_step_tokens or [])),
    )


def list_breakpoints(session_name: str | None) -> dict[str, Any]:
    return _send_request(session_name, make_request(OP_BREAKPOINT_LIST))


def remove_breakpoint(session_name: str | None, breakpoint_id: int) -> dict[str, Any]:
    if breakpoint_id <= 0:
        raise XdbError("breakpoint id must be > 0")
    return _send_request(session_name, make_request(OP_BREAKPOINT_REMOVE, breakpoint_id=breakpoint_id))


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


def coyote_status_session(session_name: str | None) -> dict[str, Any]:
    return _send_request(session_name, make_request(OP_COYOTE_STATUS))


def coyote_csr_read_session(
    session_name: str | None,
    addr: int,
    *,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    return _send_request(
        session_name,
        make_request(OP_CSR_READ, addr=addr, timeout_seconds=timeout_seconds),
    )


def coyote_csr_write_session(
    session_name: str | None,
    addr: int,
    value: int,
) -> dict[str, Any]:
    return _send_request(session_name, make_request(OP_CSR_WRITE, addr=addr, value=value))


def coyote_mem_map_session(
    session_name: str | None,
    space: str,
    addr: int,
    size: int,
) -> dict[str, Any]:
    return _send_request(
        session_name,
        make_request(OP_MEM_MAP, space=space, addr=addr, size=size),
    )


def coyote_mem_unmap_session(
    session_name: str | None,
    space: str,
    addr: int,
) -> dict[str, Any]:
    return _send_request(
        session_name,
        make_request(OP_MEM_UNMAP, space=space, addr=addr),
    )


def coyote_mem_list_session(
    session_name: str | None,
    space: str = "host",
) -> dict[str, Any]:
    return _send_request(
        session_name,
        make_request(OP_MEM_LIST, space=space),
    )


def coyote_mem_reset_session(
    session_name: str | None,
    space: str = "host",
) -> dict[str, Any]:
    return _send_request(
        session_name,
        make_request(OP_MEM_RESET, space=space),
    )


def coyote_mem_write_session(
    session_name: str | None,
    space: str,
    addr: int,
    data_hex: str,
) -> dict[str, Any]:
    return _send_request(
        session_name,
        make_request(OP_MEM_WRITE, space=space, addr=addr, data_hex=data_hex),
    )


def coyote_mem_read_session(
    session_name: str | None,
    space: str,
    addr: int,
    size: int,
) -> dict[str, Any]:
    return _send_request(
        session_name,
        make_request(OP_MEM_READ, space=space, addr=addr, size=size),
    )


def coyote_invoke_session(
    session_name: str | None,
    **kwargs: Any,
) -> dict[str, Any]:
    return _send_request(session_name, make_request(OP_INVOKE, **kwargs))


def coyote_completed_session(
    session_name: str | None,
    opcode: str,
    *,
    target_count: int | None = None,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    return _send_request(
        session_name,
        make_request(
            OP_COMPLETED,
            opcode=opcode,
            target_count=target_count,
            timeout_seconds=timeout_seconds,
        ),
    )


def coyote_clear_completed_session(session_name: str | None) -> dict[str, Any]:
    return _send_request(session_name, make_request(OP_CLEAR_COMPLETED))


def coyote_irq_wait_session(
    session_name: str | None,
    *,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    return _send_request(
        session_name,
        make_request(OP_IRQ_WAIT, timeout_seconds=timeout_seconds),
    )


def trace_transactions_session(
    session_name: str | None,
    duration_tokens: list[str],
    *,
    opcode: str | None = None,
) -> dict[str, Any]:
    return _send_request(
        session_name,
        make_request(
            OP_TRACE_TRANSACTIONS,
            duration_tokens=duration_tokens,
            opcode=opcode or "",
        ),
    )


def trace_events_clear_session(session_name: str | None) -> dict[str, Any]:
    return _send_request(session_name, make_request(OP_TRACE_EVENTS_CLEAR))


def trace_events_get_session(session_name: str | None) -> dict[str, Any]:
    return _send_request(session_name, make_request(OP_TRACE_EVENTS_GET))


def _start_stream_reader(
    pipe,
    stream_name: str,
    chunks: list[str],
    *,
    stream_output: bool,
) -> threading.Thread:
    def read_pipe() -> None:
        try:
            for line in pipe:
                chunks.append(line)
                if stream_output:
                    _emit_stream_line(stream_name, line)
        finally:
            pipe.close()

    thread = threading.Thread(target=read_pipe, daemon=True)
    thread.start()
    return thread


def exec_session(
    session_name: str | None,
    command: list[str],
    *,
    cwd: str | None = None,
    env_overrides: list[str] | None = None,
    timeout_seconds: float | None = None,
    expect_exit_code: int = 0,
    clean_env: bool = False,
    stream_output: bool = False,
) -> dict[str, Any]:
    argv = normalize_remainder_command(command)
    paths = session_paths(session_name)
    meta = require_live_meta(paths)
    session_env = derive_sim_exec_env(meta, paths.session_name)
    overrides = parse_env_overrides(list(env_overrides or []))
    run_env = {} if clean_env else dict(os.environ)
    run_env.update(session_env)
    run_env.update(overrides)
    reported_env = {**session_env, **overrides}
    run_cwd = resolve_exec_cwd(cwd, meta, paths.anchor_dir)
    started_at = _now_iso()
    started_seconds = time.time()
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    try:
        proc = subprocess.Popen(
            argv,
            cwd=run_cwd,
            env=run_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError as e:
        raise XdbError(f"command not found: {argv[0]}") from e
    assert proc.stdout is not None
    assert proc.stderr is not None
    stdout_thread = _start_stream_reader(
        proc.stdout,
        "stdout",
        stdout_chunks,
        stream_output=stream_output,
    )
    stderr_thread = _start_stream_reader(
        proc.stderr,
        "stderr",
        stderr_chunks,
        stream_output=stream_output,
    )
    timed_out = False
    try:
        exit_code: int | None = int(proc.wait(timeout=timeout_seconds))
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.kill()
        exit_code = None
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
    stdout_thread.join(timeout=5)
    stderr_thread.join(timeout=5)
    finished_seconds = time.time()
    stdout = "".join(stdout_chunks)
    stderr = "".join(stderr_chunks)
    finished_at = _now_iso()
    ok = (not timed_out) and exit_code == expect_exit_code
    return {
        "ok": ok,
        "timed_out": timed_out,
        "exit_code": exit_code,
        "expected_exit_code": expect_exit_code,
        "argv": argv,
        "cwd": run_cwd,
        "env": reported_env,
        "stdout": stdout,
        "stderr": stderr,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": max(0.0, finished_seconds - started_seconds),
        "streamed": stream_output,
        "session": {
            "name": paths.session_name,
            "id": paths.session_id,
            "runtime_root": str(meta.get("runtime_root") or ""),
            "work_dir": str(meta.get("work_dir") or ""),
            "socket_path": str(meta.get("socket_path") or ""),
            "state": str(meta.get("state") or ""),
        },
    }


def with_trace_session(
    session_name: str | None,
    command: list[str],
    duration_tokens: list[str] | None,
    *,
    step_tokens: list[str],
    transactions: bool = False,
    axis_paths: list[str] | None = None,
    decode_bytes: bool = False,
    lane_order: str = "low-to-high",
    include_idle: bool = False,
    only_handshakes: bool = False,
    correlate_by: str = "nearest",
    correlate_window_tokens: list[str] | None = None,
    exec_mode: bool = False,
    exec_until_exit: bool = False,
    exec_cwd: str | None = None,
    exec_env_overrides: list[str] | None = None,
    exec_timeout_seconds: float | None = None,
    exec_expect_exit_code: int = 0,
    exec_clean_env: bool = False,
    exec_stream_output: bool = False,
) -> dict[str, Any]:
    axis_paths = list(axis_paths or [])
    duration_tokens = list(duration_tokens or [])
    if not transactions and not axis_paths:
        raise XdbError("with-trace requires at least one trace mode")
    if exec_until_exit and not exec_mode:
        raise XdbError("--exec-until-exit requires --exec")
    if not duration_tokens and not exec_until_exit:
        raise XdbError("with-trace requires --for unless --exec-until-exit is used with --exec")
    request_args: dict[str, Any] = {"exec_until_exit": exec_until_exit}
    if exec_mode:
        request_args.update(
            exec_command=normalize_remainder_command(command),
            exec_cwd=exec_cwd or "",
            exec_env_overrides=list(exec_env_overrides or []),
            exec_timeout_seconds=exec_timeout_seconds,
            exec_expect_exit_code=exec_expect_exit_code,
            exec_clean_env=exec_clean_env,
            exec_stream_output=exec_stream_output,
            exec_base_env={} if exec_clean_env else dict(os.environ),
        )
    else:
        request_args["action_request"] = parse_with_trace_command(command)
    request = make_request(
        OP_WITH_TRACE,
        **request_args,
        duration_tokens=duration_tokens,
        step_tokens=step_tokens,
        transactions=transactions,
        axis_paths=axis_paths,
        decode_bytes=decode_bytes,
        lane_order=lane_order,
        include_idle=include_idle,
        only_handshakes=only_handshakes,
        correlate_by=correlate_by,
        correlate_window_tokens=list(correlate_window_tokens or []),
    )
    if exec_stream_output:
        return _send_streaming_request(session_name, request)
    return _send_request(session_name, request)


def snapshot_session(
    session_name: str | None,
    scope: str,
    *,
    name: str | None = None,
) -> dict[str, Any]:
    return _send_request(session_name, make_request(OP_SNAPSHOT, scope=scope, name=name or ""))


def diff_snapshot_session(
    session_name: str | None,
    before: str,
    after: str,
) -> dict[str, Any]:
    return _send_request(
        session_name,
        make_request(OP_DIFF_SNAPSHOT, before=before, after=after),
    )


def watch_changes_session(
    session_name: str | None,
    scope: str,
    duration_tokens: list[str],
) -> dict[str, Any]:
    return _send_request(
        session_name,
        make_request(OP_WATCH_CHANGES, scope=scope, duration_tokens=duration_tokens),
    )


def vcd_start_session(
    session_name: str | None,
    path: str,
    scope: str | None = None,
) -> dict[str, Any]:
    resolved = Path(path).expanduser().resolve()
    return _send_request(
        session_name,
        make_request(OP_VCD_START, path=str(resolved), scope=scope or ""),
    )


def vcd_stop_session(session_name: str | None) -> dict[str, Any]:
    return _send_request(session_name, make_request(OP_VCD_STOP))


def vcd_status_session(session_name: str | None) -> dict[str, Any]:
    return _send_request(session_name, make_request(OP_VCD_STATUS))
