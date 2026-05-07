from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
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
    ):
        self.project = str(Path(project).resolve())
        self.simset = simset
        self.mode = mode
        self.top = top
        self.vivado_log_path = vivado_log_path
        self.proc: subprocess.Popen[str] | None = None
        self._reader_thread: threading.Thread | None = None
        self._queue: queue.Queue[str] = queue.Queue()
        self._log_file = None
        self._recent_lines: deque[str] = deque(maxlen=80)

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
        vivado_bin = os.environ.get("XDB_VIVADO_BIN", "vivado")
        cmd = [vivado_bin, "-mode", "tcl", "-nolog", "-nojournal", "-notrace"]
        try:
            self.proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError as e:
            raise XdbError(
                "vivado executable not found in PATH. Run inside a Xilinx-enabled shell "
                "or set XDB_VIVADO_BIN."
            ) from e

        Path(self.vivado_log_path).parent.mkdir(parents=True, exist_ok=True)
        self._log_file = open(self.vivado_log_path, "a", encoding="utf-8", buffering=1)
        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()
        self._send_raw(_HELPERS_TCL)
        self.launch(timeout=timeout)

    def _reader_loop(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        for line in self.proc.stdout:
            text = line.rstrip("\n")
            if self._log_file is not None:
                self._log_file.write(text + "\n")
            self._recent_lines.append(text)
            self._queue.put(text)

    def _send_raw(self, script: str) -> None:
        if self.proc is None or self.proc.stdin is None:
            raise XdbError("vivado simulation process is not running")
        try:
            self.proc.stdin.write(script)
            if not script.endswith("\n"):
                self.proc.stdin.write("\n")
            self.proc.stdin.flush()
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
        body = f'''
set __xdb_project {_tcl_string(self.project)}
set __xdb_simset {_tcl_string(self.simset)}
set __xdb_mode {_tcl_string(self.mode)}
set __xdb_top {_tcl_string(effective_top)}
catch {{close_sim -force}}
catch {{close_project}}
open_project $__xdb_project
set __xdb_fileset [get_filesets $__xdb_simset]
if {{[llength $__xdb_fileset] == 0}} {{
  error "simulation fileset not found: $__xdb_simset"
}}
if {{$__xdb_top ne ""}} {{
  set_property top $__xdb_top $__xdb_fileset
}}
if {{$__xdb_mode eq "behavioral"}} {{
  launch_simulation -simset $__xdb_simset -mode behavioral
}} elseif {{$__xdb_mode eq "post-synth"}} {{
  launch_simulation -simset $__xdb_simset -mode post-synthesis
}} elseif {{$__xdb_mode eq "post-impl"}} {{
  launch_simulation -simset $__xdb_simset -mode post-implementation
}} else {{
  error "unsupported simulation mode: $__xdb_mode"
}}
set __xdb_effective_top [get_property top $__xdb_fileset]
set __xdb_time [current_time]
set ::xdb_breakpoints {{}}
xdb_reply_ok_fields $__xdb_request_id "\"project\":[xdb_json_string $__xdb_project],\"simset\":[xdb_json_string $__xdb_simset],\"mode\":[xdb_json_string $__xdb_mode],\"top\":[xdb_json_string $__xdb_effective_top],\"time\":[xdb_json_string $__xdb_time]"
'''
        data = self.request(body, timeout=timeout)
        self.top = str(data.get("top", effective_top))
        return data

    def status(self) -> dict:
        return self.time()

    def time(self) -> dict:
        body = r'''
set __xdb_time [current_time]
xdb_reply_ok_fields $__xdb_request_id "\"time\":[xdb_json_string $__xdb_time]"
'''
        return self.request(body)

    def run(self, tokens: list[str]) -> dict:
        body = f'''
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
        body = f'''
set __xdb_signal {_tcl_string(signal)}
set __xdb_value [get_value $__xdb_signal]
xdb_reply_ok_fields $__xdb_request_id "\"signal\":[xdb_json_string $__xdb_signal],\"value\":[xdb_json_string $__xdb_value]"
'''
        return self.request(body)

    def get_many(self, pattern: str) -> dict:
        body = f'''
set __xdb_pattern {_tcl_string(pattern)}
set __xdb_items [get_objects $__xdb_pattern]
xdb_reply_ok_fields $__xdb_request_id "\"pattern\":[xdb_json_string $__xdb_pattern],\"signals\":[xdb_json_signal_values $__xdb_items]"
'''
        return self.request(body)

    def scopes(self, scope: str | None) -> dict:
        pattern = "*" if not scope else f"{scope}/*"
        body = f'''
set __xdb_scope {_tcl_string(scope or "")}
set __xdb_pattern {_tcl_string(pattern)}
set __xdb_scopes [get_scopes $__xdb_pattern]
xdb_reply_ok_fields $__xdb_request_id "\"scope\":[xdb_json_string $__xdb_scope],\"scopes\":[xdb_json_array_strings $__xdb_scopes]"
'''
        return self.request(body)

    def objects(self, scope: str) -> dict:
        body = f'''
set __xdb_scope {_tcl_string(scope)}
set __xdb_pattern [format "%s/*" $__xdb_scope]
set __xdb_objects [get_objects $__xdb_pattern]
xdb_reply_ok_fields $__xdb_request_id "\"scope\":[xdb_json_string $__xdb_scope],\"objects\":[xdb_json_array_strings $__xdb_objects]"
'''
        return self.request(body)

    def set_top(self, top: str, timeout: int = 300) -> dict:
        data = self.launch(timeout=timeout, top=top)
        data["relaunched"] = True
        return data

    def add_wave(self, pattern: str) -> dict:
        body = f'''
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
            body = f'''
set __xdb_before [current_time]
set __xdb_args {_tcl_list(time_tokens)}
eval [linsert $__xdb_args 0 run]
set __xdb_after [current_time]
set __xdb_joined [join $__xdb_args " "]
xdb_reply_ok_fields $__xdb_request_id "\"time_before\":[xdb_json_string $__xdb_before],\"time_after\":[xdb_json_string $__xdb_after],\"duration\":[xdb_json_string $__xdb_joined],\"step_mode\":[xdb_json_string "time"]"
'''
            return self.request(body)

        step_count = count or 1
        body = f'''
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
        body = f'''
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
            self._send_raw("catch {close_sim -force}\ncatch {close_project}\nexit\n")
        except XdbError:
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)
        if self._log_file is not None:
            self._log_file.close()
            self._log_file = None
        self.proc = None
