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

from ..errors import XdbError


def _tcl_string(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("$", "\\$")
        .replace("[", "\\[")
        .replace("]", "\\]")
    )
    return f'"{escaped}"'


def _tcl_list(values: list[str]) -> str:
    if not values:
        return "[list]"
    return "[list " + " ".join(_tcl_string(v) for v in values) + "]"


_HELPERS_TCL = r'''
proc xdb_json_escape {s} {
  return [string map [list "\\" "\\\\" "\"" "\\\"" "\n" "\\n" "\r" "\\r" "\t" "\\t"] $s]
}

proc xdb_json_string {s} {
  return "\"[xdb_json_escape $s]\""
}

proc xdb_json_array_strings {items} {
  set parts {}
  foreach item $items {
    lappend parts [xdb_json_string $item]
  }
  return "\[[join $parts ,]\]"
}

proc xdb_json_signal_values {items} {
  set parts {}
  foreach item $items {
    set value ""
    if {[catch {set value [get_value $item]} err]} {
      set value $err
    }
    lappend parts "{\"name\":[xdb_json_string $item],\"value\":[xdb_json_string $value]}"
  }
  return "\[[join $parts ,]\]"
}

proc xdb_reply_json {request_id payload} {
  puts "__XDB_BEGIN__ $request_id"
  puts $payload
  puts "__XDB_END__ $request_id"
  flush stdout
}

proc xdb_reply_ok_fields {request_id fields} {
  if {$fields eq ""} {
    xdb_reply_json $request_id "{\"ok\":true}"
  } else {
    xdb_reply_json $request_id "{\"ok\":true,$fields}"
  }
}

proc xdb_reply_error {request_id msg} {
  xdb_reply_json $request_id "{\"ok\":false,\"error\":[xdb_json_string $msg]}"
}

if {![info exists ::xdb_breakpoints]} {
  set ::xdb_breakpoints {}
}
'''


class VivadoSimDriver:
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
        self.proc: subprocess.Popen[str] | None = None
        self._reader_thread: threading.Thread | None = None
        self._queue: queue.Queue[str] = queue.Queue()
        self._log_file = None
        self._recent_lines: deque[str] = deque(maxlen=80)
        self._pty_master_fd: int | None = None

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
        self._send_raw(_HELPERS_TCL)
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
            for idx, token in enumerate(tokens):
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

    def request(self, body_tcl: str, timeout: int = 120) -> dict:
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

    def _await_response(self, request_id: str, timeout: int) -> dict:
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
                data = json.loads(payload)
                if not data.get("ok", False):
                    raise XdbError(str(data.get("error", "simulation request failed")))
                return data
            if in_block:
                payload_lines.append(line)

    def launch(self, timeout: int = 300, top: str | None = None) -> dict:
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

    def status(self) -> dict:
        return self.time()

    def time(self) -> dict:
        body = r'''
set __xdb_time [current_time]
xdb_reply_ok_fields $__xdb_request_id "\"time\":[xdb_json_string $__xdb_time]"
'''
        return self.request(body)

    def run(self, tokens: list[str]) -> dict:
        body = fr'''
set __xdb_before [current_time]
set __xdb_args {_tcl_list(tokens)}
if {{[llength $__xdb_args] == 0}} {{
  run
}} else {{
  eval [linsert $__xdb_args 0 run]
}}
set __xdb_after [current_time]
set __xdb_joined [join $__xdb_args " "]
xdb_reply_ok_fields $__xdb_request_id "\"time_before\":[xdb_json_string $__xdb_before],\"time_after\":[xdb_json_string $__xdb_after],\"duration\":[xdb_json_string $__xdb_joined]"
'''
        return self.request(body)

    def restart(self) -> dict:
        body = r'''
set __xdb_before [current_time]
restart -force
set __xdb_after [current_time]
xdb_reply_ok_fields $__xdb_request_id "\"time_before\":[xdb_json_string $__xdb_before],\"time_after\":[xdb_json_string $__xdb_after]"
'''
        return self.request(body)

    def get_signal(self, signal: str) -> dict:
        body = fr'''
set __xdb_signal {_tcl_string(signal)}
set __xdb_value [get_value $__xdb_signal]
xdb_reply_ok_fields $__xdb_request_id "\"signal\":[xdb_json_string $__xdb_signal],\"value\":[xdb_json_string $__xdb_value]"
'''
        return self.request(body)

    def get_many(self, pattern: str) -> dict:
        body = fr'''
set __xdb_pattern {_tcl_string(pattern)}
set __xdb_items [get_objects $__xdb_pattern]
xdb_reply_ok_fields $__xdb_request_id "\"pattern\":[xdb_json_string $__xdb_pattern],\"signals\":[xdb_json_signal_values $__xdb_items]"
'''
        return self.request(body)

    def scopes(self, scope: str | None) -> dict:
        pattern = "*" if not scope else f"{scope}/*"
        body = fr'''
set __xdb_scope {_tcl_string(scope or "")}
set __xdb_pattern {_tcl_string(pattern)}
set __xdb_scopes [get_scopes $__xdb_pattern]
xdb_reply_ok_fields $__xdb_request_id "\"scope\":[xdb_json_string $__xdb_scope],\"scopes\":[xdb_json_array_strings $__xdb_scopes]"
'''
        return self.request(body)

    def objects(self, scope: str) -> dict:
        body = fr'''
set __xdb_scope {_tcl_string(scope)}
set __xdb_pattern [format "%s/*" $__xdb_scope]
set __xdb_objects [get_objects $__xdb_pattern]
xdb_reply_ok_fields $__xdb_request_id "\"scope\":[xdb_json_string $__xdb_scope],\"objects\":[xdb_json_array_strings $__xdb_objects]"
'''
        return self.request(body)

    def set_top(self, top: str, timeout: int = 300) -> dict:
        if top != self.top:
            raise XdbError("changing top module is not supported for runtime-backed simulation sessions")
        data = self.launch(timeout=timeout, top=top)
        data["relaunched"] = False
        return data

    def add_wave(self, pattern: str) -> dict:
        body = fr'''
set __xdb_pattern {_tcl_string(pattern)}
set __xdb_objects [get_objects $__xdb_pattern]
foreach __xdb_object $__xdb_objects {{
  catch {{log_wave $__xdb_object}}
  catch {{add_wave $__xdb_object}}
}}
set __xdb_count [llength $__xdb_objects]
xdb_reply_ok_fields $__xdb_request_id "\"pattern\":[xdb_json_string $__xdb_pattern],\"count\":$__xdb_count,\"objects\":[xdb_json_array_strings $__xdb_objects]"
'''
        return self.request(body)

    def step(self, count: int | None = None, time_tokens: list[str] | None = None) -> dict:
        if time_tokens:
            body = fr'''
set __xdb_before [current_time]
set __xdb_args {_tcl_list(time_tokens)}
eval [linsert $__xdb_args 0 run]
set __xdb_after [current_time]
set __xdb_joined [join $__xdb_args " "]
xdb_reply_ok_fields $__xdb_request_id "\"time_before\":[xdb_json_string $__xdb_before],\"time_after\":[xdb_json_string $__xdb_after],\"duration\":[xdb_json_string $__xdb_joined],\"step_mode\":[xdb_json_string "time"]"
'''
            return self.request(body)

        step_count = count or 1
        body = fr'''
set __xdb_count {step_count}
set __xdb_before [current_time]
for {{set __xdb_i 0}} {{$__xdb_i < $__xdb_count}} {{incr __xdb_i}} {{
  step
}}
set __xdb_after [current_time]
xdb_reply_ok_fields $__xdb_request_id "\"time_before\":[xdb_json_string $__xdb_before],\"time_after\":[xdb_json_string $__xdb_after],\"count\":$__xdb_count,\"step_mode\":[xdb_json_string "count"]"
'''
        return self.request(body)

    def add_breakpoint(self, condition: str) -> dict:
        body = fr'''
set __xdb_condition {_tcl_string(condition)}
set __xdb_id [when $__xdb_condition {{stop}}]
lappend ::xdb_breakpoints $__xdb_id
xdb_reply_ok_fields $__xdb_request_id "\"condition\":[xdb_json_string $__xdb_condition],\"breakpoint_id\":[xdb_json_string $__xdb_id]"
'''
        return self.request(body)

    def clear_breakpoints(self) -> dict:
        body = r'''
set __xdb_cleared 0
foreach __xdb_id $::xdb_breakpoints {
  if {![catch {nowhen $__xdb_id}]} {
    incr __xdb_cleared
  }
}
set ::xdb_breakpoints {}
xdb_reply_ok_fields $__xdb_request_id "\"cleared\":$__xdb_cleared"
'''
        return self.request(body)

    def shutdown(self) -> None:
        if self.proc is None:
            return
        try:
            self._send_raw("catch {quit -force}\nexit\n")
        except XdbError:
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)
        if self._pty_master_fd is not None:
            try:
                os.close(self._pty_master_fd)
            except OSError:
                pass
            self._pty_master_fd = None
        if self._log_file is not None:
            self._log_file.close()
            self._log_file = None
        self.proc = None
