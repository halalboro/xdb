from __future__ import annotations

import argparse
import json
import os
import sys

from . import __version__
from .backend.base import Capability
from .backend.select import select_backend
from .errors import UnsupportedOperationError, XdbError


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

    args = p.parse_args()

    try:
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
