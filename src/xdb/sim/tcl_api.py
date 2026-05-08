from __future__ import annotations

from typing import Any

from .tcl_helpers import _tcl_list, _tcl_string


API_TCL = r'''
proc xdb_api_time {} {
  set __xdb_time [current_time]
  return "\"time\":[xdb_json_string $__xdb_time]"
}

proc xdb_api_run {args} {
  set __xdb_before [current_time]
  if {[llength $args] == 0} {
    run
  } else {
    eval [linsert $args 0 run]
  }
  set __xdb_after [current_time]
  set __xdb_joined [join $args " "]
  return "\"time_before\":[xdb_json_string $__xdb_before],\"time_after\":[xdb_json_string $__xdb_after],\"duration\":[xdb_json_string $__xdb_joined]"
}

proc xdb_api_restart {} {
  set __xdb_before [current_time]
  restart
  set __xdb_after [current_time]
  return "\"time_before\":[xdb_json_string $__xdb_before],\"time_after\":[xdb_json_string $__xdb_after]"
}

proc xdb_api_step_time {args} {
  set __xdb_before [current_time]
  eval [linsert $args 0 run]
  set __xdb_after [current_time]
  set __xdb_joined [join $args " "]
  return "\"time_before\":[xdb_json_string $__xdb_before],\"time_after\":[xdb_json_string $__xdb_after],\"duration\":[xdb_json_string $__xdb_joined],\"step_mode\":[xdb_json_string \"time\"]"
}

proc xdb_api_step_count {count} {
  set __xdb_before [current_time]
  for {set __xdb_i 0} {$__xdb_i < $count} {incr __xdb_i} {
    step
  }
  set __xdb_after [current_time]
  return "\"time_before\":[xdb_json_string $__xdb_before],\"time_after\":[xdb_json_string $__xdb_after],\"count\":$count,\"step_mode\":[xdb_json_string \"count\"]"
}

proc xdb_api_wait_until {expr_text step_args timeout_seconds max_iterations} {
  set __xdb_expr [xdb_normalize_expr $expr_text]
  set __xdb_time_before [current_time]
  set __xdb_iterations 0
  set __xdb_deadline_ms -1
  if {$timeout_seconds ne ""} {
    set __xdb_deadline_ms [expr {[clock milliseconds] + int(1000.0 * $timeout_seconds)}]
  }
  while {1} {
    if {[uplevel #0 [list expr $__xdb_expr]]} {
      break
    }
    if {$max_iterations ne "" && $__xdb_iterations >= $max_iterations} {
      error [format "condition not met before reaching max iterations (%s)" $max_iterations]
    }
    if {$__xdb_deadline_ms >= 0 && [clock milliseconds] >= $__xdb_deadline_ms} {
      error [format "timed out after %s second(s) while waiting for condition" $timeout_seconds]
    }
    set __xdb_prev_time [current_time]
    eval [linsert $step_args 0 run]
    incr __xdb_iterations
    set __xdb_current_time [current_time]
    if {$__xdb_current_time eq $__xdb_prev_time} {
      if {[uplevel #0 [list expr $__xdb_expr]]} {
        break
      }
      error "condition not met and simulation did not advance while waiting"
    }
  }
  set __xdb_time_after [current_time]
  set __xdb_step [join $step_args " "]
  return "\"expr\":[xdb_json_string $__xdb_expr],\"step\":[xdb_json_string $__xdb_step],\"iterations\":$__xdb_iterations,\"time_before\":[xdb_json_string $__xdb_time_before],\"time_after\":[xdb_json_string $__xdb_time_after],\"timeout_seconds\":[xdb_json_string $timeout_seconds],\"max_iterations\":[xdb_json_string $max_iterations]"
}

proc xdb_api_wait_until_signal {signal expected step_args timeout_seconds max_iterations} {
  set __xdb_time_before [current_time]
  set __xdb_iterations 0
  set __xdb_deadline_ms -1
  if {$timeout_seconds ne ""} {
    set __xdb_deadline_ms [expr {[clock milliseconds] + int(1000.0 * $timeout_seconds)}]
  }
  while {1} {
    set __xdb_value [get_value $signal]
    if {$__xdb_value eq $expected} {
      break
    }
    if {$max_iterations ne "" && $__xdb_iterations >= $max_iterations} {
      error [format "signal did not reach expected value before reaching max iterations (%s)" $max_iterations]
    }
    if {$__xdb_deadline_ms >= 0 && [clock milliseconds] >= $__xdb_deadline_ms} {
      error [format "timed out after %s second(s) while waiting for signal" $timeout_seconds]
    }
    set __xdb_prev_time [current_time]
    eval [linsert $step_args 0 run]
    incr __xdb_iterations
    set __xdb_current_time [current_time]
    if {$__xdb_current_time eq $__xdb_prev_time} {
      set __xdb_value [get_value $signal]
      if {$__xdb_value eq $expected} {
        break
      }
      error "signal did not reach expected value and simulation did not advance while waiting"
    }
  }
  set __xdb_time_after [current_time]
  set __xdb_value [get_value $signal]
  set __xdb_step [join $step_args " "]
  return "\"signal\":[xdb_json_string $signal],\"value\":[xdb_json_string $__xdb_value],\"expected\":[xdb_json_string $expected],\"step\":[xdb_json_string $__xdb_step],\"iterations\":$__xdb_iterations,\"time_before\":[xdb_json_string $__xdb_time_before],\"time_after\":[xdb_json_string $__xdb_time_after],\"timeout_seconds\":[xdb_json_string $timeout_seconds],\"max_iterations\":[xdb_json_string $max_iterations]"
}

proc xdb_api_describe {top_name} {
  set __xdb_time [current_time]
  set __xdb_root_scopes [get_scopes *]
  set __xdb_top_scope ""
  foreach __xdb_scope $__xdb_root_scopes {
    if {[xdb_basename $__xdb_scope] eq $top_name} {
      set __xdb_top_scope $__xdb_scope
      break
    }
  }
  if {$__xdb_top_scope eq "" && [llength $__xdb_root_scopes] == 1} {
    set __xdb_top_scope [lindex $__xdb_root_scopes 0]
  }
  if {$__xdb_top_scope eq ""} {
    set __xdb_top_scope $top_name
  }
  set __xdb_child_scopes {}
  catch {set __xdb_child_scopes [get_scopes [format "%s/*" $__xdb_top_scope]]}
  set __xdb_objects [xdb_collect_snapshot_value_objects $__xdb_top_scope]
  return "\"top\":[xdb_json_string $top_name],\"top_scope\":[xdb_json_string $__xdb_top_scope],\"time\":[xdb_json_string $__xdb_time],\"root_scopes\":[xdb_json_array_strings $__xdb_root_scopes],\"child_scopes\":[xdb_json_array_strings $__xdb_child_scopes],\"child_scope_metadata\":[xdb_json_object_metadata_array $__xdb_child_scopes \"module\" 0],\"objects\":[xdb_json_object_metadata_array $__xdb_objects \"signal\" 1]"
}

proc xdb_api_get_signal {signal} {
  set __xdb_value [get_value $signal]
  set __xdb_object_json [xdb_json_object_metadata $signal "signal" 1]
  return "\"signal\":[xdb_json_string $signal],\"value\":[xdb_json_string $__xdb_value],\"object\":$__xdb_object_json"
}

proc xdb_api_get_many {pattern} {
  set __xdb_items [get_objects $pattern]
  return "\"pattern\":[xdb_json_string $pattern],\"signals\":[xdb_json_signal_values $__xdb_items],\"objects\":[xdb_json_object_metadata_array $__xdb_items \"signal\" 1]"
}

proc xdb_api_read_signals {signals} {
  return "\"signals\":[xdb_json_object_metadata_array $signals \"signal\" 1]"
}

proc xdb_api_scopes {scope pattern} {
  set __xdb_scopes [get_scopes $pattern]
  return "\"scope\":[xdb_json_string $scope],\"scopes\":[xdb_json_array_strings $__xdb_scopes],\"metadata\":[xdb_json_object_metadata_array $__xdb_scopes \"module\" 0]"
}

proc xdb_api_objects {scope} {
  set __xdb_pattern [format "%s/*" $scope]
  set __xdb_objects [get_objects $__xdb_pattern]
  return "\"scope\":[xdb_json_string $scope],\"objects\":[xdb_json_array_strings $__xdb_objects],\"metadata\":[xdb_json_object_metadata_array $__xdb_objects \"signal\" 1]"
}

proc xdb_api_snapshot_scope {scope} {
  set __xdb_objects [xdb_collect_snapshot_value_objects $scope]
  set __xdb_time [current_time]
  return "\"scope\":[xdb_json_string $scope],\"time\":[xdb_json_string $__xdb_time],\"objects\":[xdb_json_object_metadata_array $__xdb_objects \"signal\" 1]"
}

proc xdb_api_add_wave {pattern} {
  set __xdb_objects [get_objects $pattern]
  foreach __xdb_object $__xdb_objects {
    catch {log_wave $__xdb_object}
    catch {add_wave $__xdb_object}
  }
  set __xdb_count [llength $__xdb_objects]
  return "\"pattern\":[xdb_json_string $pattern],\"count\":$__xdb_count,\"objects\":[xdb_json_array_strings $__xdb_objects]"
}

proc xdb_api_vcd_start {path scope} {
  open_vcd $path
  if {$scope eq ""} {
    log_vcd -r /*
  } else {
    set __xdb_pattern [format "%s/*" $scope]
    log_vcd -r $__xdb_pattern
  }
  set __xdb_time [current_time]
  return "\"file\":[xdb_json_string $path],\"scope\":[xdb_json_nullable_string $scope],\"time\":[xdb_json_string $__xdb_time],\"active\":true"
}

proc xdb_api_vcd_stop {} {
  close_vcd
  set __xdb_time [current_time]
  return "\"time\":[xdb_json_string $__xdb_time],\"active\":false"
}

proc xdb_api_assert_signal {signal expected} {
  set __xdb_actual [get_value $signal]
  set __xdb_time [current_time]
  if {$__xdb_actual ne $expected} {
    error [format "assert-signal failed at %s: %s expected %s got %s" $__xdb_time $signal $expected $__xdb_actual]
  }
  return "\"passed\":true,\"kind\":[xdb_json_string \"assert-signal\"],\"signal\":[xdb_json_string $signal],\"expected\":[xdb_json_string $expected],\"value\":[xdb_json_string $__xdb_actual],\"time\":[xdb_json_string $__xdb_time]"
}

proc xdb_api_assert_tcl {expr_text} {
  set __xdb_expr [xdb_normalize_expr $expr_text]
  set __xdb_time [current_time]
  if {![uplevel #0 [list expr $__xdb_expr]]} {
    error [format "assert-tcl failed at %s: %s" $__xdb_time $__xdb_expr]
  }
  return "\"passed\":true,\"kind\":[xdb_json_string \"assert-tcl\"],\"expr\":[xdb_json_string $__xdb_expr],\"time\":[xdb_json_string $__xdb_time]"
}

proc xdb_api_expect_signal {signal expected within_args} {
  set __xdb_time_before [current_time]
  set __xdb_initial [get_value $signal]
  if {$__xdb_initial eq $expected} {
    return "\"passed\":true,\"kind\":[xdb_json_string \"expect-signal\"],\"signal\":[xdb_json_string $signal],\"expected\":[xdb_json_string $expected],\"initial\":[xdb_json_string $__xdb_initial],\"value\":[xdb_json_string $__xdb_initial],\"within\":[xdb_json_string [join $within_args \" \" ]],\"time_before\":[xdb_json_string $__xdb_time_before],\"time_after\":[xdb_json_string $__xdb_time_before],\"iterations\":0"
  }
  set ::xdb_expect_signal_hit 0
  set __xdb_condition [xdb_signal_eq_expr $signal $expected]
  set __xdb_id [when $__xdb_condition {set ::xdb_expect_signal_hit 1; stop}]
  eval [linsert $within_args 0 run]
  catch {nowhen $__xdb_id}
  set __xdb_time_after [current_time]
  set __xdb_actual [get_value $signal]
  if {!$::xdb_expect_signal_hit && $__xdb_actual ne $expected} {
    error [format "expect-signal failed: %s did not reach %s within %s (start=%s end=%s time_before=%s time_after=%s)" $signal $expected [join $within_args \" \" ] $__xdb_initial $__xdb_actual $__xdb_time_before $__xdb_time_after]
  }
  return "\"passed\":true,\"kind\":[xdb_json_string \"expect-signal\"],\"signal\":[xdb_json_string $signal],\"expected\":[xdb_json_string $expected],\"initial\":[xdb_json_string $__xdb_initial],\"value\":[xdb_json_string $__xdb_actual],\"within\":[xdb_json_string [join $within_args \" \" ]],\"time_before\":[xdb_json_string $__xdb_time_before],\"time_after\":[xdb_json_string $__xdb_time_after],\"iterations\":1"
}

proc xdb_api_expect_change {signal within_args} {
  set __xdb_time_before [current_time]
  set __xdb_initial [get_value $signal]
  set ::xdb_expect_change_hit 0
  set __xdb_condition [xdb_signal_change_expr $signal $__xdb_initial]
  set __xdb_id [when $__xdb_condition {set ::xdb_expect_change_hit 1; stop}]
  eval [linsert $within_args 0 run]
  catch {nowhen $__xdb_id}
  set __xdb_time_after [current_time]
  set __xdb_actual [get_value $signal]
  if {!$::xdb_expect_change_hit && $__xdb_actual eq $__xdb_initial} {
    error [format "expect-change failed: %s did not change within %s (value=%s time_before=%s time_after=%s)" $signal [join $within_args \" \" ] $__xdb_initial $__xdb_time_before $__xdb_time_after]
  }
  return "\"passed\":true,\"kind\":[xdb_json_string \"expect-change\"],\"signal\":[xdb_json_string $signal],\"initial\":[xdb_json_string $__xdb_initial],\"value\":[xdb_json_string $__xdb_actual],\"within\":[xdb_json_string [join $within_args \" \" ]],\"time_before\":[xdb_json_string $__xdb_time_before],\"time_after\":[xdb_json_string $__xdb_time_after],\"changed\":true"
}

proc xdb_api_breakpoint_add {condition} {
  set __xdb_id [when $condition {stop}]
  lappend ::xdb_breakpoints $__xdb_id
  return "\"condition\":[xdb_json_string $condition],\"breakpoint_id\":[xdb_json_string $__xdb_id]"
}

proc xdb_api_breakpoint_clear {} {
  set __xdb_cleared 0
  foreach __xdb_id $::xdb_breakpoints {
    if {![catch {nowhen $__xdb_id}]} {
      incr __xdb_cleared
    }
  }
  set ::xdb_breakpoints {}
  return "\"cleared\":$__xdb_cleared"
}

proc xdb_api_eval_tcl {script} {
  set __xdb_result [uplevel #0 $script]
  set __xdb_time [current_time]
  return "\"result\":[xdb_json_string $__xdb_result],\"time\":[xdb_json_string $__xdb_time]"
}

proc xdb_api_source_tcl {path} {
  set __xdb_result [source $path]
  set __xdb_time [current_time]
  return "\"path\":[xdb_json_string $path],\"result\":[xdb_json_string $__xdb_result],\"time\":[xdb_json_string $__xdb_time]"
}

proc xdb_api_force {signal values radix repeat_every cancel_after} {
  set __xdb_cmd [list add_force]
  if {$radix ne ""} {
    lappend __xdb_cmd -radix $radix
  }
  if {$repeat_every ne ""} {
    lappend __xdb_cmd -repeat_every $repeat_every
  }
  if {$cancel_after ne ""} {
    lappend __xdb_cmd -cancel_after $cancel_after
  }
  lappend __xdb_cmd $signal
  set __xdb_force_id [uplevel #0 [concat $__xdb_cmd $values]]
  dict lappend ::xdb_forces $signal $__xdb_force_id
  set __xdb_time [current_time]
  return "\"signal\":[xdb_json_string $signal],\"force_id\":[xdb_json_string $__xdb_force_id],\"time\":[xdb_json_string $__xdb_time]"
}

proc xdb_api_release_all {} {
  set __xdb_released 0
  if {[info exists ::xdb_forces]} {
    dict for {__xdb_signal __xdb_ids} $::xdb_forces {
      incr __xdb_released [llength $__xdb_ids]
    }
  }
  remove_forces -all
  set ::xdb_forces [dict create]
  set __xdb_time [current_time]
  return "\"all\":true,\"released\":$__xdb_released,\"time\":[xdb_json_string $__xdb_time]"
}

proc xdb_api_release_signal {signal} {
  set __xdb_released 0
  if {[dict exists $::xdb_forces $signal]} {
    set __xdb_ids [dict get $::xdb_forces $signal]
    foreach __xdb_id $__xdb_ids {
      if {![catch {remove_forces $__xdb_id}]} {
        incr __xdb_released
      }
    }
    dict unset ::xdb_forces $signal
  }
  set __xdb_time [current_time]
  return "\"signal\":[xdb_json_string $signal],\"released\":$__xdb_released,\"time\":[xdb_json_string $__xdb_time]"
}
'''


class TclRaw:
    def __init__(self, code: str):
        self.code = code


def _tcl_arg(value: Any) -> str:
    if isinstance(value, TclRaw):
        return value.code
    if isinstance(value, list):
        return _tcl_list([str(v) for v in value])
    if value is None:
        return _tcl_string("")
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    return _tcl_string(str(value))


def build_proc_request(proc_name: str, *args: Any) -> str:
    rendered_args = " ".join(_tcl_arg(arg) for arg in args)
    call = proc_name if not rendered_args else f"{proc_name} {rendered_args}"
    return f"set __xdb_fields [{call}]\nxdb_reply_ok_fields $__xdb_request_id $__xdb_fields"
