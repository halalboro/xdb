from __future__ import annotations


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


HELPERS_TCL = r'''
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
