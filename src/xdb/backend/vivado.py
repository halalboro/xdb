from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from xdb.errors import XdbError
from xdb.backend.base import (
    Capability,
    CaptureResult,
    IlaArmResult,
    IlaStatusResult,
    IlaTriggerCompileResult,
    IlaTriggerConfig,
    InstrumentsResult,
    ListIlasResult,
    ProbeTrigger,
    ProgramResult,
    TargetsResult,
)


class VivadoError(XdbError):
    pass


@dataclass
class VivadoResult:
    stdout: str
    stderr: str


class VivadoBackend:
    name = "vivado"

    def list_targets(self, part_hint: str | None, timeout: int = 120) -> TargetsResult:
        return list_targets(part_hint, timeout=timeout)

    def program(
        self, bit: str, ltx: str | None, part_hint: str, timeout: int = 300
    ) -> ProgramResult:
        return program(bit, ltx, part_hint, timeout=timeout)

    def list_ilas(
        self,
        part_hint: str,
        timeout: int = 180,
        *,
        ltx: str | None = None,
    ) -> ListIlasResult:
        return list_ilas(part_hint, timeout=timeout, ltx=ltx)

    def arm_ila(
        self,
        part_hint: str,
        ila_name: str,
        samples: int,
        timeout: int = 120,
        *,
        ltx: str | None = None,
        windows: int = 1,
        trigger_position: int | None = None,
        triggers: list[ProbeTrigger] | None = None,
        advanced_trigger: IlaTriggerConfig | None = None,
    ) -> IlaArmResult:
        del part_hint, ila_name, samples, timeout, ltx, windows, trigger_position, triggers
        del advanced_trigger
        raise XdbError("decoupled ILA lifecycle is not supported by the Vivado backend")

    def compile_ila_trigger(
        self,
        part_hint: str,
        ila_name: str,
        tsm_path: str,
        timeout: int = 120,
        *,
        ltx: str | None = None,
    ) -> IlaTriggerCompileResult:
        del part_hint, ila_name, tsm_path, timeout, ltx
        raise XdbError("advanced ILA triggers are not supported by the Vivado backend")

    def ila_status(
        self,
        part_hint: str,
        ila_name: str,
        timeout: int = 120,
        *,
        ltx: str | None = None,
    ) -> IlaStatusResult:
        del part_hint, ila_name, timeout, ltx
        raise XdbError("decoupled ILA lifecycle is not supported by the Vivado backend")

    def wait_ila(
        self,
        part_hint: str,
        ila_name: str,
        timeout: int = 120,
        *,
        ltx: str | None = None,
    ) -> IlaStatusResult:
        del part_hint, ila_name, timeout, ltx
        raise XdbError("decoupled ILA lifecycle is not supported by the Vivado backend")

    def upload_ila(
        self,
        part_hint: str,
        ila_name: str,
        output_path: str,
        timeout: int = 120,
        *,
        ltx: str | None = None,
        export_format: str = "CSV",
        probe_names: list[str] | None = None,
        start_window: int = 0,
        window_count: int | None = None,
        start_sample: int = 0,
        sample_count: int | None = None,
        include_gap: bool = False,
    ) -> CaptureResult:
        del part_hint, ila_name, output_path, timeout, ltx, export_format, probe_names
        del start_window, window_count, start_sample, sample_count, include_gap
        raise XdbError("decoupled ILA lifecycle is not supported by the Vivado backend")

    def capture(
        self,
        part_hint: str,
        ila_name: str,
        csv_path: str,
        samples: int,
        timeout: int = 120,
        *,
        ltx: str | None = None,
        windows: int = 1,
        trigger_position: int | None = None,
        triggers: list[ProbeTrigger] | None = None,
    ) -> CaptureResult:
        if windows != 1 or trigger_position is not None or triggers:
            raise XdbError(
                "Vivado backend does not support multi-window, explicit-position, or probe-trigger capture"
            )
        result = dict(capture(part_hint, ila_name, csv_path, samples, timeout=timeout, ltx=ltx))
        result.update(
            {
                "windows": 1,
                "total_samples": samples,
                "trigger_position": samples // 2 if trigger_position is None else trigger_position,
                "triggers": [],
            }
        )
        return cast(CaptureResult, result)

    def list_vios(
        self, part_hint: str, timeout: int = 180, *, ltx: str | None = None
    ) -> dict[str, object]:
        del part_hint, timeout, ltx
        raise XdbError("VIO is not supported by the Vivado backend")

    def read_vio(
        self,
        part_hint: str,
        vio_name: str,
        probes: list[str] | None = None,
        timeout: int = 120,
        *,
        ltx: str | None = None,
    ) -> dict[str, object]:
        del part_hint, vio_name, probes, timeout, ltx
        raise XdbError("VIO is not supported by the Vivado backend")

    def write_vio(
        self,
        part_hint: str,
        vio_name: str,
        values: dict[str, int],
        timeout: int = 120,
        *,
        ltx: str | None = None,
    ) -> dict[str, object]:
        del part_hint, vio_name, values, timeout, ltx
        raise XdbError("VIO is not supported by the Vivado backend")

    def list_instruments(self, part_hint: str, timeout: int = 180) -> InstrumentsResult:
        ilas = self.list_ilas(part_hint, timeout=timeout)
        instruments = [
            {
                "type": "ila",
                "name": ila.get("name", ""),
                "capabilities": [Capability.ILA_LIST.value, Capability.ILA_BASIC_CAPTURE.value],
            }
            for ila in ilas.get("ilas", [])
        ]
        return cast(
            InstrumentsResult,
            {
                "target": ilas.get("target", ""),
                "part": ilas.get("part", ""),
                "instruments": instruments,
            },
        )

    def capabilities(self) -> set[Capability]:
        return {
            Capability.TARGETS,
            Capability.PROGRAM,
            Capability.ILA_LIST,
            Capability.ILA_BASIC_CAPTURE,
            Capability.INSTRUMENTS_LIST,
        }


def _stop_process_tree(process: subprocess.Popen[str]) -> tuple[str, str]:
    if process.poll() is None:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
        except OSError:
            process.terminate()
    try:
        return process.communicate(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except OSError:
            process.kill()
        return process.communicate()


def _run_vivado_tcl(tcl: str, args: list[str], timeout: int = 120) -> VivadoResult:
    with tempfile.NamedTemporaryFile("w", suffix=".tcl", delete=False) as tf:
        tf.write(tcl)
        tcl_path = tf.name

    cmd = ["vivado", "-mode", "batch", "-source", tcl_path, "-notrace", "-nolog", "-nojournal"]
    if args:
        cmd += ["-tclargs", *args]

    env = os.environ.copy()
    process: subprocess.Popen[str] | None = None
    try:
        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
                start_new_session=os.name == "posix",
            )
        except FileNotFoundError as e:
            raise VivadoError(
                "vivado executable not found in PATH. Run inside a Xilinx-enabled shell "
                "(e.g., xilinx-shell) or source Vivado settings first."
            ) from e
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as e:
            stdout, stderr = _stop_process_tree(process)
            raise VivadoError(
                f"vivado timed out after {timeout} seconds\n"
                f"cmd: {' '.join(shlex.quote(x) for x in cmd)}\n"
                f"stdout:\n{stdout}\n"
                f"stderr:\n{stderr}"
            ) from e
    finally:
        try:
            Path(tcl_path).unlink(missing_ok=True)
        except OSError:
            pass

    if process.returncode != 0:
        raise VivadoError(
            f"vivado failed (rc={process.returncode})\n"
            f"cmd: {' '.join(shlex.quote(x) for x in cmd)}\n"
            f"stdout:\n{stdout}\n"
            f"stderr:\n{stderr}"
        )
    return VivadoResult(stdout=stdout, stderr=stderr)


def _extract_json(stdout: str) -> dict[str, Any]:
    start = "XDB_JSON_BEGIN"
    end = "XDB_JSON_END"
    i = stdout.find(start)
    j = stdout.find(end)
    if i == -1 or j == -1 or j <= i:
        raise VivadoError(f"could not find JSON markers in Vivado output\n{stdout}")
    payload = stdout[i + len(start) : j].strip()
    return json.loads(payload)


def list_targets(part_hint: str | None, timeout: int = 120) -> TargetsResult:
    tcl = r"""
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
"""
    res = _run_vivado_tcl(tcl, [], timeout=timeout)
    data = _extract_json(res.stdout)
    if part_hint:
        ph = part_hint.lower()
        data["targets"] = [
            t for t in data.get("targets", []) if ph in str(t.get("part", "")).lower()
        ]
    return cast(TargetsResult, data)


def program(bit: str, ltx: str | None, part_hint: str, timeout: int = 300) -> ProgramResult:
    tcl = r"""
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
"""
    res = _run_vivado_tcl(tcl, [part_hint, bit, ltx or ""], timeout=timeout)
    return cast(ProgramResult, _extract_json(res.stdout))


def list_ilas(part_hint: str, timeout: int = 180, *, ltx: str | None = None) -> ListIlasResult:
    tcl = r"""
set part_hint [lindex $argv 0]
set ltx [lindex $argv 1]
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
if {$ltx ne ""} { set_property PROBES.FILE $ltx $dev }
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
    set w "null"
    if {[lsearch -exact [list_property $p] PORT_WIDTH] >= 0} {
      set w [get_property PORT_WIDTH $p]
    }
    append out "{\"name\":\"$pn\",\"width\":" $w "}"
  }
  append out "]}"
}
append out "]}"
puts "XDB_JSON_BEGIN"
puts $out
puts "XDB_JSON_END"
exit 0
"""
    res = _run_vivado_tcl(tcl, [part_hint, ltx or ""], timeout=timeout)
    return cast(ListIlasResult, _extract_json(res.stdout))


def capture(
    part_hint: str,
    ila_name: str,
    csv_path: str,
    samples: int,
    timeout: int = 120,
    *,
    ltx: str | None = None,
) -> CaptureResult:
    tcl = r"""
set part_hint [lindex $argv 0]
set ila_name [lindex $argv 1]
set csv_path [lindex $argv 2]
set samples [lindex $argv 3]
set ltx [lindex $argv 4]

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
if {$ltx ne ""} { set_property PROBES.FILE $ltx $dev }
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
"""
    res = _run_vivado_tcl(
        tcl,
        [part_hint, ila_name, csv_path, str(samples), ltx or ""],
        timeout=timeout,
    )
    return cast(CaptureResult, _extract_json(res.stdout))
