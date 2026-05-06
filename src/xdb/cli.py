from __future__ import annotations

import argparse
import json
import os
import sys

from . import __version__
from .backend.select import select_backend
from .vivado import VivadoError


def _print(data: dict) -> None:
    print(json.dumps(data, indent=2, sort_keys=False))


def _resolve_part_hint(cli_value: str | None) -> str | None:
    return cli_value or os.environ.get("FPGA_PART_HINT")


def _require_part_hint(cli_value: str | None) -> str:
    part_hint = _resolve_part_hint(cli_value)
    if not part_hint:
        raise VivadoError(
            "missing FPGA part hint: pass --part-hint "
            "(or --fpga-part-hint) or set FPGA_PART_HINT"
        )
    return part_hint


def _resolve_bitstream(cli_value: str | None) -> str:
    bit = cli_value or os.environ.get("FPGA_BITSTREAM")
    if not bit:
        raise VivadoError("missing bitstream: pass --bit or set FPGA_BITSTREAM")
    if not os.path.isfile(bit):
        raise VivadoError(f"bitstream not found: {bit}")
    return bit


def _resolve_ltx(cli_value: str | None) -> str:
    ltx = cli_value or os.environ.get("FPGA_LTX")
    if not ltx:
        raise VivadoError("missing ltx: pass --ltx or set FPGA_LTX")
    if not os.path.isfile(ltx):
        raise VivadoError(f"ltx not found: {ltx}")
    return ltx


def _is_power_of_two(v: int) -> bool:
    return v > 0 and (v & (v - 1)) == 0


def _validate_samples(samples: int) -> int:
    if samples <= 0:
        raise VivadoError("--samples must be > 0")
    if not _is_power_of_two(samples):
        raise VivadoError("--samples must be a power of two (e.g., 128, 256, 512, 1024)")
    return samples


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

    args = p.parse_args()

    try:
        backend = select_backend()
        if args.cmd == "targets":
            _print(backend.list_targets(_resolve_part_hint(args.part_hint), timeout=args.timeout))
        elif args.cmd == "program":
            bit = _resolve_bitstream(args.bit)
            ltx = _resolve_ltx(args.ltx)
            result = backend.program(
                bit,
                ltx,
                _require_part_hint(args.part_hint),
                timeout=args.timeout,
            )
            result["bitstream"] = bit
            result["ltx"] = ltx
            _print(result)
        elif args.cmd == "ilas":
            _print(backend.list_ilas(_require_part_hint(args.part_hint), timeout=args.timeout))
        elif args.cmd == "capture":
            _print(
                backend.capture(
                    _require_part_hint(args.part_hint),
                    args.ila,
                    args.csv,
                    _validate_samples(args.samples),
                    timeout=args.timeout,
                )
            )
        else:
            p.error(f"unknown command: {args.cmd}")
    except VivadoError as e:
        print(str(e), file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
