from __future__ import annotations

import json
import math
import os
import re
import signal
import socket
import traceback
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from ..errors import XdbError
from .protocol import (
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
    OP_TIME,
    OP_TCL,
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
)
from .session_store import SessionPaths, ensure_session_dir, write_meta
from .vivado_driver import VivadoSimDriver


def _arg_int(args: dict[str, Any], name: str, default: int = 0) -> int:
    value = args.get(name)
    return default if value is None else int(value)


def _arg_optional_int(args: dict[str, Any], name: str) -> int | None:
    value = args.get(name)
    return None if value is None else int(value)


def _arg_optional_float(args: dict[str, Any], name: str) -> float | None:
    value = args.get(name)
    return None if value is None else float(value)


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
    if re.search(r"[xXzZ]", normalized):
        return None, None
    if re.fullmatch(r"[01]+", normalized):
        return int(normalized, 2), len(normalized)
    if re.fullmatch(r"[0-9]+", normalized):
        return int(normalized, 10), None
    if re.fullmatch(r"[0-9a-fA-F]+", normalized):
        return int(normalized, 16), None
    return None, None


class SimDaemon:
    def __init__(
        self,
        paths: SessionPaths,
        project: str,
        simset: str,
        mode: str,
        top: str,
        package_runtime: str = "",
        runtime_root: str = "",
        work_dir: str = "",
        compile_script: str = "",
        elaborate_script: str = "",
        simulate_script: str = "",
    ):
        self.paths = paths
        self.project = project
        self.simset = simset
        self.mode = mode
        self.top = top
        self.package_runtime = package_runtime
        self.runtime_root = runtime_root
        self.work_dir = work_dir
        self.compile_script = compile_script
        self.elaborate_script = elaborate_script
        self.simulate_script = simulate_script
        self.driver = VivadoSimDriver(
            project=project,
            simset=simset,
            mode=mode,
            top=top,
            vivado_log_path=str(paths.vivado_log_path),
            runtime_root=runtime_root,
            work_dir=work_dir,
            compile_script=compile_script,
            elaborate_script=elaborate_script,
            simulate_script=simulate_script,
        )
        self._stop = False
        self._server: socket.socket | None = None
        self.meta = write_meta(
            self.paths,
            {
                "pid": os.getpid(),
                "cwd": str(Path.cwd()),
                "launch_kind": "runtime",
                "project": self.project,
                "simset": self.simset,
                "mode": self.mode,
                "top": self.top,
                "package_runtime": self.package_runtime,
                "runtime_root": self.runtime_root,
                "work_dir": self.work_dir,
                "compile_script": self.compile_script,
                "elaborate_script": self.elaborate_script,
                "simulate_script": self.simulate_script,
                "state": "starting",
            },
        )

    def run(self) -> None:
        self._install_signal_handlers()
        try:
            ensure_session_dir(self.paths)
            self.driver.start()
            self.meta = write_meta(
                self.paths,
                {
                    **self.meta,
                    "top": self.driver.top,
                    "state": "ready",
                    "last_error": "",
                },
            )
            self._serve()
        except Exception as e:
            self.meta = write_meta(
                self.paths,
                {
                    **self.meta,
                    "state": "error",
                    "last_error": str(e),
                },
            )
            raise
        finally:
            try:
                self.driver.shutdown()
            finally:
                self._cleanup_socket()
                if self.meta.get("state") != "error":
                    self.meta = write_meta(self.paths, {**self.meta, "state": "closed"})

    def _install_signal_handlers(self) -> None:
        def _handle_signal(signum: int, _frame) -> None:
            self._stop = True
            raise SystemExit(128 + signum)

        signal.signal(signal.SIGTERM, _handle_signal)
        signal.signal(signal.SIGINT, _handle_signal)

    def _serve(self) -> None:
        self._cleanup_socket()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server = server
        server.bind(str(self.paths.socket_path))
        server.listen(8)
        server.settimeout(0.5)

        while not self._stop:
            try:
                conn, _ = server.accept()
            except TimeoutError:
                continue
            except OSError:
                if self._stop:
                    break
                raise
            with conn:
                response = self._handle_connection(conn)
                conn.sendall(json.dumps(response).encode("utf-8"))
                if bool((response.get("result") or {}).get("_shutdown")):
                    self._stop = True
                    break

    def _handle_connection(self, conn: socket.socket) -> dict[str, Any]:
        try:
            payload = self._recv_all(conn)
            req = cast(dict[str, Any], json.loads(payload.decode("utf-8")))
            result = self._dispatch(
                str(req.get("op") or ""),
                cast(dict[str, Any], req.get("args") or {}),
            )
            return {"ok": True, "result": result}
        except Exception as e:
            return {
                "ok": False,
                "error": str(e),
                "traceback": traceback.format_exc(),
            }

    @staticmethod
    def _recv_all(conn: socket.socket) -> bytes:
        chunks: list[bytes] = []
        while True:
            data = conn.recv(65536)
            if not data:
                break
            chunks.append(data)
        return b"".join(chunks)

    def _axis_child_signal_map(self, interface_path: str) -> dict[str, dict[str, Any]]:
        result = self.driver.objects(interface_path)
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

    @staticmethod
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

    @staticmethod
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
        self,
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
            decoded_bytes, valid_bytes, width_bits = self._axis_decode_bytes(signal_values, lane_order)
            record["lane_order"] = lane_order
            record["data_width_bits"] = width_bits
            record["bytes"] = decoded_bytes
            record["valid_bytes"] = valid_bytes
        return record

    def _with_trace(self, args: dict[str, Any]) -> dict[str, Any]:
        action_request = cast(dict[str, Any], args.get("action_request") or {})
        action_op = str(action_request.get("op") or "")
        action_args = cast(dict[str, Any], action_request.get("args") or {})
        if action_op not in {
            OP_INVOKE,
            OP_MEM_MAP,
            OP_MEM_UNMAP,
            OP_MEM_LIST,
            OP_MEM_RESET,
            OP_MEM_WRITE,
            OP_MEM_READ,
            OP_CSR_READ,
            OP_CSR_WRITE,
            OP_COMPLETED,
            OP_CLEAR_COMPLETED,
            OP_IRQ_WAIT,
            OP_COYOTE_STATUS,
        }:
            raise XdbError(
                f"with-trace currently supports a limited set of 'xdb sim' commands; unsupported op: {action_op}"
            )

        duration_tokens = [str(v) for v in list(args.get("duration_tokens") or []) if str(v)]
        step_tokens = [str(v) for v in list(args.get("step_tokens") or []) if str(v)]
        if not duration_tokens:
            raise XdbError("missing trace duration")
        if not step_tokens:
            raise XdbError("missing trace step")
        _duration_text, duration_value = _parse_duration_tokens(duration_tokens)
        _step_text, step_value = _parse_duration_tokens(step_tokens)
        if duration_value <= 0 or step_value <= 0:
            raise XdbError("trace duration and step must be > 0")

        transactions = bool(args.get("transactions"))
        axis_paths = [str(v) for v in list(args.get("axis_paths") or []) if str(v)]
        decode_bytes = bool(args.get("decode_bytes"))
        lane_order = str(args.get("lane_order") or "low-to-high")
        include_idle = bool(args.get("include_idle"))
        only_handshakes = bool(args.get("only_handshakes"))

        axis_interfaces = {path: self._axis_child_signal_map(path) for path in axis_paths}
        axis_signal_paths = [
            str(meta.get("path") or "")
            for signal_map in axis_interfaces.values()
            for meta in signal_map.values()
            if str(meta.get("path") or "")
        ]
        axis_records: list[dict[str, Any]] = []
        beat_counts = {path: 0 for path in axis_paths}

        def sample_axis(time_text: str) -> None:
            if not axis_paths:
                return
            sampled = self.driver.read_signals(axis_signal_paths)
            sampled_signals = [
                cast(dict[str, Any], item)
                for item in list(sampled.get("signals") or [])
                if isinstance(item, dict)
            ]
            for interface_path, signal_map in axis_interfaces.items():
                signal_values = self._axis_signal_value_map(signal_map, sampled_signals)
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
                axis_records.append(
                    self._axis_record(
                        interface_path=interface_path,
                        time_text=time_text,
                        signal_values=signal_values,
                        beat_index=beat_index,
                        lane_order=lane_order,
                        decode_bytes=decode_bytes,
                    )
                )

        time_before = str(self.driver.time().get("time") or "")
        current_time_value = _parse_sim_time(time_before)
        end_time_value = current_time_value + duration_value
        if transactions:
            self.driver.trace_events_clear()
        sample_axis(time_before)
        previous_hook = self.driver._sim_advance_hook
        self.driver._sim_advance_hook = lambda _before, after: sample_axis(after)
        try:
            action_result = self._dispatch(action_op, action_args)
            iterations = 0
            while current_time_value < end_time_value:
                run_result = self.driver.run(step_tokens)
                current_time_text = str(run_result.get("time_after") or "")
                current_time_value = _parse_sim_time(current_time_text)
                iterations += 1
            time_after = str(self.driver.time().get("time") or "")
        finally:
            self.driver._sim_advance_hook = previous_hook

        result: dict[str, Any] = {
            "action": {
                "op": action_op,
                "args": action_args,
                "result": action_result,
            },
            "duration": " ".join(duration_tokens),
            "step": " ".join(step_tokens),
            "time_before": time_before,
            "time_after": time_after,
        }
        if transactions:
            result["transactions"] = self.driver.trace_events_get()
        if axis_paths:
            result["axis"] = {
                "interfaces": axis_paths,
                "duration": " ".join(duration_tokens),
                "step": " ".join(step_tokens),
                "time_before": time_before,
                "time_after": time_after,
                "decode_bytes": decode_bytes,
                "lane_order": lane_order,
                "include_idle": include_idle,
                "only_handshakes": only_handshakes,
                "records": axis_records,
            }
        return result

    def _dispatch(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        if op == OP_STATUS:
            time_info = self.driver.time()
            return {
                "session": self.meta,
                "time": time_info.get("time", ""),
            }
        if op == OP_TIME:
            return self.driver.time()
        if op == OP_DESCRIBE:
            return self.driver.describe_session()
        if op == OP_RUN:
            return self.driver.run(list(args.get("tokens") or []))
        if op == OP_RESTART:
            return self.driver.restart()
        if op == OP_FORCE:
            signal_name = str(args.get("signal") or "")
            values = [str(v) for v in list(args.get("values") or [])]
            if not signal_name:
                raise XdbError("missing signal")
            if not values:
                raise XdbError("missing force value")
            return self.driver.force(
                signal_name,
                values,
                radix=str(args.get("radix") or "") or None,
                repeat_every=str(args.get("repeat_every") or "") or None,
                cancel_after=str(args.get("cancel_after") or "") or None,
            )
        if op == OP_RELEASE:
            all_forces = bool(args.get("all"))
            signal_name = str(args.get("signal") or "")
            if not all_forces and not signal_name:
                raise XdbError("missing signal; pass a signal path or --all")
            return self.driver.release(signal_name or None, all_forces=all_forces)
        if op == OP_GET:
            signal_name = str(args.get("signal") or "")
            if not signal_name:
                raise XdbError("missing signal")
            return self.driver.get_signal(signal_name)
        if op == OP_GET_MANY:
            pattern = str(args.get("pattern") or "")
            if not pattern:
                raise XdbError("missing pattern")
            return self.driver.get_many(pattern)
        if op == OP_READ_SIGNALS:
            signals = [str(v) for v in list(args.get("signals") or []) if str(v)]
            if not signals:
                raise XdbError("missing signals")
            return self.driver.read_signals(signals)
        if op == OP_SCOPES:
            scope = args.get("scope")
            return self.driver.scopes(None if scope in (None, "") else str(scope))
        if op == OP_OBJECTS:
            scope = str(args.get("scope") or "")
            if not scope:
                raise XdbError("missing scope")
            return self.driver.objects(scope)
        if op == OP_TOP:
            top = str(args.get("top") or "")
            if not top:
                raise XdbError("missing top module")
            result = self.driver.set_top(top)
            self.top = top
            self.meta = write_meta(self.paths, {**self.meta, "top": self.driver.top, "state": "ready"})
            return result
        if op == OP_WAVE_ADD:
            pattern = str(args.get("pattern") or "")
            if not pattern:
                raise XdbError("missing pattern")
            return self.driver.add_wave(pattern)
        if op == OP_STEP:
            time_tokens = list(args.get("time_tokens") or [])
            if time_tokens:
                return self.driver.step(time_tokens=time_tokens)
            count = _arg_int(args, "count", 1)
            if count <= 0:
                raise XdbError("step count must be > 0")
            return self.driver.step(count=count)
        if op == OP_UNTIL:
            expr = str(args.get("expr") or "")
            step_tokens = [str(v) for v in list(args.get("step_tokens") or [])]
            timeout_seconds = args.get("timeout_seconds")
            max_iterations = args.get("max_iterations")
            if not expr:
                raise XdbError("missing Tcl expression")
            if not step_tokens:
                raise XdbError("missing step duration")
            if timeout_seconds is not None and float(timeout_seconds) <= 0:
                raise XdbError("timeout_seconds must be > 0")
            if max_iterations is not None and int(max_iterations) <= 0:
                raise XdbError("max_iterations must be > 0")
            return self.driver.wait_until(
                expr,
                step_tokens=step_tokens,
                timeout_seconds=_arg_optional_float(args, "timeout_seconds"),
                max_iterations=_arg_optional_int(args, "max_iterations"),
            )
        if op == OP_UNTIL_SIGNAL:
            signal_name = str(args.get("signal") or "")
            value = str(args.get("value") or "")
            step_tokens = [str(v) for v in list(args.get("step_tokens") or [])]
            timeout_seconds = args.get("timeout_seconds")
            max_iterations = args.get("max_iterations")
            if not signal_name:
                raise XdbError("missing signal")
            if value == "":
                raise XdbError("missing expected signal value")
            if not step_tokens:
                raise XdbError("missing step duration")
            if timeout_seconds is not None and float(timeout_seconds) <= 0:
                raise XdbError("timeout_seconds must be > 0")
            if max_iterations is not None and int(max_iterations) <= 0:
                raise XdbError("max_iterations must be > 0")
            return self.driver.wait_until_signal(
                signal_name,
                value,
                step_tokens=step_tokens,
                timeout_seconds=_arg_optional_float(args, "timeout_seconds"),
                max_iterations=_arg_optional_int(args, "max_iterations"),
            )
        if op == OP_ASSERT_SIGNAL:
            signal_name = str(args.get("signal") or "")
            value = str(args.get("value") or "")
            if not signal_name:
                raise XdbError("missing signal")
            if value == "":
                raise XdbError("missing expected signal value")
            return self.driver.assert_signal(signal_name, value)
        if op == OP_ASSERT_TCL:
            expr = str(args.get("expr") or "")
            if not expr:
                raise XdbError("missing Tcl expression")
            return self.driver.assert_tcl(expr)
        if op == OP_EXPECT_SIGNAL:
            signal_name = str(args.get("signal") or "")
            value = str(args.get("value") or "")
            within_tokens = [str(v) for v in list(args.get("within_tokens") or [])]
            if not signal_name:
                raise XdbError("missing signal")
            if value == "":
                raise XdbError("missing expected signal value")
            if not within_tokens:
                raise XdbError("missing within duration")
            return self.driver.expect_signal(signal_name, value, within_tokens=within_tokens)
        if op == OP_EXPECT_CHANGE:
            signal_name = str(args.get("signal") or "")
            within_tokens = [str(v) for v in list(args.get("within_tokens") or [])]
            if not signal_name:
                raise XdbError("missing signal")
            if not within_tokens:
                raise XdbError("missing within duration")
            return self.driver.expect_change(signal_name, within_tokens=within_tokens)
        if op == OP_BREAKPOINT_ADD:
            condition = str(args.get("condition") or "")
            if not condition:
                raise XdbError("missing breakpoint condition")
            return self.driver.add_breakpoint(condition)
        if op == OP_BREAKPOINT_CLEAR:
            return self.driver.clear_breakpoints()
        if op == OP_TCL:
            script = str(args.get("script") or "")
            if not script:
                raise XdbError("missing Tcl script")
            return self.driver.eval_tcl(script)
        if op == OP_SOURCE:
            path = str(args.get("path") or "")
            if not path:
                raise XdbError("missing Tcl script path")
            return self.driver.source_tcl(path)
        if op == OP_COYOTE_STATUS:
            return self.driver.coyote_status()
        if op == OP_CSR_READ:
            return self.driver.coyote_csr_read(
                _arg_int(args, "addr"),
                timeout_seconds=_arg_optional_float(args, "timeout_seconds"),
            )
        if op == OP_CSR_WRITE:
            return self.driver.coyote_csr_write(
                _arg_int(args, "addr"),
                _arg_int(args, "value"),
            )
        if op == OP_MEM_MAP:
            return self.driver.coyote_mem_map(
                str(args.get("space") or "host"),
                _arg_int(args, "addr"),
                _arg_int(args, "size"),
            )
        if op == OP_MEM_UNMAP:
            return self.driver.coyote_mem_unmap(
                str(args.get("space") or "host"),
                _arg_int(args, "addr"),
            )
        if op == OP_MEM_LIST:
            return self.driver.coyote_mem_list(str(args.get("space") or "host"))
        if op == OP_MEM_RESET:
            return self.driver.coyote_mem_reset(str(args.get("space") or "host"))
        if op == OP_MEM_WRITE:
            data_hex = str(args.get("data_hex") or "")
            if not data_hex:
                raise XdbError("missing memory write payload")
            return self.driver.coyote_mem_write(
                str(args.get("space") or "host"),
                _arg_int(args, "addr"),
                bytes.fromhex(data_hex),
            )
        if op == OP_MEM_READ:
            return self.driver.coyote_mem_read(
                str(args.get("space") or "host"),
                _arg_int(args, "addr"),
                _arg_int(args, "size"),
            )
        if op == OP_INVOKE:
            return self.driver.coyote_invoke(
                str(args.get("opcode") or ""),
                addr=_arg_optional_int(args, "addr"),
                length=_arg_optional_int(args, "length"),
                stream_name=str(args.get("stream_name") or "host"),
                dest=_arg_int(args, "dest"),
                last=bool(args.get("last", True)),
                src_addr=_arg_optional_int(args, "src_addr"),
                src_length=_arg_optional_int(args, "src_length"),
                src_stream_name=str(args.get("src_stream_name") or "host"),
                src_dest=_arg_int(args, "src_dest"),
                dst_addr=_arg_optional_int(args, "dst_addr"),
                dst_length=_arg_optional_int(args, "dst_length"),
                dst_stream_name=str(args.get("dst_stream_name") or "host"),
                dst_dest=_arg_int(args, "dst_dest"),
            )
        if op == OP_COMPLETED:
            return self.driver.coyote_completed(
                str(args.get("opcode") or ""),
                target_count=_arg_optional_int(args, "target_count"),
                timeout_seconds=_arg_optional_float(args, "timeout_seconds"),
            )
        if op == OP_CLEAR_COMPLETED:
            return self.driver.coyote_clear_completed()
        if op == OP_IRQ_WAIT:
            return self.driver.coyote_irq_wait(
                timeout_seconds=_arg_optional_float(args, "timeout_seconds"),
            )
        if op == OP_TRACE_EVENTS_CLEAR:
            return self.driver.trace_events_clear()
        if op == OP_TRACE_EVENTS_GET:
            return self.driver.trace_events_get()
        if op == OP_WITH_TRACE:
            return self._with_trace(args)
        if op == OP_TRACE_TRANSACTIONS:
            duration_tokens = [str(v) for v in list(args.get("duration_tokens") or [])]
            if not duration_tokens:
                raise XdbError("missing trace duration")
            opcode = str(args.get("opcode") or "") or None
            return self.driver.trace_transactions(duration_tokens, opcode_filter=opcode)
        if op == OP_SNAPSHOT:
            scope = str(args.get("scope") or "")
            if not scope:
                raise XdbError("missing scope")
            name = str(args.get("name") or "")
            return self.driver.snapshot_scope(scope, name=name or None)
        if op == OP_DIFF_SNAPSHOT:
            before = str(args.get("before") or "")
            after = str(args.get("after") or "")
            if not before or not after:
                raise XdbError("diff-snapshot requires both snapshot identifiers")
            return self.driver.diff_snapshot(before, after)
        if op == OP_WATCH_CHANGES:
            scope = str(args.get("scope") or "")
            duration_tokens = [str(v) for v in list(args.get("duration_tokens") or [])]
            if not scope:
                raise XdbError("missing scope")
            if not duration_tokens:
                raise XdbError("missing duration")
            return self.driver.watch_changes(scope, duration_tokens=duration_tokens)
        if op == OP_VCD_START:
            path = str(args.get("path") or "")
            if not path:
                raise XdbError("missing VCD file path")
            scope = str(args.get("scope") or "")
            return self.driver.vcd_start(path, scope or None)
        if op == OP_VCD_STOP:
            return self.driver.vcd_stop()
        if op == OP_VCD_STATUS:
            return self.driver.vcd_status()
        if op == OP_CLOSE:
            return {"closed": True, "session_id": self.paths.session_id, "_shutdown": True}
        raise XdbError(f"unsupported simulation operation: {op}")

    def _cleanup_socket(self) -> None:
        try:
            if self._server is not None:
                self._server.close()
        finally:
            self._server = None
        try:
            self.paths.socket_path.unlink(missing_ok=True)
        except Exception:
            pass


def run_daemon(
    *,
    anchor_dir: str,
    session_name: str | None,
    project: str,
    simset: str,
    mode: str,
    top: str,
    package_runtime: str = "",
    runtime_root: str = "",
    work_dir: str = "",
    compile_script: str = "",
    elaborate_script: str = "",
    simulate_script: str = "",
) -> None:
    paths = SessionPaths(Path(anchor_dir), session_name)
    daemon = SimDaemon(
        paths=paths,
        project=project,
        simset=simset,
        mode=mode,
        top=top,
        package_runtime=package_runtime,
        runtime_root=runtime_root,
        work_dir=work_dir,
        compile_script=compile_script,
        elaborate_script=elaborate_script,
        simulate_script=simulate_script,
    )
    daemon.run()
