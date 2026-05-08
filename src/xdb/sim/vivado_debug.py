from __future__ import annotations

from pathlib import Path

from ..errors import XdbError
from .tcl_helpers import _tcl_list, _tcl_string


class VivadoDebugMixin:
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
  xdb_reply_ok_fields $__xdb_request_id "\"passed\":true,\"kind\":[xdb_json_string \"expect-signal\"],\"signal\":[xdb_json_string $__xdb_signal],\"expected\":[xdb_json_string $__xdb_expected],\"initial\":[xdb_json_string $__xdb_initial],\"value\":[xdb_json_string $__xdb_initial],\"within\":[xdb_json_string [join $__xdb_within_args \" \" ]],\"time_before\":[xdb_json_string $__xdb_time_before],\"time_after\":[xdb_json_string $__xdb_time_before],\"iterations\":0"
}} else {{
  set ::xdb_expect_signal_hit 0
  set __xdb_condition [xdb_signal_eq_expr $__xdb_signal $__xdb_expected]
  set __xdb_id [when $__xdb_condition {{set ::xdb_expect_signal_hit 1; stop}}]
  eval [linsert $__xdb_within_args 0 run]
  catch {{nowhen $__xdb_id}}
  set __xdb_time_after [current_time]
  set __xdb_actual [get_value $__xdb_signal]
  if {{!$::xdb_expect_signal_hit && $__xdb_actual ne $__xdb_expected}} {{
    error [format "expect-signal failed: %s did not reach %s within %s (start=%s end=%s time_before=%s time_after=%s)" $__xdb_signal $__xdb_expected [join $__xdb_within_args \" \" ] $__xdb_initial $__xdb_actual $__xdb_time_before $__xdb_time_after]
  }}
  xdb_reply_ok_fields $__xdb_request_id "\"passed\":true,\"kind\":[xdb_json_string \"expect-signal\"],\"signal\":[xdb_json_string $__xdb_signal],\"expected\":[xdb_json_string $__xdb_expected],\"initial\":[xdb_json_string $__xdb_initial],\"value\":[xdb_json_string $__xdb_actual],\"within\":[xdb_json_string [join $__xdb_within_args \" \" ]],\"time_before\":[xdb_json_string $__xdb_time_before],\"time_after\":[xdb_json_string $__xdb_time_after],\"iterations\":1"
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
  error [format "expect-change failed: %s did not change within %s (value=%s time_before=%s time_after=%s)" $__xdb_signal [join $__xdb_within_args \" \" ] $__xdb_initial $__xdb_time_before $__xdb_time_after]
}}
xdb_reply_ok_fields $__xdb_request_id "\"passed\":true,\"kind\":[xdb_json_string \"expect-change\"],\"signal\":[xdb_json_string $__xdb_signal],\"initial\":[xdb_json_string $__xdb_initial],\"value\":[xdb_json_string $__xdb_actual],\"within\":[xdb_json_string [join $__xdb_within_args \" \" ]],\"time_before\":[xdb_json_string $__xdb_time_before],\"time_after\":[xdb_json_string $__xdb_time_after],\"changed\":true"
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
