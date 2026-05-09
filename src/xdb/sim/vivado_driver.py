from __future__ import annotations

import json
import os
import pty
import queue
import select
import shlex
import subprocess
import threading
import time
import tty
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Callable, TextIO, cast

from xdb.errors import XdbError
from xdb.sim.coyote import CoyoteSimController
from xdb.sim.tcl_api import build_proc_request
from xdb.sim.tcl_helpers import _tcl_string, load_tcl_library
from xdb.sim.vivado_coyote_runtime import VivadoCoyoteMixin
from xdb.sim.vivado_debug import VivadoDebugMixin
from xdb.sim.vivado_queries import VivadoQueryMixin


class VivadoSimDriver(VivadoQueryMixin, VivadoDebugMixin, VivadoCoyoteMixin):
    def __init__(
        self,
        project: str,
        simset: str,
        mode: str,
        top: str,
        vivado_log_path: str,
        runtime_root: str = "",
        work_dir: str = "",
        compile_script: str = "",
        elaborate_script: str = "",
        simulate_script: str = "",
    ):
        self.project = str(Path(project).resolve()) if project else ""
        self.simset = simset
        self.mode = mode
        self.top = top
        self.vivado_log_path = vivado_log_path
        self.runtime_root = str(Path(runtime_root).resolve()) if runtime_root else ""
        self.work_dir = str(Path(work_dir).resolve()) if work_dir else ""
        self.compile_script = str(Path(compile_script).resolve()) if compile_script else ""
        self.elaborate_script = str(Path(elaborate_script).resolve()) if elaborate_script else ""
        self.simulate_script = str(Path(simulate_script).resolve()) if simulate_script else ""
        self.proc: subprocess.Popen[bytes] | None = None
        self._reader_thread: threading.Thread | None = None
        self._queue: queue.Queue[str] = queue.Queue()
        self._log_file: TextIO | None = None
        self._recent_lines: deque[str] = deque(maxlen=80)
        self._pty_master_fd: int | None = None
        self._coyote: CoyoteSimController | None = None
        self._snapshots: dict[str, dict[str, Any]] = {}
        self._vcd_state: dict[str, Any] | None = None
        self._sim_advance_hook: Callable[[str, str], None] | None = None

    def _debug_enabled(self) -> bool:
        value = os.environ.get("XDB_DEBUG") or os.environ.get("XDB_VERBOSE")
        if value is None:
            return False
        return value.strip().lower() not in {"", "0", "false", "no", "off"}

    def _tail_log(self, limit: int = 60) -> list[str]:
        path = Path(self.vivado_log_path)
        if not path.exists():
            return []
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []
        return lines[-limit:]

    def _record_debug_line(self, message: str) -> None:
        self._recent_lines.append(message)
        if self._log_file is not None:
            self._log_file.write(message + "\n")

    def _format_exit_diagnostics(self, summary: str) -> str:
        exit_code = None
        if self.proc is not None:
            exit_code = self.proc.poll()

        message = summary
        if exit_code is not None:
            message += f" (exit code {exit_code})"
        message += f". Vivado log: {self.vivado_log_path}"

        if not self._debug_enabled():
            message += " Re-run with --debug for recent Vivado output."
            return message

        recent = list(self._recent_lines)
        if not recent:
            recent = self._tail_log()
        if recent:
            message += "\n\nRecent Vivado output:\n" + "\n".join(recent[-60:])
        return message

    def start(self, timeout: int = 300) -> None:
        Path(self.vivado_log_path).parent.mkdir(parents=True, exist_ok=True)
        self._log_file = open(self.vivado_log_path, "a", encoding="utf-8", buffering=1)
        self._prepare_runtime_bundle()
        self._start_runtime_simulator()
        self._send_raw(load_tcl_library())
        self._wait_for_runtime_prompt(timeout=timeout)

    def _start_pty_process(self, cmd: list[str], *, cwd: str | None = None) -> None:
        try:
            master_fd, slave_fd = pty.openpty()
            tty.setraw(slave_fd)
            self._pty_master_fd = master_fd
            self.proc = subprocess.Popen(
                cmd,
                cwd=cwd,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                text=False,
                close_fds=True,
            )
            os.close(slave_fd)
        except Exception:
            if self._pty_master_fd is not None:
                try:
                    os.close(self._pty_master_fd)
                except OSError:
                    pass
                self._pty_master_fd = None
            raise

        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()

    def _run_script(self, script_path: str, *, cwd: str) -> None:
        cmd = ["bash", script_path]
        if self._debug_enabled():
            self._record_debug_line(
                "[xdb debug] runtime script: " + " ".join(shlex.quote(arg) for arg in cmd)
            )
            self._record_debug_line(f"[xdb debug] runtime cwd: {cwd}")
        try:
            subprocess.run(
                cmd,
                cwd=cwd,
                check=True,
                stdout=self._log_file,
                stderr=subprocess.STDOUT,
                text=True,
            )
        except subprocess.CalledProcessError as e:
            raise XdbError(
                self._format_exit_diagnostics(
                    f"runtime preparation script failed: {script_path}"
                )
            ) from e

    def _prepare_runtime_bundle(self) -> None:
        if not self.work_dir:
            raise XdbError("missing runtime work directory")
        if not self.compile_script or not self.elaborate_script or not self.simulate_script:
            raise XdbError("runtime launch metadata is incomplete")
        self._prepare_coyote_runtime()
        self._run_script(self.compile_script, cwd=self.work_dir)
        self._run_script(self.elaborate_script, cwd=self.work_dir)

    def _runtime_simulate_command(self) -> list[str]:
        simulate_path = Path(self.simulate_script)
        try:
            lines = simulate_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as e:
            raise XdbError(f"failed to read simulate script: {simulate_path}") from e
        for line in lines:
            stripped = line.strip()
            if not stripped.startswith("xsim "):
                continue
            tokens = shlex.split(stripped)
            out: list[str] = []
            skip_next = False
            for token in tokens:
                if skip_next:
                    skip_next = False
                    continue
                if token == "-tclbatch":
                    skip_next = True
                    continue
                out.append(token)
            return out
        raise XdbError(f"failed to determine xsim launch command from {simulate_path}")

    def _start_runtime_simulator(self) -> None:
        cmd = self._runtime_simulate_command()
        if self._debug_enabled():
            self._record_debug_line(
                "[xdb debug] xsim argv: " + " ".join(shlex.quote(arg) for arg in cmd)
            )
            self._record_debug_line(f"[xdb debug] xsim cwd: {self.work_dir}")
        try:
            self._start_pty_process(cmd, cwd=self.work_dir)
        except FileNotFoundError as e:
            raise XdbError(
                "xsim executable not found in PATH. Run inside a Xilinx-enabled shell or set PATH accordingly."
            ) from e

    def _wait_for_runtime_prompt(self, timeout: int) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc is not None and self.proc.poll() is not None:
                raise XdbError(
                    self._format_exit_diagnostics(
                        "xsim process exited while starting runtime simulation"
                    )
                )
            try:
                self.time()
                return
            except XdbError:
                time.sleep(0.2)
        raise XdbError("timed out waiting for xsim runtime session to become ready")

    def _reader_loop(self) -> None:
        assert self._pty_master_fd is not None
        pending = ""
        while True:
            if self.proc is not None and self.proc.poll() is not None:
                try:
                    ready, _, _ = select.select([self._pty_master_fd], [], [], 0)
                except OSError:
                    break
                if not ready:
                    break
            try:
                ready, _, _ = select.select([self._pty_master_fd], [], [], 0.2)
            except OSError:
                break
            if not ready:
                continue
            try:
                chunk = os.read(self._pty_master_fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            text_chunk = chunk.decode(errors="replace").replace("\r\n", "\n").replace("\r", "\n")
            pending += text_chunk
            while "\n" in pending:
                line, pending = pending.split("\n", 1)
                if self._log_file is not None:
                    self._log_file.write(line + "\n")
                self._recent_lines.append(line)
                self._queue.put(line)
        if pending:
            if self._log_file is not None:
                self._log_file.write(pending + "\n")
            self._recent_lines.append(pending)
            self._queue.put(pending)

    def _send_raw(self, script: str) -> None:
        if self.proc is None or self._pty_master_fd is None:
            raise XdbError("vivado simulation process is not running")
        try:
            payload = script if script.endswith("\n") else script + "\n"
            os.write(self._pty_master_fd, payload.encode())
        except BrokenPipeError as e:
            raise XdbError(
                self._format_exit_diagnostics(
                    "vivado simulation process terminated unexpectedly"
                )
            ) from e

    def request(self, body_tcl: str, timeout: int = 120) -> dict[str, Any]:
        if self.proc is None:
            raise XdbError("vivado simulation process is not running")
        if self.proc.poll() is not None:
            raise XdbError("vivado simulation process is not running")

        request_id = uuid.uuid4().hex
        script = f'''
set __xdb_request_id {_tcl_string(request_id)}
if {{[catch {{
{body_tcl}
}} __xdb_err __xdb_opts]}} {{
  xdb_reply_error $__xdb_request_id $__xdb_err
}}
'''
        self._send_raw(script)
        return self._await_response(request_id, timeout=timeout)

    def _await_response(self, request_id: str, timeout: int) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        in_block = False
        payload_lines: list[str] = []
        begin_line = f"__XDB_BEGIN__ {request_id}"
        end_line = f"__XDB_END__ {request_id}"

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise XdbError(f"timed out waiting for vivado simulation reply: {request_id}")
            try:
                line = self._queue.get(timeout=min(0.2, remaining))
            except queue.Empty:
                if self.proc is not None and self.proc.poll() is not None:
                    raise XdbError(
                        self._format_exit_diagnostics(
                            "vivado simulation process exited while waiting for reply"
                        )
                    )
                continue

            if line == begin_line:
                in_block = True
                payload_lines = []
                continue
            if line == end_line and in_block:
                payload = "\n".join(payload_lines).strip()
                if not payload:
                    raise XdbError("empty response from vivado simulation process")
                data = cast(dict[str, Any], json.loads(payload))
                if not data.get("ok", False):
                    raise XdbError(str(data.get("error", "simulation request failed")))
                return data
            if in_block:
                payload_lines.append(line)

    def launch(self, timeout: int = 300, top: str | None = None) -> dict[str, Any]:
        effective_top = self.top if top is None else top
        if top is not None and top != self.top:
            raise XdbError("changing top module is not supported for runtime-backed simulation sessions")
        time_info = self.time()
        return {
            "project": self.project,
            "simset": self.simset,
            "mode": self.mode,
            "top": effective_top,
            "time": str(time_info.get("time", "")),
        }

    def status(self) -> dict[str, Any]:
        return self.time()

    def time(self) -> dict[str, Any]:
        return self.request(build_proc_request("xdb_api_time"))

    def run(self, tokens: list[str]) -> dict[str, Any]:
        result = self.request(build_proc_request("xdb_api_run", tokens))
        self._notify_sim_advance(result)
        return result

    def restart(self) -> dict[str, Any]:
        result = self.request(build_proc_request("xdb_api_restart"))
        self._notify_sim_advance(result)
        return result

    def step(
        self, count: int | None = None, time_tokens: list[str] | None = None
    ) -> dict[str, Any]:
        if time_tokens:
            result = self.request(build_proc_request("xdb_api_step_time", time_tokens))
            self._notify_sim_advance(result)
            return result
        step_count = count or 1
        result = self.request(build_proc_request("xdb_api_step_count", step_count))
        self._notify_sim_advance(result)
        return result

    def wait_until(
        self,
        expr: str,
        *,
        step_tokens: list[str],
        timeout_seconds: float | None = None,
        max_iterations: int | None = None,
    ) -> dict[str, Any]:
        request_timeout = 86400 if timeout_seconds is None else max(86400, int(timeout_seconds) + 60)
        return self.request(
            build_proc_request(
                "xdb_api_wait_until",
                expr,
                step_tokens,
                "" if timeout_seconds is None else str(timeout_seconds),
                "" if max_iterations is None else str(max_iterations),
            ),
            timeout=request_timeout,
        )

    def wait_until_signal(
        self,
        signal: str,
        value: str,
        *,
        step_tokens: list[str],
        timeout_seconds: float | None = None,
        max_iterations: int | None = None,
    ) -> dict[str, Any]:
        request_timeout = 86400 if timeout_seconds is None else max(86400, int(timeout_seconds) + 60)
        return self.request(
            build_proc_request(
                "xdb_api_wait_until_signal",
                signal,
                value,
                step_tokens,
                "" if timeout_seconds is None else str(timeout_seconds),
                "" if max_iterations is None else str(max_iterations),
            ),
            timeout=request_timeout,
        )

    def _notify_sim_advance(self, result: dict[str, Any]) -> None:
        if self._sim_advance_hook is None:
            return
        before = str(result.get("time_before") or "")
        after = str(result.get("time_after") or "")
        if before and after and before != after:
            self._sim_advance_hook(before, after)

    def shutdown(self) -> None:
        try:
            if self.proc is not None:
                try:
                    self._send_raw("catch {quit -force}\nexit\n")
                except XdbError:
                    pass
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(timeout=5)
        finally:
            if self.proc is not None and self._vcd_state is not None:
                try:
                    self.request(
                        r'''
catch {close_vcd}
set __xdb_time [current_time]
xdb_reply_ok_fields $__xdb_request_id "\"time\":[xdb_json_string $__xdb_time],\"active\":false"
'''
                    )
                except XdbError:
                    pass
                self._vcd_state = None
            if self._pty_master_fd is not None:
                try:
                    os.close(self._pty_master_fd)
                except OSError:
                    pass
                self._pty_master_fd = None
            if self._coyote is not None:
                self._coyote.close()
                self._coyote = None
            if self._log_file is not None:
                self._log_file.close()
                self._log_file = None
            self.proc = None
