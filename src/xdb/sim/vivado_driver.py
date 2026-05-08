from __future__ import annotations

import json
import os
import pty
import queue
import re
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
from .coyote import (
    CoyoteSimController,
    MAX_TRANSFER_SIZE,
    ensure_supported_local_opcode,
    parse_stream_name,
)


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

proc xdb_json_nullable_string {s} {
  if {$s eq ""} {
    return "null"
  }
  return [xdb_json_string $s]
}

proc xdb_safe_get_property {prop item} {
  if {[catch {set value [get_property $prop $item]}]} {
    return ""
  }
  return [string trim $value]
}

proc xdb_parent_scope {path} {
  set normalized [string trim $path]
  if {$normalized eq ""} {
    return ""
  }
  set last_sep [string last "/" $normalized]
  if {$last_sep <= 0} {
    return ""
  }
  return [string range $normalized 0 [expr {$last_sep - 1}]]
}

proc xdb_basename {path} {
  set normalized [string trim $path]
  if {$normalized eq ""} {
    return ""
  }
  set last_sep [string last "/" $normalized]
  if {$last_sep < 0} {
    return $normalized
  }
  return [string range $normalized [expr {$last_sep + 1}] end]
}

proc xdb_normalize_kind {raw_kind default_kind} {
  set raw [string tolower [string trim $raw_kind]]
  if {$raw eq ""} {
    return $default_kind
  }
  if {[string match "*parameter*" $raw]} {
    return "parameter"
  }
  if {[string match "*interface*" $raw]} {
    return "interface"
  }
  if {[string match "*module*" $raw] || [string match "*scope*" $raw]} {
    return "module"
  }
  if {[string match "*reg*" $raw]} {
    return "reg"
  }
  if {[string match "*net*" $raw] || [string match "*wire*" $raw]} {
    return "net"
  }
  if {
    [string match "*signal*" $raw] ||
    [string match "*logic*" $raw] ||
    [string match "*variable*" $raw] ||
    [string match "*port*" $raw]
  } {
    return "signal"
  }
  return $raw
}

proc xdb_infer_radix {value} {
  set trimmed [string trim $value]
  if {$trimmed eq ""} {
    return ""
  }
  if {[regexp {^0x[0-9a-fA-F]+$} $trimmed]} {
    return "hex"
  }
  if {[regexp {^0b[01xXzZ_]+$} $trimmed]} {
    return "bin"
  }
  if {[regexp {^0o[0-7_]+$} $trimmed]} {
    return "oct"
  }
  if {[regexp {^0d[0-9_]+$} $trimmed]} {
    return "dec"
  }
  if {[regexp {^[0-9]+'([bBoOdDhH])} $trimmed -> radix]} {
    switch -nocase -- $radix {
      b { return "bin" }
      o { return "oct" }
      d { return "dec" }
      h { return "hex" }
    }
  }
  if {[regexp {^[0-9_]+$} $trimmed]} {
    return "dec"
  }
  return ""
}

proc xdb_object_width {item} {
  foreach prop {SIZE WIDTH BIT_WIDTH BUS_WIDTH} {
    set value [xdb_safe_get_property $prop $item]
    if {$value ne "" && [string is integer -strict $value]} {
      return $value
    }
  }

  set left [xdb_safe_get_property LEFT $item]
  set right [xdb_safe_get_property RIGHT $item]
  if {
    $left ne "" &&
    $right ne "" &&
    [string is integer -strict $left] &&
    [string is integer -strict $right]
  } {
    return [expr {abs($left - $right) + 1}]
  }

  if {![catch {set value [get_value -radix bin $item]}]} {
    set normalized [string map {_ ""} [string trim $value]]
    if {[regexp {^[01xXzZ]+$} $normalized]} {
      return [string length $normalized]
    }
    if {[regexp {^[0-9]+'[bB]([01xXzZ]+)$} $normalized -> bits]} {
      return [string length $bits]
    }
  }

  return ""
}

proc xdb_json_object_metadata {item default_kind include_value} {
  set path [string trim $item]
  set raw_kind ""
  foreach prop {TYPE CLASS OBJECT_CLASS OBJECT_TYPE} {
    set candidate [xdb_safe_get_property $prop $item]
    if {$candidate ne ""} {
      set raw_kind $candidate
      break
    }
  }
  set kind [xdb_normalize_kind $raw_kind $default_kind]
  set width [xdb_object_width $item]
  set parent_scope [xdb_parent_scope $path]
  set value ""
  set value_radix ""
  set has_value 0
  if {$include_value} {
    if {![catch {set value [get_value $item]}]} {
      set has_value 1
      set value_radix [xdb_infer_radix $value]
    }
  }

  set fields {}
  lappend fields "\"path\":[xdb_json_string $path]"
  lappend fields "\"kind\":[xdb_json_string $kind]"
  lappend fields "\"raw_kind\":[xdb_json_nullable_string $raw_kind]"
  if {$width eq ""} {
    lappend fields "\"width\":null"
  } else {
    lappend fields "\"width\":$width"
  }
  lappend fields "\"parent_scope\":[xdb_json_nullable_string $parent_scope]"
  if {$include_value && $has_value} {
    lappend fields "\"value\":[xdb_json_string $value]"
    lappend fields "\"value_radix\":[xdb_json_nullable_string $value_radix]"
  } else {
    lappend fields "\"value\":null"
    lappend fields "\"value_radix\":null"
  }
  return "\{[join $fields ,]\}"
}

proc xdb_json_object_metadata_array {items default_kind include_value} {
  set parts {}
  foreach item $items {
    lappend parts [xdb_json_object_metadata $item $default_kind $include_value]
  }
  return "\[[join $parts ,]\]"
}

proc xdb_collect_snapshot_value_objects {root_scope} {
  set pending [list [string trim $root_scope]]
  set visited_scopes [dict create]
  set seen_objects [dict create]
  set out {}

  while {[llength $pending] > 0} {
    set scope [lindex $pending 0]
    set pending [lrange $pending 1 end]
    if {$scope eq ""} {
      continue
    }
    if {[dict exists $visited_scopes $scope]} {
      continue
    }
    dict set visited_scopes $scope 1

    set pattern [format "%s/*" $scope]
    set scope_objects {}
    if {![catch {set scope_objects [get_objects $pattern]}]} {
      foreach item $scope_objects {
        if {[catch {get_value $item}]} {
          continue
        }
        if {![dict exists $seen_objects $item]} {
          dict set seen_objects $item 1
          lappend out $item
        }
      }
    }

    set child_scopes {}
    if {![catch {set child_scopes [get_scopes $pattern]}]} {
      foreach child_scope $child_scopes {
        if {![dict exists $visited_scopes $child_scope]} {
          lappend pending $child_scope
        }
      }
    }
  }

  return $out
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

proc xdb_normalize_expr {expr_text} {
  set normalized [string trim $expr_text]
  if {[string length $normalized] >= 4 && [string range $normalized 0 3] eq "expr"} {
    set rest [string trim [string range $normalized 4 end]]
    if {$rest ne ""} {
      set normalized $rest
    }
  }
  if {[string length $normalized] >= 2 && [string index $normalized 0] eq "{" && [string index $normalized end] eq "}"} {
    set normalized [string range $normalized 1 end-1]
  }
  return [string trim $normalized]
}

proc xdb_signal_eq_expr {signal expected} {
  return [format {[string equal [get_value %s] %s]} [list $signal] [list $expected]]
}

proc xdb_signal_change_expr {signal initial} {
  return [format {![string equal [get_value %s] %s]} [list $signal] [list $initial]]
}

if {![info exists ::xdb_breakpoints]} {
  set ::xdb_breakpoints {}
}

if {![info exists ::xdb_forces]} {
  set ::xdb_forces [dict create]
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
        self._coyote: CoyoteSimController | None = None
        self._snapshots: dict[str, dict] = {}
        self._vcd_state: dict | None = None

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
        self._prepare_coyote_runtime()
        self._run_script(self.compile_script, cwd=self.work_dir)
        self._run_script(self.elaborate_script, cwd=self.work_dir)

    def _prepare_coyote_runtime(self) -> None:
        if not self.runtime_root:
            return
        runtime_root = Path(self.runtime_root)
        lynx_pkg = runtime_root / "lynx_pkg.sv"
        if not lynx_pkg.is_file():
            return
        try:
            text = lynx_pkg.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            raise XdbError(f"failed to read Coyote runtime file: {lynx_pkg}") from e
        replaced = text.replace(
            'localparam string BUILD_DIR = "/build/source/.nix-hw-v80";',
            f'localparam string BUILD_DIR = "{runtime_root}";',
        )
        replaced = replaced.replace(
            'localparam string BUILD_DIR = "/build/source/.nix-hw-u280";',
            f'localparam string BUILD_DIR = "{runtime_root}";',
        )
        if replaced == text:
            replaced = re.sub(
                r'localparam\s+string\s+BUILD_DIR\s*=\s*"[^"]*"\s*;',
                f'localparam string BUILD_DIR = "{runtime_root}";',
                text,
                count=1,
            )
        if replaced != text:
            try:
                lynx_pkg.write_text(replaced, encoding="utf-8")
            except OSError as e:
                raise XdbError(f"failed to rewrite Coyote runtime file: {lynx_pkg}") from e
        if self._coyote is not None:
            self._coyote.close()
        self._coyote = CoyoteSimController(str(runtime_root))
        self._coyote.start()

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

    @staticmethod
    def _infer_known_signal_paths(objects: list[dict], patterns: list[str]) -> list[str]:
        compiled = [re.compile(pattern, re.IGNORECASE) for pattern in patterns]
        out: list[str] = []
        seen: set[str] = set()
        for obj in objects:
            path = str(obj.get("path") or "")
            if not path or path in seen:
                continue
            base = path.rsplit("/", 1)[-1]
            if any(regex.search(base) for regex in compiled):
                seen.add(path)
                out.append(path)
        return out

    @staticmethod
    def _infer_dut_scope(top_scope: str, child_scopes: list[str]) -> str | None:
        if not child_scopes:
            return None
        preferred: list[tuple[int, str]] = []
        for scope in child_scopes:
            base = scope.rsplit("/", 1)[-1]
            score = None
            lowered = base.lower()
            if lowered == "dut":
                score = 0
            elif lowered == "inst_dut":
                score = 1
            elif "dut" in lowered:
                score = 2
            elif lowered.startswith("inst_"):
                score = 3
            if score is not None:
                preferred.append((score, scope))
        if preferred:
            preferred.sort(key=lambda item: (item[0], len(item[1]), item[1]))
            return preferred[0][1]
        return child_scopes[0] if child_scopes else (top_scope or None)

    def describe_session(self) -> dict:
        body = fr'''
set __xdb_top_name {_tcl_string(self.top)}
set __xdb_time [current_time]
set __xdb_root_scopes [get_scopes *]
set __xdb_top_scope ""
foreach __xdb_scope $__xdb_root_scopes {{
  if {{[xdb_basename $__xdb_scope] eq $__xdb_top_name}} {{
    set __xdb_top_scope $__xdb_scope
    break
  }}
}}
if {{$__xdb_top_scope eq "" && [llength $__xdb_root_scopes] == 1}} {{
  set __xdb_top_scope [lindex $__xdb_root_scopes 0]
}}
if {{$__xdb_top_scope eq ""}} {{
  set __xdb_top_scope $__xdb_top_name
}}
set __xdb_child_scopes {{}}
catch {{set __xdb_child_scopes [get_scopes [format "%s/*" $__xdb_top_scope]]}}
set __xdb_objects [xdb_collect_snapshot_value_objects $__xdb_top_scope]
xdb_reply_ok_fields $__xdb_request_id "\"top\":[xdb_json_string $__xdb_top_name],\"top_scope\":[xdb_json_string $__xdb_top_scope],\"time\":[xdb_json_string $__xdb_time],\"root_scopes\":[xdb_json_array_strings $__xdb_root_scopes],\"child_scopes\":[xdb_json_array_strings $__xdb_child_scopes],\"child_scope_metadata\":[xdb_json_object_metadata_array $__xdb_child_scopes \"module\" 0],\"objects\":[xdb_json_object_metadata_array $__xdb_objects \"signal\" 1]"
'''
        result = self.request(body)
        objects = list(result.get("objects") or [])
        top_scope = str(result.get("top_scope") or "")
        child_scopes = [str(scope) for scope in list(result.get("child_scopes") or [])]
        clocks = self._infer_known_signal_paths(
            objects,
            [r"(^|_)(clk|clock)(_|$)", r"(^|_)(aclk)(_|$)"],
        )
        resets = self._infer_known_signal_paths(
            objects,
            [r"(^|_)(rst|reset|aresetn|resetn|srst|rstn)(_|$)"],
        )
        dut_scope = self._infer_dut_scope(top_scope, child_scopes)
        common_scopes = [scope for scope in [top_scope, *child_scopes] if scope]
        return {
            "top": result.get("top", self.top),
            "top_scope": top_scope,
            "time": result.get("time", ""),
            "dut": dut_scope,
            "clocks": clocks,
            "resets": resets,
            "common_scopes": common_scopes,
            "root_scopes": result.get("root_scopes", []),
            "child_scopes": child_scopes,
            "child_scope_metadata": result.get("child_scope_metadata", []),
            "project": self.project,
            "simset": self.simset,
            "mode": self.mode,
            "runtime_root": self.runtime_root,
            "work_dir": self.work_dir,
            "coyote": self._coyote is not None,
        }

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
restart
set __xdb_after [current_time]
xdb_reply_ok_fields $__xdb_request_id "\"time_before\":[xdb_json_string $__xdb_before],\"time_after\":[xdb_json_string $__xdb_after]"
'''
        return self.request(body)

    def get_signal(self, signal: str) -> dict:
        body = fr'''
set __xdb_signal {_tcl_string(signal)}
set __xdb_value [get_value $__xdb_signal]
set __xdb_object_json [xdb_json_object_metadata $__xdb_signal "signal" 1]
xdb_reply_ok_fields $__xdb_request_id "\"signal\":[xdb_json_string $__xdb_signal],\"value\":[xdb_json_string $__xdb_value],\"object\":$__xdb_object_json"
'''
        return self.request(body)

    def get_many(self, pattern: str) -> dict:
        body = fr'''
set __xdb_pattern {_tcl_string(pattern)}
set __xdb_items [get_objects $__xdb_pattern]
xdb_reply_ok_fields $__xdb_request_id "\"pattern\":[xdb_json_string $__xdb_pattern],\"signals\":[xdb_json_signal_values $__xdb_items],\"objects\":[xdb_json_object_metadata_array $__xdb_items \"signal\" 1]"
'''
        return self.request(body)

    def read_signals(self, signals: list[str]) -> dict:
        body = fr'''
set __xdb_signals {_tcl_list(signals)}
xdb_reply_ok_fields $__xdb_request_id "\"signals\":[xdb_json_object_metadata_array $__xdb_signals \"signal\" 1]"
'''
        return self.request(body)

    def scopes(self, scope: str | None) -> dict:
        pattern = "*" if not scope else f"{scope}/*"
        body = fr'''
set __xdb_scope {_tcl_string(scope or "")}
set __xdb_pattern {_tcl_string(pattern)}
set __xdb_scopes [get_scopes $__xdb_pattern]
xdb_reply_ok_fields $__xdb_request_id "\"scope\":[xdb_json_string $__xdb_scope],\"scopes\":[xdb_json_array_strings $__xdb_scopes],\"metadata\":[xdb_json_object_metadata_array $__xdb_scopes \"module\" 0]"
'''
        return self.request(body)

    def objects(self, scope: str) -> dict:
        body = fr'''
set __xdb_scope {_tcl_string(scope)}
set __xdb_pattern [format "%s/*" $__xdb_scope]
set __xdb_objects [get_objects $__xdb_pattern]
xdb_reply_ok_fields $__xdb_request_id "\"scope\":[xdb_json_string $__xdb_scope],\"objects\":[xdb_json_array_strings $__xdb_objects],\"metadata\":[xdb_json_object_metadata_array $__xdb_objects \"signal\" 1]"
'''
        return self.request(body)

    def snapshot_scope(self, scope: str, *, name: str | None = None) -> dict:
        body = fr'''
set __xdb_scope {_tcl_string(scope)}
set __xdb_objects [xdb_collect_snapshot_value_objects $__xdb_scope]
set __xdb_time [current_time]
xdb_reply_ok_fields $__xdb_request_id "\"scope\":[xdb_json_string $__xdb_scope],\"time\":[xdb_json_string $__xdb_time],\"objects\":[xdb_json_object_metadata_array $__xdb_objects \"signal\" 1]"
'''
        result = self.request(body)
        snapshot_id = name or f"snapshot-{uuid.uuid4().hex[:12]}"
        if snapshot_id in self._snapshots:
            raise XdbError(f"snapshot already exists: {snapshot_id}")
        stored = {
            "snapshot": snapshot_id,
            "scope": result.get("scope", scope),
            "time": result.get("time", ""),
            "objects": list(result.get("objects") or []),
        }
        stored["count"] = len(stored["objects"])
        self._snapshots[snapshot_id] = stored
        return dict(stored)

    @staticmethod
    def _diff_snapshot_payload(before: dict, after: dict) -> dict:
        before_map = {str(obj.get("path")): obj for obj in list(before.get("objects") or [])}
        after_map = {str(obj.get("path")): obj for obj in list(after.get("objects") or [])}
        added_paths = sorted(set(after_map) - set(before_map))
        removed_paths = sorted(set(before_map) - set(after_map))
        shared_paths = sorted(set(before_map) & set(after_map))

        changed = []
        unchanged_count = 0
        compare_fields = ["kind", "width", "value", "value_radix", "parent_scope"]
        for path in shared_paths:
            old_obj = before_map[path]
            new_obj = after_map[path]
            changed_fields = [field for field in compare_fields if old_obj.get(field) != new_obj.get(field)]
            if changed_fields:
                changed.append(
                    {
                        "path": path,
                        "fields": changed_fields,
                        "before": old_obj,
                        "after": new_obj,
                    }
                )
            else:
                unchanged_count += 1

        return {
            "before": str(before.get("snapshot") or ""),
            "after": str(after.get("snapshot") or ""),
            "scope_before": str(before.get("scope") or ""),
            "scope_after": str(after.get("scope") or ""),
            "time_before": str(before.get("time") or ""),
            "time_after": str(after.get("time") or ""),
            "added": [after_map[path] for path in added_paths],
            "removed": [before_map[path] for path in removed_paths],
            "changed": changed,
            "unchanged_count": unchanged_count,
            "before_count": len(before_map),
            "after_count": len(after_map),
            "changed_count": len(changed),
            "added_count": len(added_paths),
            "removed_count": len(removed_paths),
        }

    def diff_snapshot(self, before: str, after: str) -> dict:
        before_snapshot = self._snapshots.get(before)
        if before_snapshot is None:
            raise XdbError(f"unknown snapshot: {before}")
        after_snapshot = self._snapshots.get(after)
        if after_snapshot is None:
            raise XdbError(f"unknown snapshot: {after}")
        return self._diff_snapshot_payload(before_snapshot, after_snapshot)

    def watch_changes(self, scope: str, *, duration_tokens: list[str]) -> dict:
        before_id = f"watch-before-{uuid.uuid4().hex[:10]}"
        after_id = f"watch-after-{uuid.uuid4().hex[:10]}"
        before = self.snapshot_scope(scope, name=before_id)
        run_result = self.run(duration_tokens)
        after = self.snapshot_scope(scope, name=after_id)
        diff = self._diff_snapshot_payload(before, after)
        return {
            "scope": scope,
            "duration": " ".join(duration_tokens),
            "run": run_result,
            "before": before,
            "after": after,
            "diff": diff,
        }

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

    def vcd_start(self, file_path: str, scope: str | None = None) -> dict:
        if self._vcd_state is not None:
            raise XdbError(
                f"a VCD dump is already active: {self._vcd_state.get('file', '<unknown>')}"
            )
        resolved = Path(file_path).expanduser().resolve()
        resolved.parent.mkdir(parents=True, exist_ok=True)
        body = fr'''
set __xdb_path {_tcl_string(str(resolved))}
set __xdb_scope {_tcl_string(scope or "")}
open_vcd $__xdb_path
if {{$__xdb_scope eq ""}} {{
  log_vcd -r /*
}} else {{
  set __xdb_pattern [format "%s/*" $__xdb_scope]
  log_vcd -r $__xdb_pattern
}}
set __xdb_time [current_time]
xdb_reply_ok_fields $__xdb_request_id "\"file\":[xdb_json_string $__xdb_path],\"scope\":[xdb_json_nullable_string $__xdb_scope],\"time\":[xdb_json_string $__xdb_time],\"active\":true"
'''
        result = self.request(body)
        self._vcd_state = {
            "active": True,
            "file": str(resolved),
            "scope": scope,
            "started_at": str(result.get("time") or ""),
        }
        return dict(self._vcd_state)

    def vcd_stop(self) -> dict:
        if self._vcd_state is None:
            return {
                "active": False,
                "stopped": False,
                "file": None,
                "scope": None,
            }
        state = dict(self._vcd_state)
        body = r'''
close_vcd
set __xdb_time [current_time]
xdb_reply_ok_fields $__xdb_request_id "\"time\":[xdb_json_string $__xdb_time],\"active\":false"
'''
        result = self.request(body)
        self._vcd_state = None
        return {
            "active": False,
            "stopped": True,
            "file": state.get("file"),
            "scope": state.get("scope"),
            "started_at": state.get("started_at"),
            "stopped_at": result.get("time"),
        }

    def vcd_status(self) -> dict:
        time_info = self.time()
        if self._vcd_state is None:
            return {
                "active": False,
                "file": None,
                "scope": None,
                "time": time_info.get("time"),
            }
        return {
            "active": True,
            "file": self._vcd_state.get("file"),
            "scope": self._vcd_state.get("scope"),
            "started_at": self._vcd_state.get("started_at"),
            "time": time_info.get("time"),
        }

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

    def wait_until(
        self,
        expr: str,
        *,
        step_tokens: list[str],
        timeout_seconds: float | None = None,
        max_iterations: int | None = None,
    ) -> dict:
        request_timeout = 86400 if timeout_seconds is None else max(86400, int(timeout_seconds) + 60)
        body = fr'''
set __xdb_expr_raw {_tcl_string(expr)}
set __xdb_expr [xdb_normalize_expr $__xdb_expr_raw]
set __xdb_step_args {_tcl_list(step_tokens)}
set __xdb_timeout_seconds {_tcl_string("" if timeout_seconds is None else str(timeout_seconds))}
set __xdb_max_iterations {_tcl_string("" if max_iterations is None else str(max_iterations))}
set __xdb_time_before [current_time]
set __xdb_iterations 0
set __xdb_deadline_ms -1
if {{$__xdb_timeout_seconds ne ""}} {{
  set __xdb_deadline_ms [expr {{[clock milliseconds] + int(1000.0 * $__xdb_timeout_seconds)}}]
}}
while {{1}} {{
  if {{[uplevel #0 [list expr $__xdb_expr]]}} {{
    break
  }}
  if {{$__xdb_max_iterations ne "" && $__xdb_iterations >= $__xdb_max_iterations}} {{
    error [format "condition not met before reaching max iterations (%s)" $__xdb_max_iterations]
  }}
  if {{$__xdb_deadline_ms >= 0 && [clock milliseconds] >= $__xdb_deadline_ms}} {{
    error [format "timed out after %s second(s) while waiting for condition" $__xdb_timeout_seconds]
  }}
  set __xdb_prev_time [current_time]
  eval [linsert $__xdb_step_args 0 run]
  incr __xdb_iterations
  set __xdb_current_time [current_time]
  if {{$__xdb_current_time eq $__xdb_prev_time}} {{
    if {{[uplevel #0 [list expr $__xdb_expr]]}} {{
      break
    }}
    error "condition not met and simulation did not advance while waiting"
  }}
}}
set __xdb_time_after [current_time]
set __xdb_step [join $__xdb_step_args " "]
xdb_reply_ok_fields $__xdb_request_id "\"expr\":[xdb_json_string $__xdb_expr],\"step\":[xdb_json_string $__xdb_step],\"iterations\":$__xdb_iterations,\"time_before\":[xdb_json_string $__xdb_time_before],\"time_after\":[xdb_json_string $__xdb_time_after],\"timeout_seconds\":[xdb_json_string $__xdb_timeout_seconds],\"max_iterations\":[xdb_json_string $__xdb_max_iterations]"
'''
        return self.request(body, timeout=request_timeout)

    def wait_until_signal(
        self,
        signal: str,
        value: str,
        *,
        step_tokens: list[str],
        timeout_seconds: float | None = None,
        max_iterations: int | None = None,
    ) -> dict:
        request_timeout = 86400 if timeout_seconds is None else max(86400, int(timeout_seconds) + 60)
        body = fr'''
set __xdb_signal {_tcl_string(signal)}
set __xdb_expected {_tcl_string(value)}
set __xdb_step_args {_tcl_list(step_tokens)}
set __xdb_timeout_seconds {_tcl_string("" if timeout_seconds is None else str(timeout_seconds))}
set __xdb_max_iterations {_tcl_string("" if max_iterations is None else str(max_iterations))}
set __xdb_time_before [current_time]
set __xdb_iterations 0
set __xdb_deadline_ms -1
if {{$__xdb_timeout_seconds ne ""}} {{
  set __xdb_deadline_ms [expr {{[clock milliseconds] + int(1000.0 * $__xdb_timeout_seconds)}}]
}}
while {{1}} {{
  set __xdb_value [get_value $__xdb_signal]
  if {{$__xdb_value eq $__xdb_expected}} {{
    break
  }}
  if {{$__xdb_max_iterations ne "" && $__xdb_iterations >= $__xdb_max_iterations}} {{
    error [format "signal did not reach expected value before reaching max iterations (%s)" $__xdb_max_iterations]
  }}
  if {{$__xdb_deadline_ms >= 0 && [clock milliseconds] >= $__xdb_deadline_ms}} {{
    error [format "timed out after %s second(s) while waiting for signal" $__xdb_timeout_seconds]
  }}
  set __xdb_prev_time [current_time]
  eval [linsert $__xdb_step_args 0 run]
  incr __xdb_iterations
  set __xdb_current_time [current_time]
  if {{$__xdb_current_time eq $__xdb_prev_time}} {{
    set __xdb_value [get_value $__xdb_signal]
    if {{$__xdb_value eq $__xdb_expected}} {{
      break
    }}
    error "signal did not reach expected value and simulation did not advance while waiting"
  }}
}}
set __xdb_time_after [current_time]
set __xdb_value [get_value $__xdb_signal]
set __xdb_step [join $__xdb_step_args " "]
xdb_reply_ok_fields $__xdb_request_id "\"signal\":[xdb_json_string $__xdb_signal],\"value\":[xdb_json_string $__xdb_value],\"expected\":[xdb_json_string $__xdb_expected],\"step\":[xdb_json_string $__xdb_step],\"iterations\":$__xdb_iterations,\"time_before\":[xdb_json_string $__xdb_time_before],\"time_after\":[xdb_json_string $__xdb_time_after],\"timeout_seconds\":[xdb_json_string $__xdb_timeout_seconds],\"max_iterations\":[xdb_json_string $__xdb_max_iterations]"
'''
        return self.request(body, timeout=request_timeout)

    def assert_signal(self, signal: str, value: str) -> dict:
        body = fr'''
set __xdb_signal {_tcl_string(signal)}
set __xdb_expected {_tcl_string(value)}
set __xdb_actual [get_value $__xdb_signal]
set __xdb_time [current_time]
if {{$__xdb_actual ne $__xdb_expected}} {{
  error [format "assert-signal failed at %s: %s expected %s got %s" $__xdb_time $__xdb_signal $__xdb_expected $__xdb_actual]
}}
xdb_reply_ok_fields $__xdb_request_id "\"passed\":true,\"kind\":[xdb_json_string \"assert-signal\"],\"signal\":[xdb_json_string $__xdb_signal],\"expected\":[xdb_json_string $__xdb_expected],\"value\":[xdb_json_string $__xdb_actual],\"time\":[xdb_json_string $__xdb_time]"
'''
        return self.request(body)

    def assert_tcl(self, expr: str) -> dict:
        body = fr'''
set __xdb_expr_raw {_tcl_string(expr)}
set __xdb_expr [xdb_normalize_expr $__xdb_expr_raw]
set __xdb_time [current_time]
if {{![uplevel #0 [list expr $__xdb_expr]]}} {{
  error [format "assert-tcl failed at %s: %s" $__xdb_time $__xdb_expr]
}}
xdb_reply_ok_fields $__xdb_request_id "\"passed\":true,\"kind\":[xdb_json_string \"assert-tcl\"],\"expr\":[xdb_json_string $__xdb_expr],\"time\":[xdb_json_string $__xdb_time]"
'''
        return self.request(body)

    def expect_signal(self, signal: str, value: str, *, within_tokens: list[str]) -> dict:
        body = fr'''
set __xdb_signal {_tcl_string(signal)}
set __xdb_expected {_tcl_string(value)}
set __xdb_within_args {_tcl_list(within_tokens)}
set __xdb_time_before [current_time]
set __xdb_initial [get_value $__xdb_signal]
if {{$__xdb_initial eq $__xdb_expected}} {{
  xdb_reply_ok_fields $__xdb_request_id "\"passed\":true,\"kind\":[xdb_json_string \"expect-signal\"],\"signal\":[xdb_json_string $__xdb_signal],\"expected\":[xdb_json_string $__xdb_expected],\"initial\":[xdb_json_string $__xdb_initial],\"value\":[xdb_json_string $__xdb_initial],\"within\":[xdb_json_string [join $__xdb_within_args \" \"]],\"time_before\":[xdb_json_string $__xdb_time_before],\"time_after\":[xdb_json_string $__xdb_time_before],\"iterations\":0"
}} else {{
  set ::xdb_expect_signal_hit 0
  set __xdb_condition [xdb_signal_eq_expr $__xdb_signal $__xdb_expected]
  set __xdb_id [when $__xdb_condition {{set ::xdb_expect_signal_hit 1; stop}}]
  eval [linsert $__xdb_within_args 0 run]
  catch {{nowhen $__xdb_id}}
  set __xdb_time_after [current_time]
  set __xdb_actual [get_value $__xdb_signal]
  if {{!$::xdb_expect_signal_hit && $__xdb_actual ne $__xdb_expected}} {{
    error [format "expect-signal failed: %s did not reach %s within %s (start=%s end=%s time_before=%s time_after=%s)" $__xdb_signal $__xdb_expected [join $__xdb_within_args \" \"] $__xdb_initial $__xdb_actual $__xdb_time_before $__xdb_time_after]
  }}
  xdb_reply_ok_fields $__xdb_request_id "\"passed\":true,\"kind\":[xdb_json_string \"expect-signal\"],\"signal\":[xdb_json_string $__xdb_signal],\"expected\":[xdb_json_string $__xdb_expected],\"initial\":[xdb_json_string $__xdb_initial],\"value\":[xdb_json_string $__xdb_actual],\"within\":[xdb_json_string [join $__xdb_within_args \" \"]],\"time_before\":[xdb_json_string $__xdb_time_before],\"time_after\":[xdb_json_string $__xdb_time_after],\"iterations\":1"
}}
'''
        return self.request(body)

    def expect_change(self, signal: str, *, within_tokens: list[str]) -> dict:
        body = fr'''
set __xdb_signal {_tcl_string(signal)}
set __xdb_within_args {_tcl_list(within_tokens)}
set __xdb_time_before [current_time]
set __xdb_initial [get_value $__xdb_signal]
set ::xdb_expect_change_hit 0
set __xdb_condition [xdb_signal_change_expr $__xdb_signal $__xdb_initial]
set __xdb_id [when $__xdb_condition {{set ::xdb_expect_change_hit 1; stop}}]
eval [linsert $__xdb_within_args 0 run]
catch {{nowhen $__xdb_id}}
set __xdb_time_after [current_time]
set __xdb_actual [get_value $__xdb_signal]
if {{!$::xdb_expect_change_hit && $__xdb_actual eq $__xdb_initial}} {{
  error [format "expect-change failed: %s did not change within %s (value=%s time_before=%s time_after=%s)" $__xdb_signal [join $__xdb_within_args \" \"] $__xdb_initial $__xdb_time_before $__xdb_time_after]
}}
xdb_reply_ok_fields $__xdb_request_id "\"passed\":true,\"kind\":[xdb_json_string \"expect-change\"],\"signal\":[xdb_json_string $__xdb_signal],\"initial\":[xdb_json_string $__xdb_initial],\"value\":[xdb_json_string $__xdb_actual],\"within\":[xdb_json_string [join $__xdb_within_args \" \"]],\"time_before\":[xdb_json_string $__xdb_time_before],\"time_after\":[xdb_json_string $__xdb_time_after],\"changed\":true"
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

    def eval_tcl(self, script: str) -> dict:
        body = fr'''
set __xdb_script {_tcl_string(script)}
set __xdb_result [uplevel #0 $__xdb_script]
set __xdb_time [current_time]
xdb_reply_ok_fields $__xdb_request_id "\"result\":[xdb_json_string $__xdb_result],\"time\":[xdb_json_string $__xdb_time]"
'''
        return self.request(body)

    def source_tcl(self, path: str) -> dict:
        body = fr'''
set __xdb_path {_tcl_string(path)}
set __xdb_result [source $__xdb_path]
set __xdb_time [current_time]
xdb_reply_ok_fields $__xdb_request_id "\"path\":[xdb_json_string $__xdb_path],\"result\":[xdb_json_string $__xdb_result],\"time\":[xdb_json_string $__xdb_time]"
'''
        return self.request(body)

    def force(
        self,
        signal: str,
        values: list[str],
        *,
        radix: str | None = None,
        repeat_every: str | None = None,
        cancel_after: str | None = None,
    ) -> dict:
        body = fr'''
set __xdb_signal {_tcl_string(signal)}
set __xdb_values {_tcl_list(values)}
set __xdb_radix {_tcl_string(radix or "")}
set __xdb_repeat_every {_tcl_string(repeat_every or "")}
set __xdb_cancel_after {_tcl_string(cancel_after or "")}
set __xdb_cmd [list add_force]
if {{$__xdb_radix ne ""}} {{
  lappend __xdb_cmd -radix $__xdb_radix
}}
if {{$__xdb_repeat_every ne ""}} {{
  lappend __xdb_cmd -repeat_every $__xdb_repeat_every
}}
if {{$__xdb_cancel_after ne ""}} {{
  lappend __xdb_cmd -cancel_after $__xdb_cancel_after
}}
lappend __xdb_cmd $__xdb_signal
set __xdb_force_id [uplevel #0 [concat $__xdb_cmd $__xdb_values]]
dict lappend ::xdb_forces $__xdb_signal $__xdb_force_id
set __xdb_time [current_time]
xdb_reply_ok_fields $__xdb_request_id "\"signal\":[xdb_json_string $__xdb_signal],\"force_id\":[xdb_json_string $__xdb_force_id],\"time\":[xdb_json_string $__xdb_time]"
'''
        return self.request(body)

    def release(self, signal: str | None = None, *, all_forces: bool = False) -> dict:
        if all_forces:
            body = r'''
set __xdb_released 0
if {[info exists ::xdb_forces]} {
  dict for {__xdb_signal __xdb_ids} $::xdb_forces {
    incr __xdb_released [llength $__xdb_ids]
  }
}
remove_forces -all
set ::xdb_forces [dict create]
set __xdb_time [current_time]
xdb_reply_ok_fields $__xdb_request_id "\"all\":true,\"released\":$__xdb_released,\"time\":[xdb_json_string $__xdb_time]"
'''
            return self.request(body)

        body = fr'''
set __xdb_signal {_tcl_string(signal or "")}
set __xdb_released 0
if {{[dict exists $::xdb_forces $__xdb_signal]}} {{
  set __xdb_ids [dict get $::xdb_forces $__xdb_signal]
  foreach __xdb_id $__xdb_ids {{
    if {{![catch {{remove_forces $__xdb_id}}]}} {{
      incr __xdb_released
    }}
  }}
  dict unset ::xdb_forces $__xdb_signal
}}
set __xdb_time [current_time]
xdb_reply_ok_fields $__xdb_request_id "\"signal\":[xdb_json_string $__xdb_signal],\"released\":$__xdb_released,\"time\":[xdb_json_string $__xdb_time]"
'''
        return self.request(body)

    def _require_coyote(self) -> CoyoteSimController:
        if self._coyote is None:
            raise XdbError(
                "Coyote simulation protocol is not available for this simulation runtime"
            )
        return self._coyote

    def _coyote_pump_step(self) -> None:
        self.run(["10", "ns"])

    def _coyote_wait_for_item(
        self,
        getter,
        *,
        timeout_seconds: float | None,
        description: str,
    ):
        deadline = None if timeout_seconds is None else time.monotonic() + timeout_seconds
        while True:
            item = getter()
            if item is not None:
                return item
            if deadline is not None and time.monotonic() >= deadline:
                raise XdbError(f"timed out waiting for {description}")
            self._coyote_pump_step()

    def coyote_status(self) -> dict:
        return self._require_coyote().status()

    def coyote_csr_read(self, addr: int, *, timeout_seconds: float | None = None) -> dict:
        controller = self._require_coyote()
        controller.write_input(
            controller.encode_csr_read(addr),
            pump=self._coyote_pump_step,
        )
        value = self._coyote_wait_for_item(
            controller.get_csr_result_nowait,
            timeout_seconds=timeout_seconds,
            description=f"CSR read response at 0x{addr:x}",
        )
        return {
            "addr": addr,
            "addr_hex": f"0x{addr:x}",
            "value": value,
            "value_hex": f"0x{value:x}",
        }

    def coyote_csr_write(self, addr: int, value: int) -> dict:
        controller = self._require_coyote()
        controller.write_input(
            controller.encode_csr_write(addr, value),
            pump=self._coyote_pump_step,
        )
        self._coyote_pump_step()
        return {
            "addr": addr,
            "addr_hex": f"0x{addr:x}",
            "value": value,
            "value_hex": f"0x{value:x}",
            "written": True,
        }

    def coyote_mem_map(self, space: str, addr: int, size: int) -> dict:
        if space != "host":
            raise XdbError("only host memory is currently supported")
        controller = self._require_coyote()
        result = controller.map_host_memory(addr, size)
        self._coyote_pump_step()
        return result

    def coyote_mem_unmap(self, space: str, addr: int) -> dict:
        if space != "host":
            raise XdbError("only host memory is currently supported")
        controller = self._require_coyote()
        result = controller.unmap_host_memory(addr)
        self._coyote_pump_step()
        return result

    def coyote_mem_write(self, space: str, addr: int, data: bytes) -> dict:
        if space != "host":
            raise XdbError("only host memory is currently supported")
        controller = self._require_coyote()
        result = controller.write_host_memory(addr, data)
        self._coyote_pump_step()
        return result

    def coyote_mem_read(self, space: str, addr: int, size: int) -> dict:
        if space != "host":
            raise XdbError("only host memory is currently supported")
        return self._require_coyote().read_host_memory(addr, size)

    def coyote_invoke(
        self,
        opcode_name: str,
        *,
        addr: int | None = None,
        length: int | None = None,
        stream_name: str = "host",
        dest: int = 0,
        last: bool = True,
        src_addr: int | None = None,
        src_length: int | None = None,
        src_stream_name: str = "host",
        src_dest: int = 0,
        dst_addr: int | None = None,
        dst_length: int | None = None,
        dst_stream_name: str = "host",
        dst_dest: int = 0,
    ) -> dict:
        controller = self._require_coyote()
        opcode = ensure_supported_local_opcode(opcode_name)
        if opcode_name == "local-transfer":
            if src_stream_name != "host" or dst_stream_name != "host":
                raise XdbError(
                    "current xdb Coyote support only implements host-stream local transfers"
                )
            if src_addr is None or dst_addr is None:
                raise XdbError("local-transfer requires --src-addr and --dst-addr")
            effective_src_len = src_length if src_length is not None else length
            effective_dst_len = dst_length if dst_length is not None else length
            if effective_src_len is None or effective_dst_len is None:
                raise XdbError("local-transfer requires --len or both --src-len and --dst-len")
            if effective_src_len <= 0 or effective_dst_len <= 0:
                raise XdbError("transfer lengths must be > 0")
            if effective_src_len > MAX_TRANSFER_SIZE or effective_dst_len > MAX_TRANSFER_SIZE:
                raise XdbError("Coyote transfers over 128MB are not supported")
            controller.ensure_host_memory(dst_addr, effective_dst_len)
            payload = controller.encode_invoke_transfer(
                src_addr=src_addr,
                src_len=effective_src_len,
                src_stream=parse_stream_name(src_stream_name),
                src_dest=src_dest,
                dst_addr=dst_addr,
                dst_len=effective_dst_len,
                dst_stream=parse_stream_name(dst_stream_name),
                dst_dest=dst_dest,
                last=last,
            )
            controller.write_input(payload, pump=self._coyote_pump_step)
            self._coyote_pump_step()
            return {
                "opcode": opcode_name,
                "src_addr": src_addr,
                "src_addr_hex": f"0x{src_addr:x}",
                "src_len": effective_src_len,
                "dst_addr": dst_addr,
                "dst_addr_hex": f"0x{dst_addr:x}",
                "dst_len": effective_dst_len,
                "issued": True,
            }

        if addr is None or length is None:
            raise XdbError(f"{opcode_name} requires --addr and --len")
        if length <= 0:
            raise XdbError("length must be > 0")
        if length > MAX_TRANSFER_SIZE:
            raise XdbError("Coyote transfers over 128MB are not supported")
        if stream_name != "host":
            raise XdbError(
                "current xdb Coyote support only implements host-stream local operations"
            )
        stream = parse_stream_name(stream_name)
        if opcode_name in {"local-read", "local-offload"}:
            source_write = controller.encode_sync_source_write(addr, length)
            payload = source_write + controller.encode_invoke_single(
                opcode=opcode,
                addr=addr,
                length=length,
                stream=stream,
                dest=dest,
                last=last,
            )
        else:
            if opcode_name in {"local-write", "local-sync"}:
                controller.ensure_host_memory(addr, length)
            payload = controller.encode_invoke_single(
                opcode=opcode,
                addr=addr,
                length=length,
                stream=stream,
                dest=dest,
                last=last,
            )
        controller.write_input(payload, pump=self._coyote_pump_step)
        self._coyote_pump_step()
        return {
            "opcode": opcode_name,
            "addr": addr,
            "addr_hex": f"0x{addr:x}",
            "length": length,
            "stream": stream_name,
            "dest": dest,
            "last": bool(last),
            "issued": True,
        }

    def coyote_completed(
        self,
        opcode_name: str,
        *,
        target_count: int | None = None,
        timeout_seconds: float | None = None,
    ) -> dict:
        controller = self._require_coyote()
        opcode = ensure_supported_local_opcode(opcode_name)

        def check_once(wait_timeout: float | None) -> int:
            controller.write_input(
                controller.encode_check_completed(opcode),
                pump=self._coyote_pump_step,
            )
            return self._coyote_wait_for_item(
                controller.get_completed_result_nowait,
                timeout_seconds=wait_timeout,
                description=f"completion count for {opcode_name}",
            )

        if target_count is None:
            count = check_once(timeout_seconds)
            return {"opcode": opcode_name, "count": count}
        if target_count <= 0:
            raise XdbError("target completion count must be > 0")
        deadline = None if timeout_seconds is None else time.monotonic() + timeout_seconds
        count = 0
        while count < target_count:
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            count = check_once(remaining)
            if count >= target_count:
                break
            if deadline is not None and time.monotonic() >= deadline:
                raise XdbError(
                    f"timed out waiting for completion count {target_count} for {opcode_name}"
                )
            self._coyote_pump_step()
        return {
            "opcode": opcode_name,
            "count": count,
            "target_count": target_count,
            "satisfied": count >= target_count,
        }

    def coyote_clear_completed(self) -> dict:
        controller = self._require_coyote()
        controller.write_input(
            controller.encode_clear_completed(),
            pump=self._coyote_pump_step,
        )
        self._coyote_pump_step()
        return {"cleared": True}

    def coyote_irq_wait(self, *, timeout_seconds: float | None = None) -> dict:
        event = self._coyote_wait_for_item(
            self._require_coyote().get_irq_nowait,
            timeout_seconds=timeout_seconds,
            description="Coyote IRQ",
        )
        return {
            "pid": int(event["pid"]),
            "value": int(event["value"]),
            "value_hex": f"0x{int(event['value']):x}",
        }

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
