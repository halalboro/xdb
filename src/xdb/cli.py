from __future__ import annotations

import argparse
import json
import os
import sys
import traceback

from . import __version__
from .backend.base import Capability
from .backend.select import select_backend
from .errors import UnsupportedOperationError, XdbError
from .sim.client import (
    add_breakpoint,
    add_wave,
    assert_signal_session,
    assert_tcl_session,
    clear_breakpoints,
    close_session,
    coyote_clear_completed_session,
    coyote_completed_session,
    coyote_csr_read_session,
    coyote_csr_write_session,
    coyote_invoke_session,
    coyote_irq_wait_session,
    coyote_mem_list_session,
    coyote_mem_map_session,
    coyote_mem_read_session,
    coyote_mem_reset_session,
    coyote_mem_unmap_session,
    coyote_mem_write_session,
    coyote_status_session,
    expect_change_session,
    expect_signal_session,
    describe_session,
    force_session,
    get_many_signals,
    get_objects,
    get_scopes,
    get_signal,
    read_signals,
    launch_session,
    release_session,
    restart_session,
    run_session,
    set_top,
    source_session,
    snapshot_session,
    step_session,
    time_session,
    vcd_start_session,
    vcd_status_session,
    vcd_stop_session,
    tcl_session,
    wait_until_session,
    wait_until_signal_session,
    diff_snapshot_session,
    watch_changes_session,
)
from .sim.coyote import parse_hex_bytes
from .sim.daemon import run_daemon


def _print(data: dict) -> None:
    print(json.dumps(data, indent=2, sort_keys=False))


def _resolve_part_hint(cli_value: str | None) -> str | None:
    return cli_value or os.environ.get("FPGA_PART_HINT")


def _require_part_hint(cli_value: str | None) -> str:
    part_hint = _resolve_part_hint(cli_value)
    if not part_hint:
        raise XdbError(
            "missing FPGA part hint: pass --part-hint "
            "(or --fpga-part-hint) or set FPGA_PART_HINT"
        )
    return part_hint


def _resolve_bitstream(cli_value: str | None) -> str:
    bit = cli_value or os.environ.get("FPGA_BITSTREAM")
    if not bit:
        raise XdbError("missing bitstream: pass --bit or set FPGA_BITSTREAM")
    if not os.path.isfile(bit):
        raise XdbError(f"bitstream not found: {bit}")
    return bit


def _resolve_ltx(cli_value: str | None) -> str:
    ltx = cli_value or os.environ.get("FPGA_LTX")
    if not ltx:
        raise XdbError("missing ltx: pass --ltx or set FPGA_LTX")
    if not os.path.isfile(ltx):
        raise XdbError(f"ltx not found: {ltx}")
    return ltx


def _is_power_of_two(v: int) -> bool:
    return v > 0 and (v & (v - 1)) == 0


def _validate_samples(samples: int) -> int:
    if samples <= 0:
        raise XdbError("--samples must be > 0")
    if not _is_power_of_two(samples):
        raise XdbError("--samples must be a power of two (e.g., 128, 256, 512, 1024)")
    return samples


def _unsupported_operation_message(
    operation: str,
    backend_name: str,
    target_hint: str | None,
) -> str:
    suggestion = ""
    if backend_name == "vivado":
        suggestion = " try XDB_BACKEND=chipscopy for Versal features"
    elif backend_name == "chipscopy":
        suggestion = " try XDB_BACKEND=vivado for UltraScale+ ILA workflows"
    tgt = target_hint or "<unspecified>"
    return (
        f"operation not supported: operation={operation} backend={backend_name} "
        f"target={tgt}.{suggestion}".strip()
    )


def _require_capability(
    backend,
    capability: Capability,
    operation: str,
    target_hint: str | None,
) -> None:
    caps = backend.capabilities()
    if capability not in caps:
        raise UnsupportedOperationError(
            _unsupported_operation_message(operation, backend.name, target_hint)
        )


def _join_tokens(values: list[str]) -> str | None:
    if not values:
        return None
    return " ".join(values).strip() or None


def _resolve_tcl_script(values: list[str], file_path: str | None) -> str:
    if file_path and values:
        raise XdbError("pass either Tcl tokens or --file, not both")
    if file_path:
        if file_path == "-":
            return sys.stdin.read()
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                return f.read()
        except OSError as e:
            raise XdbError(f"failed to read Tcl script: {file_path}") from e
    script = _join_tokens(values)
    if script == "-":
        return sys.stdin.read()
    if not script:
        raise XdbError("missing Tcl script")
    return script


def _env_debug_enabled() -> bool:
    for name in ("XDB_DEBUG", "XDB_VERBOSE"):
        value = os.environ.get(name)
        if value is None:
            continue
        if value.strip().lower() in {"", "0", "false", "no", "off"}:
            continue
        return True
    return False


def _resolve_sim_step_tokens(values: list[str] | None) -> list[str]:
    if not values:
        raise XdbError("missing step duration")
    tokens = [value.strip() for value in values if value.strip()]
    if not tokens:
        raise XdbError("missing step duration")
    return tokens


def _validate_positive_timeout_seconds(value: float | None) -> float | None:
    if value is None:
        return None
    if value <= 0:
        raise XdbError("--timeout must be > 0")
    return value


def _validate_positive_iteration_limit(value: int | None) -> int | None:
    if value is None:
        return None
    if value <= 0:
        raise XdbError("--max-iterations must be > 0")
    return value


def _parse_int(value: str, *, what: str) -> int:
    text = value.strip()
    if not text:
        raise XdbError(f"missing {what}")
    try:
        return int(text, 0)
    except ValueError as e:
        raise XdbError(f"invalid {what}: {value}") from e


def _read_binary_file(path: str) -> bytes:
    resolved = path if path != "-" else None
    try:
        if resolved is None:
            return sys.stdin.buffer.read()
        with open(path, "rb") as f:
            return f.read()
    except OSError as e:
        raise XdbError(f"failed to read binary payload: {path}") from e


def _resolve_mem_payload(args: argparse.Namespace) -> bytes:
    if args.hex_data is not None:
        return parse_hex_bytes(args.hex_data)
    if args.text_data is not None:
        return args.text_data.encode("utf-8")
    if args.file is not None:
        return _read_binary_file(args.file)
    raise XdbError("provide one of --hex, --text, or --file")


def _configure_diagnostics(debug: bool) -> None:
    effective_debug = debug or _env_debug_enabled()
    if effective_debug:
        os.environ["XDB_DEBUG"] = "1"
        os.environ["XDB_VERBOSE"] = "1"
    else:
        os.environ.pop("XDB_DEBUG", None)
        os.environ.pop("XDB_VERBOSE", None)


def _print_error(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)


def _add_debug_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--debug",
        "--verbose",
        dest="debug",
        action="store_true",
        help="print tracebacks and detailed Vivado diagnostics on failure",
    )


def main() -> None:  # pyright: ignore[reportGeneralTypeIssues]
    p = argparse.ArgumentParser(prog="xdb", description="Generic FPGA ILA debug toolkit")
    p.add_argument("--version", action="version", version=f"xdb {__version__}")
    _add_debug_flag(p)

    sub = p.add_subparsers(dest="cmd", required=True)

    s_targets = sub.add_parser("targets")
    _add_debug_flag(s_targets)
    s_targets.add_argument("--part-hint", "--fpga-part-hint", dest="part_hint", default=None)
    s_targets.add_argument("--fdev-name", default=os.environ.get("FDEV_NAME"))
    s_targets.add_argument("--fpga-bdf", default=os.environ.get("FPGA_BDF"))
    s_targets.add_argument("--timeout", type=int, default=120)

    s_program = sub.add_parser("program")
    _add_debug_flag(s_program)
    s_program.add_argument("--bit", default=None)
    s_program.add_argument("--ltx", default=None)
    s_program.add_argument("--part-hint", "--fpga-part-hint", dest="part_hint", default=None)
    s_program.add_argument("--fdev-name", default=os.environ.get("FDEV_NAME"))
    s_program.add_argument("--fpga-bdf", default=os.environ.get("FPGA_BDF"))
    s_program.add_argument("--timeout", type=int, default=300)

    s_ilas = sub.add_parser("ilas")
    _add_debug_flag(s_ilas)
    s_ilas.add_argument("--part-hint", "--fpga-part-hint", dest="part_hint", default=None)
    s_ilas.add_argument("--fdev-name", default=os.environ.get("FDEV_NAME"))
    s_ilas.add_argument("--fpga-bdf", default=os.environ.get("FPGA_BDF"))
    s_ilas.add_argument("--timeout", type=int, default=180)

    s_capture = sub.add_parser("capture")
    _add_debug_flag(s_capture)
    s_capture.add_argument("--part-hint", "--fpga-part-hint", dest="part_hint", default=None)
    s_capture.add_argument("--fdev-name", default=os.environ.get("FDEV_NAME"))
    s_capture.add_argument("--fpga-bdf", default=os.environ.get("FPGA_BDF"))
    s_capture.add_argument("--ila", required=True)
    s_capture.add_argument("--csv", required=True)
    s_capture.add_argument("--samples", type=int, default=2048)
    s_capture.add_argument("--timeout", type=int, default=120)

    s_instruments = sub.add_parser("instruments")
    instruments_sub = s_instruments.add_subparsers(dest="instruments_cmd", required=True)
    s_instruments_list = instruments_sub.add_parser("list")
    _add_debug_flag(s_instruments_list)
    s_instruments_list.add_argument("--part-hint", "--fpga-part-hint", dest="part_hint", default=None)
    s_instruments_list.add_argument("--timeout", type=int, default=180)

    s_sim = sub.add_parser("sim", description="Persistent Vivado simulation session control")
    _add_debug_flag(s_sim)
    sim_sub = s_sim.add_subparsers(dest="sim_cmd", required=True)

    def add_sim_session_arg(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--session", default=None)

    s_sim_launch = sim_sub.add_parser("launch")
    _add_debug_flag(s_sim_launch)
    add_sim_session_arg(s_sim_launch)
    s_sim_launch.add_argument("--simset", default=None)
    s_sim_launch.add_argument(
        "--mode",
        choices=["behavioral", "post-synth", "post-impl"],
        default=None,
    )
    s_sim_launch.add_argument("--top", default=None)
    s_sim_launch.add_argument("--replace", action="store_true")
    s_sim_launch.add_argument("--timeout", type=int, default=300)

    s_sim_run = sim_sub.add_parser("run")
    _add_debug_flag(s_sim_run)
    add_sim_session_arg(s_sim_run)
    s_sim_run.add_argument("time", nargs="*")

    s_sim_restart = sim_sub.add_parser("restart")
    _add_debug_flag(s_sim_restart)
    add_sim_session_arg(s_sim_restart)

    s_sim_close = sim_sub.add_parser("close")
    _add_debug_flag(s_sim_close)
    add_sim_session_arg(s_sim_close)

    s_sim_time = sim_sub.add_parser("time")
    _add_debug_flag(s_sim_time)
    add_sim_session_arg(s_sim_time)

    s_sim_describe = sim_sub.add_parser("describe", help="summarize the current simulation session")
    _add_debug_flag(s_sim_describe)
    add_sim_session_arg(s_sim_describe)

    s_sim_get = sim_sub.add_parser("get")
    _add_debug_flag(s_sim_get)
    add_sim_session_arg(s_sim_get)
    s_sim_get.add_argument("signal")

    s_sim_get_many = sim_sub.add_parser("get-many")
    _add_debug_flag(s_sim_get_many)
    add_sim_session_arg(s_sim_get_many)
    s_sim_get_many.add_argument("pattern")

    s_sim_read = sim_sub.add_parser("read", help="read several named signals in one request")
    _add_debug_flag(s_sim_read)
    add_sim_session_arg(s_sim_read)
    s_sim_read.add_argument("signals", nargs="+")

    s_sim_scopes = sim_sub.add_parser("scopes")
    _add_debug_flag(s_sim_scopes)
    add_sim_session_arg(s_sim_scopes)
    s_sim_scopes.add_argument("scope", nargs="?", default=None)

    s_sim_objects = sim_sub.add_parser("objects")
    _add_debug_flag(s_sim_objects)
    add_sim_session_arg(s_sim_objects)
    s_sim_objects.add_argument("scope")

    s_sim_top = sim_sub.add_parser("top")
    _add_debug_flag(s_sim_top)
    add_sim_session_arg(s_sim_top)
    s_sim_top.add_argument("module")

    s_sim_snapshot = sim_sub.add_parser("snapshot", help="capture a structured snapshot of a scope subtree")
    _add_debug_flag(s_sim_snapshot)
    add_sim_session_arg(s_sim_snapshot)
    s_sim_snapshot.add_argument("scope")
    s_sim_snapshot.add_argument("--name", default=None)

    s_sim_diff_snapshot = sim_sub.add_parser("diff-snapshot", help="compare two named snapshots")
    _add_debug_flag(s_sim_diff_snapshot)
    add_sim_session_arg(s_sim_diff_snapshot)
    s_sim_diff_snapshot.add_argument("before")
    s_sim_diff_snapshot.add_argument("after")

    s_sim_watch_changes = sim_sub.add_parser("watch-changes", help="snapshot a scope, run, and diff the result")
    _add_debug_flag(s_sim_watch_changes)
    add_sim_session_arg(s_sim_watch_changes)
    s_sim_watch_changes.add_argument("scope")
    s_sim_watch_changes.add_argument("--for", dest="duration", nargs="+", required=True)

    s_sim_wave = sim_sub.add_parser("wave")
    _add_debug_flag(s_sim_wave)
    sim_wave_sub = s_sim_wave.add_subparsers(dest="sim_wave_cmd", required=True)
    s_sim_wave_add = sim_wave_sub.add_parser("add")
    _add_debug_flag(s_sim_wave_add)
    add_sim_session_arg(s_sim_wave_add)
    s_sim_wave_add.add_argument("pattern")

    s_sim_vcd = sim_sub.add_parser("vcd", help="control persistent VCD dumping")
    _add_debug_flag(s_sim_vcd)
    sim_vcd_sub = s_sim_vcd.add_subparsers(dest="sim_vcd_cmd", required=True)
    s_sim_vcd_start = sim_vcd_sub.add_parser("start")
    _add_debug_flag(s_sim_vcd_start)
    add_sim_session_arg(s_sim_vcd_start)
    s_sim_vcd_start.add_argument("file")
    s_sim_vcd_start.add_argument("scope", nargs="?", default=None)
    s_sim_vcd_stop = sim_vcd_sub.add_parser("stop")
    _add_debug_flag(s_sim_vcd_stop)
    add_sim_session_arg(s_sim_vcd_stop)
    s_sim_vcd_status = sim_vcd_sub.add_parser("status")
    _add_debug_flag(s_sim_vcd_status)
    add_sim_session_arg(s_sim_vcd_status)

    s_sim_step = sim_sub.add_parser("step")
    _add_debug_flag(s_sim_step)
    add_sim_session_arg(s_sim_step)
    s_sim_step.add_argument("arg", nargs="*", default=[])

    s_sim_until = sim_sub.add_parser(
        "until",
        aliases=["wait", "wait-on-condition"],
        help="run in steps until a Tcl expression becomes true",
        description=(
            "Run the simulator in repeated time steps until the given Tcl expression "
            "evaluates true. The default step is '10 ns'. Use --timeout and/or "
            "--max-iterations to bound the wait. Example: xdb sim until "
            "'{[get_value /tb_top/done] eq \"1\"}'"
        ),
    )
    _add_debug_flag(s_sim_until)
    add_sim_session_arg(s_sim_until)
    s_sim_until.add_argument(
        "--step",
        nargs="+",
        default=["10", "ns"],
        metavar="STEP",
        help="simulation time step between condition checks, default: 10 ns",
    )
    s_sim_until.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="maximum wall-clock seconds to wait before failing",
    )
    s_sim_until.add_argument(
        "--max-iterations",
        type=int,
        default=None,
        help="maximum number of run/check iterations before failing",
    )
    s_sim_until.add_argument(
        "expr",
        nargs="+",
        metavar="TCL_EXPR",
        help="Tcl expr body, e.g. '{[get_value /tb_top/done] eq \"1\"}'",
    )

    s_sim_until_signal = sim_sub.add_parser(
        "until-signal",
        aliases=["wait-signal", "wait-on-signal"],
        help="run in steps until a signal reaches an exact value",
        description=(
            "Run the simulator in repeated time steps until get_value <signal> equals "
            "the expected value exactly. The default step is '10 ns'. Use --timeout "
            "and/or --max-iterations to bound the wait. Example: xdb sim until-signal "
            "/tb_top/done 1"
        ),
    )
    _add_debug_flag(s_sim_until_signal)
    add_sim_session_arg(s_sim_until_signal)
    s_sim_until_signal.add_argument(
        "--step",
        nargs="+",
        default=["10", "ns"],
        metavar="STEP",
        help="simulation time step between value checks, default: 10 ns",
    )
    s_sim_until_signal.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="maximum wall-clock seconds to wait before failing",
    )
    s_sim_until_signal.add_argument(
        "--max-iterations",
        type=int,
        default=None,
        help="maximum number of run/check iterations before failing",
    )
    s_sim_until_signal.add_argument("signal", help="hierarchical signal path")
    s_sim_until_signal.add_argument("value", help="exact expected get_value result")

    s_sim_assert_signal = sim_sub.add_parser("assert-signal", help="assert a signal has an exact value now")
    _add_debug_flag(s_sim_assert_signal)
    add_sim_session_arg(s_sim_assert_signal)
    s_sim_assert_signal.add_argument("signal")
    s_sim_assert_signal.add_argument("value")

    s_sim_assert_tcl = sim_sub.add_parser("assert-tcl", help="assert a Tcl expression is true now")
    _add_debug_flag(s_sim_assert_tcl)
    add_sim_session_arg(s_sim_assert_tcl)
    s_sim_assert_tcl.add_argument("expr", nargs="+")

    s_sim_expect_signal = sim_sub.add_parser("expect-signal", help="expect a signal to reach a value within a simulation time bound")
    _add_debug_flag(s_sim_expect_signal)
    add_sim_session_arg(s_sim_expect_signal)
    s_sim_expect_signal.add_argument("--within", nargs="+", required=True)
    s_sim_expect_signal.add_argument("signal")
    s_sim_expect_signal.add_argument("value")

    s_sim_expect_change = sim_sub.add_parser("expect-change", help="expect a signal to change within a simulation time bound")
    _add_debug_flag(s_sim_expect_change)
    add_sim_session_arg(s_sim_expect_change)
    s_sim_expect_change.add_argument("--within", nargs="+", required=True)
    s_sim_expect_change.add_argument("signal")

    s_sim_breakpoint = sim_sub.add_parser("breakpoint")
    _add_debug_flag(s_sim_breakpoint)
    sim_bp_sub = s_sim_breakpoint.add_subparsers(dest="sim_bp_cmd", required=True)
    s_sim_breakpoint_add = sim_bp_sub.add_parser("add")
    _add_debug_flag(s_sim_breakpoint_add)
    add_sim_session_arg(s_sim_breakpoint_add)
    s_sim_breakpoint_add.add_argument("condition", nargs="+")
    s_sim_breakpoint_clear = sim_bp_sub.add_parser("clear")
    _add_debug_flag(s_sim_breakpoint_clear)
    add_sim_session_arg(s_sim_breakpoint_clear)

    s_sim_tcl = sim_sub.add_parser("tcl")
    _add_debug_flag(s_sim_tcl)
    add_sim_session_arg(s_sim_tcl)
    s_sim_tcl.add_argument("--file", default=None)
    s_sim_tcl.add_argument("script", nargs="*")

    s_sim_source = sim_sub.add_parser("source")
    _add_debug_flag(s_sim_source)
    add_sim_session_arg(s_sim_source)
    s_sim_source.add_argument("path")

    s_sim_force = sim_sub.add_parser("force")
    _add_debug_flag(s_sim_force)
    add_sim_session_arg(s_sim_force)
    s_sim_force.add_argument("--radix", default=None)
    s_sim_force.add_argument("--repeat-every", default=None)
    s_sim_force.add_argument("--cancel-after", default=None)
    s_sim_force.add_argument("signal")
    s_sim_force.add_argument("values", nargs="+")

    s_sim_release = sim_sub.add_parser("release")
    _add_debug_flag(s_sim_release)
    add_sim_session_arg(s_sim_release)
    s_sim_release.add_argument("--all", action="store_true")
    s_sim_release.add_argument("signal", nargs="?", default=None)

    s_sim_csr = sim_sub.add_parser("csr", help="Coyote CSR access")
    _add_debug_flag(s_sim_csr)
    sim_csr_sub = s_sim_csr.add_subparsers(dest="sim_csr_cmd", required=True)
    s_sim_csr_read = sim_csr_sub.add_parser("read")
    _add_debug_flag(s_sim_csr_read)
    add_sim_session_arg(s_sim_csr_read)
    s_sim_csr_read.add_argument("addr")
    s_sim_csr_read.add_argument("--timeout", type=float, default=None)
    s_sim_csr_write = sim_csr_sub.add_parser("write")
    _add_debug_flag(s_sim_csr_write)
    add_sim_session_arg(s_sim_csr_write)
    s_sim_csr_write.add_argument("addr")
    s_sim_csr_write.add_argument("value")

    s_sim_mem = sim_sub.add_parser("mem", help="Coyote host memory access")
    _add_debug_flag(s_sim_mem)
    sim_mem_sub = s_sim_mem.add_subparsers(dest="sim_mem_cmd", required=True)
    s_sim_mem_map = sim_mem_sub.add_parser("map")
    _add_debug_flag(s_sim_mem_map)
    add_sim_session_arg(s_sim_mem_map)
    s_sim_mem_map.add_argument("space")
    s_sim_mem_map.add_argument("addr")
    s_sim_mem_map.add_argument("size")
    s_sim_mem_unmap = sim_mem_sub.add_parser("unmap")
    _add_debug_flag(s_sim_mem_unmap)
    add_sim_session_arg(s_sim_mem_unmap)
    s_sim_mem_unmap.add_argument("space")
    s_sim_mem_unmap.add_argument("addr")
    s_sim_mem_list = sim_mem_sub.add_parser("list")
    _add_debug_flag(s_sim_mem_list)
    add_sim_session_arg(s_sim_mem_list)
    s_sim_mem_list.add_argument("space", nargs="?", default="host")
    s_sim_mem_reset = sim_mem_sub.add_parser("reset")
    _add_debug_flag(s_sim_mem_reset)
    add_sim_session_arg(s_sim_mem_reset)
    s_sim_mem_reset.add_argument("space", nargs="?", default="host")
    s_sim_mem_read = sim_mem_sub.add_parser("read")
    _add_debug_flag(s_sim_mem_read)
    add_sim_session_arg(s_sim_mem_read)
    s_sim_mem_read.add_argument("space")
    s_sim_mem_read.add_argument("addr")
    s_sim_mem_read.add_argument("size")
    s_sim_mem_write = sim_mem_sub.add_parser("write")
    _add_debug_flag(s_sim_mem_write)
    add_sim_session_arg(s_sim_mem_write)
    s_sim_mem_write.add_argument("space")
    s_sim_mem_write.add_argument("addr")
    mem_payload_group = s_sim_mem_write.add_mutually_exclusive_group(required=True)
    mem_payload_group.add_argument("--hex", dest="hex_data", default=None)
    mem_payload_group.add_argument("--text", dest="text_data", default=None)
    mem_payload_group.add_argument("--file", default=None)

    s_sim_invoke = sim_sub.add_parser("invoke", help="Coyote high-level invoke")
    _add_debug_flag(s_sim_invoke)
    add_sim_session_arg(s_sim_invoke)
    s_sim_invoke.add_argument("opcode")
    s_sim_invoke.add_argument("--addr", default=None)
    s_sim_invoke.add_argument("--len", dest="length", default=None)
    s_sim_invoke.add_argument("--stream", default="host")
    s_sim_invoke.add_argument("--dest", default="0")
    s_sim_invoke.add_argument("--last", action=argparse.BooleanOptionalAction, default=True)
    s_sim_invoke.add_argument("--src-addr", default=None)
    s_sim_invoke.add_argument("--src-len", default=None)
    s_sim_invoke.add_argument("--src-stream", default="host")
    s_sim_invoke.add_argument("--src-dest", default="0")
    s_sim_invoke.add_argument("--dst-addr", default=None)
    s_sim_invoke.add_argument("--dst-len", default=None)
    s_sim_invoke.add_argument("--dst-stream", default="host")
    s_sim_invoke.add_argument("--dst-dest", default="0")

    s_sim_completed = sim_sub.add_parser("completed", help="Coyote completion counters")
    _add_debug_flag(s_sim_completed)
    add_sim_session_arg(s_sim_completed)
    s_sim_completed.add_argument("opcode")
    s_sim_completed.add_argument("--count", type=int, default=None)
    s_sim_completed.add_argument("--timeout", type=float, default=None)

    s_sim_clear_completed = sim_sub.add_parser("clear-completed")
    _add_debug_flag(s_sim_clear_completed)
    add_sim_session_arg(s_sim_clear_completed)

    s_sim_irq = sim_sub.add_parser("irq", help="Coyote IRQ handling")
    _add_debug_flag(s_sim_irq)
    sim_irq_sub = s_sim_irq.add_subparsers(dest="sim_irq_cmd", required=True)
    s_sim_irq_wait = sim_irq_sub.add_parser("wait")
    _add_debug_flag(s_sim_irq_wait)
    add_sim_session_arg(s_sim_irq_wait)
    s_sim_irq_wait.add_argument("--timeout", type=float, default=None)

    s_sim_coyote_status = sim_sub.add_parser("coyote-status")
    _add_debug_flag(s_sim_coyote_status)
    add_sim_session_arg(s_sim_coyote_status)

    s_simd = sub.add_parser("_simd", help=argparse.SUPPRESS)
    _add_debug_flag(s_simd)
    s_simd.add_argument("--anchor-dir", required=True)
    s_simd.add_argument("--session", default=None)
    s_simd.add_argument("--project", default="")
    s_simd.add_argument("--simset", required=True)
    s_simd.add_argument("--mode", required=True)
    s_simd.add_argument("--top", default="")
    s_simd.add_argument("--package-runtime", default="")
    s_simd.add_argument("--runtime-root", default="")
    s_simd.add_argument("--work-dir", default="")
    s_simd.add_argument("--compile-script", default="")
    s_simd.add_argument("--elaborate-script", default="")
    s_simd.add_argument("--simulate-script", default="")

    args = p.parse_args()
    _configure_diagnostics(bool(args.debug))

    try:
        if args.cmd == "_simd":
            run_daemon(
                anchor_dir=args.anchor_dir,
                session_name=args.session,
                project=args.project,
                simset=args.simset,
                mode=args.mode,
                top=args.top,
                package_runtime=args.package_runtime,
                runtime_root=args.runtime_root,
                work_dir=args.work_dir,
                compile_script=args.compile_script,
                elaborate_script=args.elaborate_script,
                simulate_script=args.simulate_script,
            )
            return

        if args.cmd == "sim":
            if args.sim_cmd == "launch":
                _print(
                    launch_session(
                        simset=args.simset,
                        mode=args.mode,
                        top=args.top,
                        session_name=args.session,
                        replace=args.replace,
                        timeout=args.timeout,
                    )
                )
            elif args.sim_cmd == "run":
                _print(run_session(args.session, args.time))
            elif args.sim_cmd == "restart":
                _print(restart_session(args.session))
            elif args.sim_cmd == "close":
                _print(close_session(args.session))
            elif args.sim_cmd == "time":
                _print(time_session(args.session))
            elif args.sim_cmd == "describe":
                _print(describe_session(args.session))
            elif args.sim_cmd == "get":
                _print(get_signal(args.session, args.signal))
            elif args.sim_cmd == "get-many":
                _print(get_many_signals(args.session, args.pattern))
            elif args.sim_cmd == "read":
                _print(read_signals(args.session, args.signals))
            elif args.sim_cmd == "scopes":
                _print(get_scopes(args.session, args.scope))
            elif args.sim_cmd == "objects":
                _print(get_objects(args.session, args.scope))
            elif args.sim_cmd == "top":
                _print(set_top(args.session, args.module))
            elif args.sim_cmd == "snapshot":
                _print(snapshot_session(args.session, args.scope, name=args.name))
            elif args.sim_cmd == "diff-snapshot":
                _print(diff_snapshot_session(args.session, args.before, args.after))
            elif args.sim_cmd == "watch-changes":
                _print(
                    watch_changes_session(
                        args.session,
                        args.scope,
                        _resolve_sim_step_tokens(args.duration),
                    )
                )
            elif args.sim_cmd == "wave" and args.sim_wave_cmd == "add":
                _print(add_wave(args.session, args.pattern))
            elif args.sim_cmd == "vcd" and args.sim_vcd_cmd == "start":
                _print(vcd_start_session(args.session, args.file, args.scope))
            elif args.sim_cmd == "vcd" and args.sim_vcd_cmd == "stop":
                _print(vcd_stop_session(args.session))
            elif args.sim_cmd == "vcd" and args.sim_vcd_cmd == "status":
                _print(vcd_status_session(args.session))
            elif args.sim_cmd == "step":
                _print(step_session(args.session, _join_tokens(args.arg)))
            elif args.sim_cmd in {"until", "wait", "wait-on-condition"}:
                _print(
                    wait_until_session(
                        args.session,
                        _join_tokens(args.expr) or "",
                        _resolve_sim_step_tokens(args.step),
                        timeout_seconds=_validate_positive_timeout_seconds(args.timeout),
                        max_iterations=_validate_positive_iteration_limit(args.max_iterations),
                    )
                )
            elif args.sim_cmd in {"until-signal", "wait-signal", "wait-on-signal"}:
                _print(
                    wait_until_signal_session(
                        args.session,
                        args.signal,
                        args.value,
                        _resolve_sim_step_tokens(args.step),
                        timeout_seconds=_validate_positive_timeout_seconds(args.timeout),
                        max_iterations=_validate_positive_iteration_limit(args.max_iterations),
                    )
                )
            elif args.sim_cmd == "assert-signal":
                _print(assert_signal_session(args.session, args.signal, args.value))
            elif args.sim_cmd == "assert-tcl":
                _print(assert_tcl_session(args.session, _join_tokens(args.expr) or ""))
            elif args.sim_cmd == "expect-signal":
                _print(
                    expect_signal_session(
                        args.session,
                        args.signal,
                        args.value,
                        _resolve_sim_step_tokens(args.within),
                    )
                )
            elif args.sim_cmd == "expect-change":
                _print(
                    expect_change_session(
                        args.session,
                        args.signal,
                        _resolve_sim_step_tokens(args.within),
                    )
                )
            elif args.sim_cmd == "breakpoint" and args.sim_bp_cmd == "add":
                _print(add_breakpoint(args.session, _join_tokens(args.condition) or ""))
            elif args.sim_cmd == "breakpoint" and args.sim_bp_cmd == "clear":
                _print(clear_breakpoints(args.session))
            elif args.sim_cmd == "tcl":
                _print(tcl_session(args.session, _resolve_tcl_script(args.script, args.file)))
            elif args.sim_cmd == "source":
                _print(source_session(args.session, args.path))
            elif args.sim_cmd == "force":
                _print(
                    force_session(
                        args.session,
                        args.signal,
                        args.values,
                        radix=args.radix,
                        repeat_every=args.repeat_every,
                        cancel_after=args.cancel_after,
                    )
                )
            elif args.sim_cmd == "release":
                _print(release_session(args.session, args.signal, all_forces=args.all))
            elif args.sim_cmd == "csr" and args.sim_csr_cmd == "read":
                _print(
                    coyote_csr_read_session(
                        args.session,
                        _parse_int(args.addr, what="CSR address"),
                        timeout_seconds=_validate_positive_timeout_seconds(args.timeout),
                    )
                )
            elif args.sim_cmd == "csr" and args.sim_csr_cmd == "write":
                _print(
                    coyote_csr_write_session(
                        args.session,
                        _parse_int(args.addr, what="CSR address"),
                        _parse_int(args.value, what="CSR value"),
                    )
                )
            elif args.sim_cmd == "mem" and args.sim_mem_cmd == "map":
                _print(
                    coyote_mem_map_session(
                        args.session,
                        args.space,
                        _parse_int(args.addr, what="memory address"),
                        _parse_int(args.size, what="memory size"),
                    )
                )
            elif args.sim_cmd == "mem" and args.sim_mem_cmd == "unmap":
                _print(
                    coyote_mem_unmap_session(
                        args.session,
                        args.space,
                        _parse_int(args.addr, what="memory address"),
                    )
                )
            elif args.sim_cmd == "mem" and args.sim_mem_cmd == "list":
                _print(coyote_mem_list_session(args.session, args.space))
            elif args.sim_cmd == "mem" and args.sim_mem_cmd == "reset":
                _print(coyote_mem_reset_session(args.session, args.space))
            elif args.sim_cmd == "mem" and args.sim_mem_cmd == "write":
                _print(
                    coyote_mem_write_session(
                        args.session,
                        args.space,
                        _parse_int(args.addr, what="memory address"),
                        _resolve_mem_payload(args).hex(),
                    )
                )
            elif args.sim_cmd == "mem" and args.sim_mem_cmd == "read":
                _print(
                    coyote_mem_read_session(
                        args.session,
                        args.space,
                        _parse_int(args.addr, what="memory address"),
                        _parse_int(args.size, what="memory size"),
                    )
                )
            elif args.sim_cmd == "invoke":
                _print(
                    coyote_invoke_session(
                        args.session,
                        opcode=args.opcode,
                        addr=None if args.addr is None else _parse_int(args.addr, what="address"),
                        length=None
                        if args.length is None
                        else _parse_int(args.length, what="length"),
                        stream_name=args.stream,
                        dest=_parse_int(args.dest, what="destination stream"),
                        last=bool(args.last),
                        src_addr=None
                        if args.src_addr is None
                        else _parse_int(args.src_addr, what="source address"),
                        src_length=None
                        if args.src_len is None
                        else _parse_int(args.src_len, what="source length"),
                        src_stream_name=args.src_stream,
                        src_dest=_parse_int(args.src_dest, what="source destination stream"),
                        dst_addr=None
                        if args.dst_addr is None
                        else _parse_int(args.dst_addr, what="destination address"),
                        dst_length=None
                        if args.dst_len is None
                        else _parse_int(args.dst_len, what="destination length"),
                        dst_stream_name=args.dst_stream,
                        dst_dest=_parse_int(args.dst_dest, what="destination stream"),
                    )
                )
            elif args.sim_cmd == "completed":
                _print(
                    coyote_completed_session(
                        args.session,
                        args.opcode,
                        target_count=args.count,
                        timeout_seconds=_validate_positive_timeout_seconds(args.timeout),
                    )
                )
            elif args.sim_cmd == "clear-completed":
                _print(coyote_clear_completed_session(args.session))
            elif args.sim_cmd == "irq" and args.sim_irq_cmd == "wait":
                _print(
                    coyote_irq_wait_session(
                        args.session,
                        timeout_seconds=_validate_positive_timeout_seconds(args.timeout),
                    )
                )
            elif args.sim_cmd == "coyote-status":
                _print(coyote_status_session(args.session))
            else:
                p.error("unknown sim command")
            return

        backend = select_backend()
        if args.cmd == "targets":
            _require_capability(
                backend,
                Capability.TARGETS,
                operation="targets",
                target_hint=_resolve_part_hint(args.part_hint),
            )
            _print(backend.list_targets(_resolve_part_hint(args.part_hint), timeout=args.timeout))
        elif args.cmd == "program":
            part_hint = _require_part_hint(args.part_hint)
            _require_capability(
                backend,
                Capability.PROGRAM,
                operation="program",
                target_hint=part_hint,
            )
            bit = _resolve_bitstream(args.bit)
            ltx = _resolve_ltx(args.ltx)
            result = backend.program(
                bit,
                ltx,
                part_hint,
                timeout=args.timeout,
            )
            result["bitstream"] = bit
            result["ltx"] = ltx
            _print(result)
        elif args.cmd == "ilas":
            part_hint = _require_part_hint(args.part_hint)
            _require_capability(
                backend,
                Capability.ILA_LIST,
                operation="ilas",
                target_hint=part_hint,
            )
            _print(backend.list_ilas(part_hint, timeout=args.timeout))
        elif args.cmd == "capture":
            part_hint = _require_part_hint(args.part_hint)
            _require_capability(
                backend,
                Capability.ILA_BASIC_CAPTURE,
                operation="capture",
                target_hint=part_hint,
            )
            _print(
                backend.capture(
                    part_hint,
                    args.ila,
                    args.csv,
                    _validate_samples(args.samples),
                    timeout=args.timeout,
                )
            )
        elif args.cmd == "instruments" and args.instruments_cmd == "list":
            part_hint = _require_part_hint(args.part_hint)
            _require_capability(
                backend,
                Capability.INSTRUMENTS_LIST,
                operation="instruments list",
                target_hint=part_hint,
            )
            _print(backend.list_instruments(part_hint, timeout=args.timeout))
        else:
            p.error(f"unknown command: {args.cmd}")
    except XdbError as e:
        _print_error(str(e))
        if args.debug:
            traceback.print_exc()
        sys.exit(2)
    except KeyboardInterrupt:
        _print_error("interrupted")
        if args.debug:
            traceback.print_exc()
        sys.exit(130)
    except Exception as e:
        if args.debug:
            traceback.print_exc()
        else:
            _print_error(
                f"unexpected internal error: {e}. Re-run with --debug for a traceback."
            )
        sys.exit(2)


if __name__ == "__main__":
    main()
