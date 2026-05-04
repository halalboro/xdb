from __future__ import annotations

import json
import os
import shlex
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


class VivadoError(RuntimeError):
    pass


@dataclass
class VivadoResult:
    stdout: str
    stderr: str


def _run_vivado_tcl(tcl: str, args: list[str], timeout: int = 120) -> VivadoResult:
    with tempfile.NamedTemporaryFile("w", suffix=".tcl", delete=False) as tf:
        tf.write(tcl)
        tcl_path = tf.name

    cmd = ["vivado", "-mode", "batch", "-source", tcl_path, "-notrace", "-nolog", "-nojournal"]
    if args:
        cmd += ["-tclargs", *args]

    env = os.environ.copy()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=timeout)
    except FileNotFoundError as e:
        raise VivadoError(
            "vivado executable not found in PATH. Run inside a Xilinx-enabled shell "
            "(e.g., xilinx-shell) or source Vivado settings first."
        ) from e
    try:
        Path(tcl_path).unlink(missing_ok=True)
    except Exception:
        pass

    if p.returncode != 0:
        raise VivadoError(
            f"vivado failed (rc={p.returncode})\n"
            f"cmd: {' '.join(shlex.quote(x) for x in cmd)}\n"
            f"stdout:\n{p.stdout}\n"
            f"stderr:\n{p.stderr}"
        )
    return VivadoResult(stdout=p.stdout, stderr=p.stderr)


def _extract_json(stdout: str) -> dict:
    start = "XDB_JSON_BEGIN"
    end = "XDB_JSON_END"
    i = stdout.find(start)
    j = stdout.find(end)
    if i == -1 or j == -1 or j <= i:
        raise VivadoError(f"could not find JSON markers in Vivado output\n{stdout}")
    payload = stdout[i + len(start):j].strip()
    return json.loads(payload)


def list_targets(part_hint: str | None, timeout: int = 120) -> dict:
    tcl = r'''
open_hw_manager
connect_hw_server
set targets [get_hw_targets *]
set out "{\"targets\":"
append out "\["
set first 1
foreach t $targets {
  open_hw_target $t
  set devs [get_hw_devices]
  set part ""
  if {[llength $devs] > 0} {
    set part [get_property PART [lindex $devs 0]]
  }
  if {!$first} { append out "," }
  set first 0
  append out "{\"target\":\"" [string map {"\\" "\\\\\""} $t] "\",\"part\":\"" $part "\"}"
  close_hw_target
}
append out "]}"
puts "XDB_JSON_BEGIN"
puts $out
puts "XDB_JSON_END"
exit 0
'''
    res = _run_vivado_tcl(tcl, [], timeout=timeout)
    data = _extract_json(res.stdout)
    if part_hint:
        ph = part_hint.lower()
        data["targets"] = [t for t in data.get("targets", []) if ph in (t.get("part", "").lower())]
    return data


def program(bit: str, ltx: str | None, part_hint: str, timeout: int = 300) -> dict:
    tcl = r'''
set part_hint [lindex $argv 0]
set bit [lindex $argv 1]
set ltx [lindex $argv 2]

open_hw_manager
connect_hw_server
set chosen ""
foreach t [get_hw_targets *] {
  open_hw_target $t
  set devs [get_hw_devices]
  if {[llength $devs] > 0} {
    set p [string tolower [get_property PART [lindex $devs 0]]]
    if {[string first [string tolower $part_hint] $p] >= 0} {
      set chosen $t
      break
    }
  }
  close_hw_target
}
if {$chosen eq ""} { error "no target matching part hint $part_hint" }

current_hw_target $chosen
open_hw_target $chosen
set dev [lindex [get_hw_devices] 0]
current_hw_device $dev
set_property PROGRAM.FILE $bit $dev
if {$ltx ne ""} { set_property PROBES.FILE $ltx $dev }
program_hw_devices $dev
refresh_hw_device $dev
puts "XDB_JSON_BEGIN"
puts "{\"ok\":true,\"target\":\"$chosen\",\"part\":\"[get_property PART $dev]\"}"
puts "XDB_JSON_END"
exit 0
'''
    res = _run_vivado_tcl(tcl, [part_hint, bit, ltx or ""], timeout=timeout)
    return _extract_json(res.stdout)


def list_ilas(part_hint: str, timeout: int = 180) -> dict:
    tcl = r'''
set part_hint [lindex $argv 0]
open_hw_manager
connect_hw_server
set chosen ""
foreach t [get_hw_targets *] {
  open_hw_target $t
  set devs [get_hw_devices]
  if {[llength $devs] > 0} {
    set p [string tolower [get_property PART [lindex $devs 0]]]
    if {[string first [string tolower $part_hint] $p] >= 0} {
      set chosen $t
      break
    }
  }
  close_hw_target
}
if {$chosen eq ""} { error "no target matching part hint $part_hint" }
current_hw_target $chosen
open_hw_target $chosen
set dev [lindex [get_hw_devices] 0]
current_hw_device $dev
refresh_hw_device $dev
set ilas [get_hw_ilas]
set out "{\"target\":\"$chosen\",\"part\":\"[get_property PART $dev]\",\"ilas\":"
append out "\["
set fi 1
foreach ila $ilas {
  if {!$fi} { append out "," }
  set fi 0
  set nm [get_property NAME $ila]
  append out "{\"name\":\"$nm\",\"probes\":"
  append out "\["
  set fp 1
  foreach p [get_hw_probes -of_objects $ila] {
    if {!$fp} { append out "," }
    set fp 0
    set pn [get_property NAME $p]
    set w [get_property PORT_WIDTH $p]
    append out "{\"name\":\"$pn\",\"width\":" $w "}"
  }
  append out "]}"
}
append out "]}"
puts "XDB_JSON_BEGIN"
puts $out
puts "XDB_JSON_END"
exit 0
'''
    res = _run_vivado_tcl(tcl, [part_hint], timeout=timeout)
    return _extract_json(res.stdout)


def capture(part_hint: str, ila_name: str, csv_path: str, samples: int, timeout: int = 120) -> dict:
    tcl = r'''
set part_hint [lindex $argv 0]
set ila_name [lindex $argv 1]
set csv_path [lindex $argv 2]
set samples [lindex $argv 3]

open_hw_manager
connect_hw_server
set chosen ""
foreach t [get_hw_targets *] {
  open_hw_target $t
  set devs [get_hw_devices]
  if {[llength $devs] > 0} {
    set p [string tolower [get_property PART [lindex $devs 0]]]
    if {[string first [string tolower $part_hint] $p] >= 0} {
      set chosen $t
      break
    }
  }
  close_hw_target
}
if {$chosen eq ""} { error "no target matching part hint $part_hint" }
current_hw_target $chosen
open_hw_target $chosen
set dev [lindex [get_hw_devices] 0]
current_hw_device $dev
refresh_hw_device $dev
set ila [get_hw_ilas $ila_name]
if {[llength $ila] == 0} { error "ILA not found: $ila_name" }
set ila [lindex $ila 0]
set_property CONTROL.DATA_DEPTH $samples $ila
run_hw_ila $ila
wait_on_hw_ila $ila
write_hw_ila_data -csv_file $csv_path [upload_hw_ila_data $ila]
set out "{\"ok\":true,\"target\":\"$chosen\",\"part\":\"[get_property PART $dev]\","
append out "\"ila\":\"$ila_name\",\"csv\":\"$csv_path\",\"samples\":$samples}"
puts "XDB_JSON_BEGIN"
puts $out
puts "XDB_JSON_END"
exit 0
'''
    res = _run_vivado_tcl(tcl, [part_hint, ila_name, csv_path, str(samples)], timeout=timeout)
    return _extract_json(res.stdout)
