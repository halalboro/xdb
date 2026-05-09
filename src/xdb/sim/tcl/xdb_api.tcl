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

if {![info exists ::xdb_next_breakpoint_id]} {
  set ::xdb_next_breakpoint_id 1
}

if {![info exists ::xdb_forces]} {
  set ::xdb_forces [dict create]
}

proc xdb_api_time {} {
  set __xdb_time [current_time]
  return "\"time\":[xdb_json_string $__xdb_time]"
}

proc xdb_api_run {run_args} {
  set __xdb_before [current_time]
  set __xdb_breakpoint_json "\"breakpoint_hit\":false"
  if {[xdb_has_polled_breakpoints]} {
    set __xdb_breakpoint_json [xdb_run_with_polled_breakpoints $run_args]
  } elseif {[llength $run_args] == 0} {
    run
  } else {
    eval [linsert $run_args 0 run]
  }
  set __xdb_after [current_time]
  set __xdb_joined [join $run_args " "]
  return "\"time_before\":[xdb_json_string $__xdb_before],\"time_after\":[xdb_json_string $__xdb_after],\"duration\":[xdb_json_string $__xdb_joined],$__xdb_breakpoint_json"
}

proc xdb_api_restart {} {
  set __xdb_before [current_time]
  restart
  set __xdb_after [current_time]
  return "\"time_before\":[xdb_json_string $__xdb_before],\"time_after\":[xdb_json_string $__xdb_after]"
}

proc xdb_api_step_time {step_args} {
  set __xdb_before [current_time]
  set __xdb_breakpoint_json "\"breakpoint_hit\":false"
  if {[xdb_has_polled_breakpoints]} {
    set __xdb_breakpoint_json [xdb_run_with_polled_breakpoints $step_args]
  } else {
    eval [linsert $step_args 0 run]
  }
  set __xdb_after [current_time]
  set __xdb_joined [join $step_args " "]
  return "\"time_before\":[xdb_json_string $__xdb_before],\"time_after\":[xdb_json_string $__xdb_after],\"duration\":[xdb_json_string $__xdb_joined],\"step_mode\":[xdb_json_string time],$__xdb_breakpoint_json"
}

proc xdb_api_step_count {count} {
  set __xdb_before [current_time]
  set __xdb_breakpoint_json "\"breakpoint_hit\":false"
  for {set __xdb_i 0} {$__xdb_i < $count} {incr __xdb_i} {
    step
    set __xdb_hit [xdb_poll_breakpoints]
    if {[llength $__xdb_hit] > 0} {
      set __xdb_breakpoint_json [xdb_breakpoint_hit_json $__xdb_hit]
      break
    }
  }
  set __xdb_after [current_time]
  return "\"time_before\":[xdb_json_string $__xdb_before],\"time_after\":[xdb_json_string $__xdb_after],\"count\":$count,\"step_mode\":[xdb_json_string count],$__xdb_breakpoint_json"
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
  if {$__xdb_top_scope eq "" && [llength $__xdb_root_scopes] > 0} {
    set __xdb_top_scope [xdb_parent_scope [lindex $__xdb_root_scopes 0]]
  }
  if {$__xdb_top_scope eq ""} {
    set __xdb_top_scope $top_name
  }
  set __xdb_child_scopes {}
  catch {set __xdb_child_scopes [get_scopes [format "%s/*" $__xdb_top_scope]]}
  if {[llength $__xdb_child_scopes] == 0 && [llength $__xdb_root_scopes] > 0} {
    set __xdb_child_scopes $__xdb_root_scopes
  }
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
    log_vcd *
  } else {
    set __xdb_pattern [format "%s/*" $scope]
    log_vcd $__xdb_pattern
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
  return "\"passed\":true,\"kind\":[xdb_json_string assert-signal],\"signal\":[xdb_json_string $signal],\"expected\":[xdb_json_string $expected],\"value\":[xdb_json_string $__xdb_actual],\"time\":[xdb_json_string $__xdb_time]"
}

proc xdb_api_assert_tcl {expr_text} {
  set __xdb_expr [xdb_normalize_expr $expr_text]
  set __xdb_time [current_time]
  if {![uplevel #0 [list expr $__xdb_expr]]} {
    error [format "assert-tcl failed at %s: %s" $__xdb_time $__xdb_expr]
  }
  return "\"passed\":true,\"kind\":[xdb_json_string assert-tcl],\"expr\":[xdb_json_string $__xdb_expr],\"time\":[xdb_json_string $__xdb_time]"
}

proc xdb_time_unit_to_fs {unit} {
  set __xdb_unit [string tolower $unit]
  switch -- $__xdb_unit {
    fs { return 1.0 }
    ps { return 1000.0 }
    ns { return 1000000.0 }
    us { return 1000000000.0 }
    ms { return 1000000000000.0 }
    s - sec - secs - second - seconds { return 1000000000000000.0 }
    default { error [format "unsupported simulation time unit: %s" $unit] }
  }
}

proc xdb_parse_time_to_fs {time_text} {
  set __xdb_text [string trim $time_text]
  if {![regexp {^([0-9]+(?:\.[0-9]*)?|\.[0-9]+)\s*([A-Za-z]+)$} $__xdb_text -> __xdb_value __xdb_unit]} {
    error [format "invalid simulation time: %s" $time_text]
  }
  return [expr {double($__xdb_value) * [xdb_time_unit_to_fs $__xdb_unit]}]
}

proc xdb_duration_args_to_fs {duration_args} {
  set __xdb_text [string map {" " "" "\t" "" "\n" ""} [join $duration_args ""]]
  if {![regexp {^([0-9]+(?:\.[0-9]*)?|\.[0-9]+)([A-Za-z]+)$} $__xdb_text -> __xdb_value __xdb_unit]} {
    error [format "invalid simulation duration: %s" [join $duration_args " "]]
  }
  return [expr {double($__xdb_value) * [xdb_time_unit_to_fs $__xdb_unit]}]
}

proc xdb_has_polled_breakpoints {} {
  foreach __xdb_bp $::xdb_breakpoints {
    if {[dict get $__xdb_bp mode] eq "poll"} {
      return 1
    }
  }
  return 0
}

proc xdb_poll_breakpoints {} {
  foreach __xdb_bp $::xdb_breakpoints {
    if {[dict get $__xdb_bp mode] ne "poll"} {
      continue
    }
    set __xdb_condition [dict get $__xdb_bp condition]
    if {[uplevel #0 [list expr $__xdb_condition]]} {
      return $__xdb_bp
    }
  }
  return {}
}

proc xdb_breakpoint_hit_json {breakpoint} {
  return "\"breakpoint_hit\":true,\"breakpoint_id\":[dict get $breakpoint id],\"breakpoint_mode\":[xdb_json_string [dict get $breakpoint mode]],\"breakpoint_condition\":[xdb_json_string [dict get $breakpoint condition]]"
}

proc xdb_run_with_polled_breakpoints {run_args} {
  if {[llength $run_args] == 0} {
    error "polled breakpoints require a bounded run duration"
  }
  set __xdb_start_fs [xdb_parse_time_to_fs [current_time]]
  set __xdb_deadline_fs [expr {$__xdb_start_fs + [xdb_duration_args_to_fs $run_args]}]
  set __xdb_iterations 0
  set __xdb_previous_fs $__xdb_start_fs
  set __xdb_hit [xdb_poll_breakpoints]
  if {[llength $__xdb_hit] > 0} {
    return [xdb_breakpoint_hit_json $__xdb_hit]
  }
  while {[xdb_parse_time_to_fs [current_time]] < $__xdb_deadline_fs} {
    step
    incr __xdb_iterations
    set __xdb_current_fs [xdb_parse_time_to_fs [current_time]]
    set __xdb_hit [xdb_poll_breakpoints]
    if {[llength $__xdb_hit] > 0} {
      return [xdb_breakpoint_hit_json $__xdb_hit]
    }
    if {$__xdb_current_fs <= $__xdb_previous_fs} {
      error "simulation did not advance while polling breakpoints"
    }
    set __xdb_previous_fs $__xdb_current_fs
    if {$__xdb_iterations > 1000000} {
      error "polled breakpoint run exceeded iteration limit"
    }
  }
  return "\"breakpoint_hit\":false"
}

proc xdb_api_expect_signal {signal expected within_args} {
  set __xdb_time_before [current_time]
  set __xdb_within [join $within_args " "]
  set __xdb_initial [get_value $signal]
  set __xdb_start_fs [xdb_parse_time_to_fs $__xdb_time_before]
  set __xdb_deadline_fs [expr {$__xdb_start_fs + [xdb_duration_args_to_fs $within_args]}]
  set __xdb_iterations 0
  set __xdb_actual $__xdb_initial
  while {$__xdb_actual ne $expected} {
    set __xdb_current_fs [xdb_parse_time_to_fs [current_time]]
    if {$__xdb_current_fs > $__xdb_deadline_fs} {
      break
    }
    step
    incr __xdb_iterations
    set __xdb_actual [get_value $signal]
    set __xdb_after_step_fs [xdb_parse_time_to_fs [current_time]]
    if {$__xdb_actual eq $expected && $__xdb_after_step_fs <= $__xdb_deadline_fs} {
      break
    }
    if {$__xdb_after_step_fs > $__xdb_deadline_fs} {
      break
    }
    if {$__xdb_iterations > 1000000} {
      error [format "expect-signal exceeded iteration limit while waiting for %s to reach %s" $signal $expected]
    }
  }
  set __xdb_time_after [current_time]
  set __xdb_actual [get_value $signal]
  set __xdb_time_after_fs [xdb_parse_time_to_fs $__xdb_time_after]
  if {$__xdb_actual ne $expected || $__xdb_time_after_fs > $__xdb_deadline_fs} {
    error [format "expect-signal failed: %s did not reach %s within %s (start=%s end=%s time_before=%s time_after=%s)" $signal $expected $__xdb_within $__xdb_initial $__xdb_actual $__xdb_time_before $__xdb_time_after]
  }
  return "\"passed\":true,\"kind\":[xdb_json_string expect-signal],\"signal\":[xdb_json_string $signal],\"expected\":[xdb_json_string $expected],\"initial\":[xdb_json_string $__xdb_initial],\"value\":[xdb_json_string $__xdb_actual],\"within\":[xdb_json_string $__xdb_within],\"time_before\":[xdb_json_string $__xdb_time_before],\"time_after\":[xdb_json_string $__xdb_time_after],\"iterations\":$__xdb_iterations"
}

proc xdb_api_expect_change {signal within_args} {
  set __xdb_time_before [current_time]
  set __xdb_within [join $within_args " "]
  set __xdb_initial [get_value $signal]
  set __xdb_start_fs [xdb_parse_time_to_fs $__xdb_time_before]
  set __xdb_deadline_fs [expr {$__xdb_start_fs + [xdb_duration_args_to_fs $within_args]}]
  set __xdb_iterations 0
  set __xdb_actual $__xdb_initial
  while {$__xdb_actual eq $__xdb_initial} {
    set __xdb_current_fs [xdb_parse_time_to_fs [current_time]]
    if {$__xdb_current_fs > $__xdb_deadline_fs} {
      break
    }
    step
    incr __xdb_iterations
    set __xdb_actual [get_value $signal]
    set __xdb_after_step_fs [xdb_parse_time_to_fs [current_time]]
    if {$__xdb_actual ne $__xdb_initial && $__xdb_after_step_fs <= $__xdb_deadline_fs} {
      break
    }
    if {$__xdb_after_step_fs > $__xdb_deadline_fs} {
      break
    }
    if {$__xdb_iterations > 1000000} {
      error [format "expect-change exceeded iteration limit while waiting for %s to change" $signal]
    }
  }
  set __xdb_time_after [current_time]
  set __xdb_actual [get_value $signal]
  set __xdb_time_after_fs [xdb_parse_time_to_fs $__xdb_time_after]
  if {$__xdb_actual eq $__xdb_initial || $__xdb_time_after_fs > $__xdb_deadline_fs} {
    error [format "expect-change failed: %s did not change within %s (value=%s time_before=%s time_after=%s)" $signal $__xdb_within $__xdb_initial $__xdb_time_before $__xdb_time_after]
  }
  return "\"passed\":true,\"kind\":[xdb_json_string expect-change],\"signal\":[xdb_json_string $signal],\"initial\":[xdb_json_string $__xdb_initial],\"value\":[xdb_json_string $__xdb_actual],\"within\":[xdb_json_string $__xdb_within],\"time_before\":[xdb_json_string $__xdb_time_before],\"time_after\":[xdb_json_string $__xdb_time_after],\"changed\":true,\"iterations\":$__xdb_iterations"
}

proc xdb_api_breakpoint_add {condition} {
  set __xdb_condition [xdb_normalize_expr $condition]
  set __xdb_id $::xdb_next_breakpoint_id
  incr ::xdb_next_breakpoint_id
  set __xdb_mode poll
  set __xdb_backend_id ""
  if {[llength [info commands when]] > 0} {
    if {![catch {when $__xdb_condition {stop}} __xdb_when_id]} {
      set __xdb_mode when
      set __xdb_backend_id $__xdb_when_id
    }
  }
  lappend ::xdb_breakpoints [dict create id $__xdb_id mode $__xdb_mode condition $__xdb_condition backend_id $__xdb_backend_id]
  return "\"condition\":[xdb_json_string $__xdb_condition],\"breakpoint_id\":$__xdb_id,\"mode\":[xdb_json_string $__xdb_mode],\"backend_id\":[xdb_json_string $__xdb_backend_id]"
}

proc xdb_api_breakpoint_clear {} {
  set __xdb_cleared 0
  foreach __xdb_bp $::xdb_breakpoints {
    if {[dict get $__xdb_bp mode] eq "when" && [dict get $__xdb_bp backend_id] ne ""} {
      catch {nowhen [dict get $__xdb_bp backend_id]}
    }
    incr __xdb_cleared
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
