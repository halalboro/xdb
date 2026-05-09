from __future__ import annotations

import argparse
import json
import math
import os
import re
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, NoReturn, cast

from xdb.errors import XdbError
from xdb.sim.coyote import parse_hex_bytes
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
    OP_CLEAR_COMPLETED,
    OP_CLOSE,
    OP_COMPLETED,
    OP_COYOTE_STATUS,
    OP_CSR_READ,
    OP_CSR_WRITE,
    OP_DESCRIBE,
    OP_EXPECT_CHANGE,
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
    read_runtime_stage_stamp,
    remove_session,
    remove_workspace_tree,
    require_live_meta,
    resolve_launch_spec,
    resolve_mode_arg,
    resolve_simset_arg,
    resolve_top_arg,
    session_paths,
    terminate_session,
    tree_fingerprint,
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


_SIM_TIME_UNITS = {
    "fs": Decimal("1e-15"),
    "ps": Decimal("1e-12"),
    "ns": Decimal("1e-9"),
    "us": Decimal("1e-6"),
    "ms": Decimal("1e-3"),
    "s": Decimal("1"),
}

_AXIS_REQUIRED_SIGNALS = ("tvalid", "tready", "tdata")
_AXIS_OPTIONAL_SIGNALS = ("tkeep", "tlast", "tid")


def _parse_sim_time(text: str) -> Decimal:
    normalized = text.strip()
    match = re.match(r"^([0-9]+(?:\.[0-9]*)?)\s*([a-zA-Z]+)$", normalized)
    if not match:
        raise XdbError(f"unsupported simulation time format: {text!r}")
    value = Decimal(match.group(1))
    unit = match.group(2).lower()
    if unit not in _SIM_TIME_UNITS:
        raise XdbError(f"unsupported simulation time unit: {unit!r}")
    return value * _SIM_TIME_UNITS[unit]


def _parse_duration_tokens(tokens: list[str]) -> tuple[str, Decimal]:
    joined = " ".join(token.strip() for token in tokens if token.strip())
    if not joined:
        raise XdbError("missing duration")
    return joined, _parse_sim_time(joined)


def _parse_logic_int(value: str) -> tuple[int | None, int | None]:
    normalized = value.strip().replace("_", "")
    if not normalized:
        return None, None
    sized = re.match(r"^([0-9]+)'([bBoOdDhH])([0-9a-fA-FxXzZ]+)$", normalized)
    if sized:
        width = int(sized.group(1))
        radix = sized.group(2).lower()
        digits = sized.group(3)
        if re.search(r"[xXzZ]", digits):
            return None, width
        base = {"b": 2, "o": 8, "d": 10, "h": 16}[radix]
        return int(digits, base), width
    prefixed = re.match(r"^0([boxd])([0-9a-fA-F]+)$", normalized, re.IGNORECASE)
    if prefixed:
        radix = prefixed.group(1).lower()
        digits = prefixed.group(2)
        base = {"b": 2, "o": 8, "d": 10, "x": 16}.get(radix)
        if base is None:
            return None, None
        return int(digits, base), None
    if re.search(r"[xXzZ]", normalized):
        return None, None
    if re.fullmatch(r"[01]+", normalized):
        return int(normalized, 2), len(normalized)
    if re.fullmatch(r"[0-9]+", normalized):
        return int(normalized, 10), None
    if re.fullmatch(r"[0-9a-fA-F]+", normalized):
        return int(normalized, 16), None
    return None, None


def _axis_child_signal_map(session_name: str | None, interface_path: str) -> dict[str, dict[str, Any]]:
    result = get_objects(session_name, interface_path)
    metadata = [
        cast(dict[str, Any], item)
        for item in list(result.get("metadata") or [])
        if isinstance(item, dict)
    ]
    signal_map: dict[str, dict[str, Any]] = {}
    for item in metadata:
        path = str(item.get("path") or "")
        base = path.rsplit("/", 1)[-1].lower()
        if base in {*_AXIS_REQUIRED_SIGNALS, *_AXIS_OPTIONAL_SIGNALS}:
            signal_map[base] = item
    missing = [name for name in _AXIS_REQUIRED_SIGNALS if name not in signal_map]
    if missing:
        raise XdbError(
            f"AXIS interface {interface_path!r} is missing required signals: {', '.join(missing)}"
        )
    return signal_map


def _axis_signal_value_map(
    signal_metadata: dict[str, dict[str, Any]],
    sampled_signals: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    by_path = {
        str(item.get("path") or ""): item
        for item in sampled_signals
        if isinstance(item, dict) and item.get("path")
    }
    return {
        name: cast(dict[str, Any], by_path.get(str(meta.get("path") or ""), meta))
        for name, meta in signal_metadata.items()
    }


def _axis_decode_bytes(
    signal_values: dict[str, dict[str, Any]], lane_order: str
) -> tuple[list[str] | None, list[str] | None, int | None]:
    tdata = signal_values.get("tdata") or {}
    tkeep = signal_values.get("tkeep") or {}
    data_value, parsed_data_width = _parse_logic_int(str(tdata.get("value") or ""))
    keep_value, parsed_keep_width = _parse_logic_int(str(tkeep.get("value") or ""))
    meta_data_width = tdata.get("width")
    data_width = int(meta_data_width) if isinstance(meta_data_width, int) else parsed_data_width
    lane_count = None
    if isinstance(data_width, int) and data_width > 0:
        lane_count = max(1, math.ceil(data_width / 8))
    elif isinstance(parsed_keep_width, int) and parsed_keep_width > 0:
        lane_count = parsed_keep_width
    if lane_count is None or lane_count <= 0 or data_value is None:
        return None, None, data_width
    bytes_low_to_high = [f"{(data_value >> (8 * i)) & 0xFF:02x}" for i in range(lane_count)]
    keep_bits_low_to_high = [
        True if keep_value is None else bool((keep_value >> i) & 1) for i in range(lane_count)
    ]
    if lane_order == "high-to-low":
        ordered_bytes = list(reversed(bytes_low_to_high))
        ordered_keep = list(reversed(keep_bits_low_to_high))
    else:
        ordered_bytes = bytes_low_to_high
        ordered_keep = keep_bits_low_to_high
    valid_bytes = [byte for byte, keep in zip(ordered_bytes, ordered_keep) if keep]
    return ordered_bytes, valid_bytes, data_width


def _axis_record(
    *,
    interface_path: str,
    time_text: str,
    signal_values: dict[str, dict[str, Any]],
    beat_index: int | None,
    lane_order: str,
    decode_bytes: bool,
) -> dict[str, Any]:
    tvalid = str((signal_values.get("tvalid") or {}).get("value") or "")
    tready = str((signal_values.get("tready") or {}).get("value") or "")
    record: dict[str, Any] = {
        "interface": interface_path,
        "time": time_text,
        "handshake": tvalid == "1" and tready == "1",
        "tvalid": tvalid,
        "tready": tready,
        "tdata": str((signal_values.get("tdata") or {}).get("value") or ""),
        "tkeep": str((signal_values.get("tkeep") or {}).get("value") or ""),
        "tlast": str((signal_values.get("tlast") or {}).get("value") or ""),
        "tid": str((signal_values.get("tid") or {}).get("value") or ""),
    }
    if beat_index is not None:
        record["beat_index"] = beat_index
    if decode_bytes:
        decoded_bytes, valid_bytes, width_bits = _axis_decode_bytes(signal_values, lane_order)
        record["lane_order"] = lane_order
        record["data_width_bits"] = width_bits
        record["bytes"] = decoded_bytes
        record["valid_bytes"] = valid_bytes
    return record


def _config_matches(
    meta: SessionMeta,
    launch_spec: Mapping[str, object],
    simset: str,
    mode: str,
    top: str,
) -> bool:
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
    cleanup_stale_session(paths)
    if paths.session_dir.exists() and not load_meta(paths):
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


def _launch_spec_summary(launch_spec: Mapping[str, object]) -> dict[str, Any]:
    return {
        key: launch_spec[key]
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
            "needs_stage",
        )
        if key in launch_spec
    }


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
            _close_live_session(session_name, live_meta)
        else:
            if bool(launch_spec.get("needs_stage")):
                raise XdbError(
                    "the packaged simulation input changed and the writable workspace needs "
                    "to be refreshed; use --replace or close the current session first"
                )
            if _config_matches(live_meta, launch_spec, effective_simset, effective_mode, effective_top):
                status = _send_request(session_name, make_request(OP_STATUS))
                status["reused"] = True
                status.update(_launch_spec_summary(launch_spec))
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
    status.update(_launch_spec_summary(launch_spec))
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
        **_launch_spec_summary(staged_spec),
        "provenance": provenance,
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
        **_launch_spec_summary(launch_spec),
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
        else _config_matches(
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


def _axis_trace_collect(
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
    if not interface_paths:
        raise XdbError("missing AXIS interface path")
    if lane_order not in {"low-to-high", "high-to-low"}:
        raise XdbError("lane order must be 'low-to-high' or 'high-to-low'")

    duration_text, duration_value = _parse_duration_tokens(duration_tokens)
    step_text, step_value = _parse_duration_tokens(step_tokens)
    if duration_value <= 0:
        raise XdbError("AXIS trace duration must be > 0")
    if step_value <= 0:
        raise XdbError("AXIS trace step must be > 0")

    interfaces = {
        path: _axis_child_signal_map(session_name, path)
        for path in interface_paths
    }
    signal_paths = [
        str(meta.get("path") or "")
        for signal_map in interfaces.values()
        for meta in signal_map.values()
        if str(meta.get("path") or "")
    ]

    start_time_text = str(time_session(session_name).get("time") or "")
    current_time_text = start_time_text
    current_time_value = _parse_sim_time(current_time_text)
    end_time_value = current_time_value + duration_value

    records: list[dict[str, Any]] = []
    beat_counts = {path: 0 for path in interface_paths}
    iterations = 0
    while current_time_value < end_time_value:
        sampled = read_signals(session_name, signal_paths)
        sampled_signals = [
            cast(dict[str, Any], item)
            for item in list(sampled.get("signals") or [])
            if isinstance(item, dict)
        ]
        for interface_path, signal_map in interfaces.items():
            signal_values = _axis_signal_value_map(signal_map, sampled_signals)
            tvalid = str((signal_values.get("tvalid") or {}).get("value") or "")
            tready = str((signal_values.get("tready") or {}).get("value") or "")
            handshake = tvalid == "1" and tready == "1"
            if only_handshakes and not handshake:
                continue
            if not include_idle and not handshake:
                continue
            beat_index = None
            if handshake:
                beat_index = beat_counts[interface_path]
                beat_counts[interface_path] += 1
            records.append(
                _axis_record(
                    interface_path=interface_path,
                    time_text=current_time_text,
                    signal_values=signal_values,
                    beat_index=beat_index,
                    lane_order=lane_order,
                    decode_bytes=decode_bytes,
                )
            )
        run_result = run_session(session_name, step_tokens)
        next_time_text = str(run_result.get("time_after") or "")
        next_time_value = _parse_sim_time(next_time_text)
        iterations += 1
        if next_time_value <= current_time_value:
            raise XdbError("simulation did not advance while tracing AXIS activity")
        current_time_text = next_time_text
        current_time_value = next_time_value

    return {
        "interfaces": interface_paths,
        "duration": duration_text,
        "step": step_text,
        "time_before": start_time_text,
        "time_after": current_time_text,
        "iterations": iterations,
        "decode_bytes": bool(decode_bytes),
        "lane_order": lane_order,
        "include_idle": bool(include_idle),
        "only_handshakes": bool(only_handshakes),
        "records": records,
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
    return _axis_trace_collect(
        session_name,
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


class _WithTraceArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise XdbError(message)

    def exit(self, status: int = 0, message: str | None = None) -> NoReturn:
        if status:
            raise XdbError((message or "invalid wrapped command arguments").strip())
        raise XdbError((message or "unexpected parser exit").strip())


def _with_trace_parser() -> argparse.ArgumentParser:
    return _WithTraceArgumentParser(add_help=False)


def _parse_with_trace_wait_args(
    rest: list[str], *, positional_count: int
) -> tuple[list[str], float | None, int | None, list[str]]:
    step_tokens = ["10", "ns"]
    timeout_seconds: float | None = None
    max_iterations: int | None = None
    positionals: list[str] = []
    index = 0
    while index < len(rest):
        token = rest[index]
        if token == "--timeout":
            if index + 1 >= len(rest):
                raise XdbError("--timeout requires a value")
            timeout_seconds = float(rest[index + 1])
            index += 2
            continue
        if token == "--max-iterations":
            if index + 1 >= len(rest):
                raise XdbError("--max-iterations requires a value")
            max_iterations = int(rest[index + 1])
            index += 2
            continue
        if token == "--step":
            step_start = index + 1
            step_end = step_start
            while step_end < len(rest) and not rest[step_end].startswith("--"):
                remaining_after = len(rest) - (step_end + 1)
                if remaining_after < positional_count:
                    break
                step_end += 1
            if step_end == step_start:
                raise XdbError("--step requires a duration")
            step_tokens = rest[step_start:step_end]
            index = step_end
            continue
        positionals = rest[index:]
        break
    if len(positionals) < positional_count:
        raise XdbError("missing wrapped wait command argument")
    return step_tokens, timeout_seconds, max_iterations, positionals


def _parse_with_trace_command(command: list[str]) -> SimRequest:
    if not command:
        raise XdbError("missing command after '--'")
    tokens = list(command)
    if tokens and tokens[0] == "--":
        tokens = tokens[1:]
    if tokens[:2] == ["xdb", "sim"]:
        tokens = tokens[2:]
    elif tokens[:1] == ["sim"]:
        tokens = tokens[1:]
    else:
        raise XdbError("with-trace currently only supports wrapped 'xdb sim ...' commands")
    if not tokens:
        raise XdbError("missing wrapped 'xdb sim' subcommand")

    subcommand, rest = tokens[0], tokens[1:]
    if subcommand == "run":
        if not rest:
            raise XdbError("with-trace wrapped 'xdb sim run' requires an explicit duration")
        return make_request(OP_RUN, tokens=rest)
    if subcommand == "step":
        if not rest:
            return make_request(OP_STEP, count=1)
        if len(rest) == 1 and rest[0].isdigit():
            count = int(rest[0])
            if count <= 0:
                raise XdbError("step count must be > 0")
            return make_request(OP_STEP, count=count)
        return make_request(OP_STEP, time_tokens=rest)
    if subcommand in {"until", "wait", "wait-on-condition"}:
        wait_step, wait_timeout, wait_max_iterations, positionals = _parse_with_trace_wait_args(
            rest,
            positional_count=1,
        )
        return make_request(
            OP_UNTIL,
            expr=" ".join(positionals),
            step_tokens=wait_step,
            timeout_seconds=wait_timeout,
            max_iterations=wait_max_iterations,
        )
    if subcommand in {"until-signal", "wait-signal", "wait-on-signal"}:
        wait_step, wait_timeout, wait_max_iterations, positionals = _parse_with_trace_wait_args(
            rest,
            positional_count=2,
        )
        return make_request(
            OP_UNTIL_SIGNAL,
            signal=positionals[0],
            value=positionals[1],
            step_tokens=wait_step,
            timeout_seconds=wait_timeout,
            max_iterations=wait_max_iterations,
        )
    if subcommand == "coyote-status":
        if rest:
            raise XdbError("xdb sim coyote-status does not accept extra arguments in with-trace")
        return make_request(OP_COYOTE_STATUS)
    if subcommand == "clear-completed":
        if rest:
            raise XdbError("xdb sim clear-completed does not accept extra arguments in with-trace")
        return make_request(OP_CLEAR_COMPLETED)
    if subcommand == "invoke":
        parser = _with_trace_parser()
        parser.add_argument("opcode")
        parser.add_argument("--addr", default=None)
        parser.add_argument("--len", dest="length", default=None)
        parser.add_argument("--stream", default="host")
        parser.add_argument("--dest", default="0")
        parser.add_argument("--last", default=True)
        parser.add_argument("--src-addr", default=None)
        parser.add_argument("--src-len", default=None)
        parser.add_argument("--src-stream", default="host")
        parser.add_argument("--src-dest", default="0")
        parser.add_argument("--dst-addr", default=None)
        parser.add_argument("--dst-len", default=None)
        parser.add_argument("--dst-stream", default="host")
        parser.add_argument("--dst-dest", default="0")
        ns = parser.parse_args(rest)
        return make_request(
            OP_INVOKE,
            opcode=ns.opcode,
            addr=None if ns.addr is None else int(ns.addr, 0),
            length=None if ns.length is None else int(ns.length, 0),
            stream_name=ns.stream,
            dest=int(ns.dest, 0),
            last=bool(ns.last),
            src_addr=None if ns.src_addr is None else int(ns.src_addr, 0),
            src_length=None if ns.src_len is None else int(ns.src_len, 0),
            src_stream_name=ns.src_stream,
            src_dest=int(ns.src_dest, 0),
            dst_addr=None if ns.dst_addr is None else int(ns.dst_addr, 0),
            dst_length=None if ns.dst_len is None else int(ns.dst_len, 0),
            dst_stream_name=ns.dst_stream,
            dst_dest=int(ns.dst_dest, 0),
        )
    if subcommand == "completed":
        parser = _with_trace_parser()
        parser.add_argument("opcode")
        parser.add_argument("--count", type=int, default=None)
        parser.add_argument("--timeout", type=float, default=None)
        ns = parser.parse_args(rest)
        return make_request(
            OP_COMPLETED,
            opcode=ns.opcode,
            target_count=ns.count,
            timeout_seconds=ns.timeout,
        )
    if subcommand == "irq" and rest[:1] == ["wait"]:
        parser = _with_trace_parser()
        parser.add_argument("wait")
        parser.add_argument("--timeout", type=float, default=None)
        ns = parser.parse_args(rest)
        return make_request(OP_IRQ_WAIT, timeout_seconds=ns.timeout)
    if subcommand == "csr":
        if not rest:
            raise XdbError("missing xdb sim csr subcommand")
        csr_sub = rest[0]
        parser = _with_trace_parser()
        if csr_sub == "read":
            parser.add_argument("read")
            parser.add_argument("addr")
            parser.add_argument("--timeout", type=float, default=None)
            ns = parser.parse_args(rest)
            return make_request(
                OP_CSR_READ,
                addr=int(ns.addr, 0),
                timeout_seconds=ns.timeout,
            )
        if csr_sub == "write":
            parser.add_argument("write")
            parser.add_argument("addr")
            parser.add_argument("value")
            ns = parser.parse_args(rest)
            return make_request(OP_CSR_WRITE, addr=int(ns.addr, 0), value=int(ns.value, 0))
        raise XdbError(f"unsupported xdb sim csr subcommand for with-trace: {csr_sub}")
    if subcommand == "mem":
        if not rest:
            raise XdbError("missing xdb sim mem subcommand")
        mem_sub = rest[0]
        parser = _with_trace_parser()
        if mem_sub == "map":
            parser.add_argument("map")
            parser.add_argument("space")
            parser.add_argument("addr")
            parser.add_argument("size")
            ns = parser.parse_args(rest)
            return make_request(OP_MEM_MAP, space=ns.space, addr=int(ns.addr, 0), size=int(ns.size, 0))
        if mem_sub == "unmap":
            parser.add_argument("unmap")
            parser.add_argument("space")
            parser.add_argument("addr")
            ns = parser.parse_args(rest)
            return make_request(OP_MEM_UNMAP, space=ns.space, addr=int(ns.addr, 0))
        if mem_sub == "list":
            parser.add_argument("list")
            parser.add_argument("space", nargs="?", default="host")
            ns = parser.parse_args(rest)
            return make_request(OP_MEM_LIST, space=ns.space)
        if mem_sub == "reset":
            parser.add_argument("reset")
            parser.add_argument("space", nargs="?", default="host")
            ns = parser.parse_args(rest)
            return make_request(OP_MEM_RESET, space=ns.space)
        if mem_sub == "read":
            parser.add_argument("read")
            parser.add_argument("space")
            parser.add_argument("addr")
            parser.add_argument("size")
            ns = parser.parse_args(rest)
            return make_request(OP_MEM_READ, space=ns.space, addr=int(ns.addr, 0), size=int(ns.size, 0))
        if mem_sub == "write":
            parser.add_argument("write")
            parser.add_argument("space")
            parser.add_argument("addr")
            parser.add_argument("--hex", dest="hex_data", default=None)
            parser.add_argument("--text", dest="text_data", default=None)
            parser.add_argument("--file", default=None)
            ns = parser.parse_args(rest)
            data_bytes = b""
            if ns.hex_data is not None:
                data_bytes = parse_hex_bytes(ns.hex_data)
            elif ns.text_data is not None:
                data_bytes = ns.text_data.encode("utf-8")
            elif ns.file is not None:
                data_bytes = Path(ns.file).expanduser().read_bytes()
            else:
                raise XdbError("xdb sim mem write requires --hex, --text, or --file")
            return make_request(
                OP_MEM_WRITE,
                space=ns.space,
                addr=int(ns.addr, 0),
                data_hex=data_bytes.hex(),
            )
        raise XdbError(f"unsupported xdb sim mem subcommand for with-trace: {mem_sub}")
    raise XdbError(f"unsupported wrapped xdb sim subcommand for with-trace: {subcommand}")


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
        request_args["action_request"] = _parse_with_trace_command(command)
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
