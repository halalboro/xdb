from __future__ import annotations

import argparse
import json
import os
import sys

from . import __version__
from .backend.base import Capability
from .backend.select import select_backend
from .errors import UnsupportedOperationError, XdbError
from .sim.client import (
    add_breakpoint,
    add_wave,
    clear_breakpoints,
    close_session,
    get_many_signals,
    get_objects,
    get_scopes,
    get_signal,
    launch_session,
    restart_session,
    run_session,
    set_top,
    step_session,
    time_session,
)
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


def main() -> None:
    p = argparse.ArgumentParser(prog="xdb", description="Generic FPGA ILA debug toolkit")
    p.add_argument("--version", action="version", version=f"xdb {__version__}")

    sub = p.add_subparsers(dest="cmd", required=True)

    s_targets = sub.add_parser("targets")
    s_targets.add_argument("--part-hint", "--fpga-part-hint", dest="part_hint", default=None)
    s_targets.add_argument("--fdev-name", default=os.environ.get("FDEV_NAME"))
    s_targets.add_argument("--fpga-bdf", default=os.environ.get("FPGA_BDF"))
    s_targets.add_argument("--timeout", type=int, default=120)

    s_program = sub.add_parser("program")
    s_program.add_argument("--bit", default=None)
    s_program.add_argument("--ltx", default=None)
    s_program.add_argument("--verbose", action="store_true")
    s_program.add_argument("--part-hint", "--fpga-part-hint", dest="part_hint", default=None)
    s_program.add_argument("--fdev-name", default=os.environ.get("FDEV_NAME"))
    s_program.add_argument("--fpga-bdf", default=os.environ.get("FPGA_BDF"))
    s_program.add_argument("--timeout", type=int, default=300)

    s_ilas = sub.add_parser("ilas")
    s_ilas.add_argument("--part-hint", "--fpga-part-hint", dest="part_hint", default=None)
    s_ilas.add_argument("--fdev-name", default=os.environ.get("FDEV_NAME"))
    s_ilas.add_argument("--fpga-bdf", default=os.environ.get("FPGA_BDF"))
    s_ilas.add_argument("--timeout", type=int, default=180)

    s_capture = sub.add_parser("capture")
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
    s_instruments_list.add_argument("--part-hint", "--fpga-part-hint", dest="part_hint", default=None)
    s_instruments_list.add_argument("--timeout", type=int, default=180)

    s_sim = sub.add_parser("sim", description="Persistent Vivado simulation session control")
    sim_sub = s_sim.add_subparsers(dest="sim_cmd", required=True)

    def add_sim_session_arg(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--session", default=None)

    s_sim_launch = sim_sub.add_parser("launch")
    add_sim_session_arg(s_sim_launch)
    s_sim_launch.add_argument("--project", default=None)
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
    add_sim_session_arg(s_sim_run)
    s_sim_run.add_argument("time", nargs="*")

    s_sim_restart = sim_sub.add_parser("restart")
    add_sim_session_arg(s_sim_restart)

    s_sim_close = sim_sub.add_parser("close")
    add_sim_session_arg(s_sim_close)

    s_sim_time = sim_sub.add_parser("time")
    add_sim_session_arg(s_sim_time)

    s_sim_get = sim_sub.add_parser("get")
    add_sim_session_arg(s_sim_get)
    s_sim_get.add_argument("signal")

    s_sim_get_many = sim_sub.add_parser("get-many")
    add_sim_session_arg(s_sim_get_many)
    s_sim_get_many.add_argument("pattern")

    s_sim_scopes = sim_sub.add_parser("scopes")
    add_sim_session_arg(s_sim_scopes)
    s_sim_scopes.add_argument("scope", nargs="?", default=None)

    s_sim_objects = sim_sub.add_parser("objects")
    add_sim_session_arg(s_sim_objects)
    s_sim_objects.add_argument("scope")

    s_sim_top = sim_sub.add_parser("top")
    add_sim_session_arg(s_sim_top)
    s_sim_top.add_argument("module")

    s_sim_wave = sim_sub.add_parser("wave")
    sim_wave_sub = s_sim_wave.add_subparsers(dest="sim_wave_cmd", required=True)
    s_sim_wave_add = sim_wave_sub.add_parser("add")
    add_sim_session_arg(s_sim_wave_add)
    s_sim_wave_add.add_argument("pattern")

    s_sim_step = sim_sub.add_parser("step")
    add_sim_session_arg(s_sim_step)
    s_sim_step.add_argument("arg", nargs="*", default=[])

    s_sim_breakpoint = sim_sub.add_parser("breakpoint")
    sim_bp_sub = s_sim_breakpoint.add_subparsers(dest="sim_bp_cmd", required=True)
    s_sim_breakpoint_add = sim_bp_sub.add_parser("add")
    add_sim_session_arg(s_sim_breakpoint_add)
    s_sim_breakpoint_add.add_argument("condition", nargs="+")
    s_sim_breakpoint_clear = sim_bp_sub.add_parser("clear")
    add_sim_session_arg(s_sim_breakpoint_clear)

    s_simd = sub.add_parser("_simd", help=argparse.SUPPRESS)
    s_simd.add_argument("--anchor-dir", required=True)
    s_simd.add_argument("--session", default=None)
    s_simd.add_argument("--project", required=True)
    s_simd.add_argument("--simset", required=True)
    s_simd.add_argument("--mode", required=True)
    s_simd.add_argument("--top", default="")

    args = p.parse_args()

    try:
        if args.cmd == "_simd":
            run_daemon(
                anchor_dir=args.anchor_dir,
                session_name=args.session,
                project=args.project,
                simset=args.simset,
                mode=args.mode,
                top=args.top,
            )
            return

        if args.cmd == "sim":
            if args.sim_cmd == "launch":
                _print(
                    launch_session(
                        project=args.project,
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
            elif args.sim_cmd == "get":
                _print(get_signal(args.session, args.signal))
            elif args.sim_cmd == "get-many":
                _print(get_many_signals(args.session, args.pattern))
            elif args.sim_cmd == "scopes":
                _print(get_scopes(args.session, args.scope))
            elif args.sim_cmd == "objects":
                _print(get_objects(args.session, args.scope))
            elif args.sim_cmd == "top":
                _print(set_top(args.session, args.module))
            elif args.sim_cmd == "wave" and args.sim_wave_cmd == "add":
                _print(add_wave(args.session, args.pattern))
            elif args.sim_cmd == "step":
                _print(step_session(args.session, _join_tokens(args.arg)))
            elif args.sim_cmd == "breakpoint" and args.sim_bp_cmd == "add":
                _print(add_breakpoint(args.session, _join_tokens(args.condition) or ""))
            elif args.sim_cmd == "breakpoint" and args.sim_bp_cmd == "clear":
                _print(clear_breakpoints(args.session))
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
        print(str(e), file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
