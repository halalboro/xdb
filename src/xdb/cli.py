from __future__ import annotations

import argparse
import json
import os
import sys

from . import __version__
from .vivado import VivadoError, capture, list_ilas, list_targets, program


def _print(data: dict) -> None:
    print(json.dumps(data, indent=2, sort_keys=False))


def _resolve_part_hint(cli_value: str | None) -> str | None:
    return cli_value or os.environ.get("FPGA_PART_HINT")


def _require_part_hint(cli_value: str | None) -> str:
    part_hint = _resolve_part_hint(cli_value)
    if not part_hint:
        raise VivadoError("missing FPGA part hint: pass --part-hint (or --fpga-part-hint) or set FPGA_PART_HINT")
    return part_hint


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
    s_program.add_argument("--bit", required=True)
    s_program.add_argument("--ltx", default=None)
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
        if args.cmd == "targets":
            _print(list_targets(_resolve_part_hint(args.part_hint), timeout=args.timeout))
        elif args.cmd == "program":
            _print(program(args.bit, args.ltx, _require_part_hint(args.part_hint), timeout=args.timeout))
        elif args.cmd == "ilas":
            _print(list_ilas(_require_part_hint(args.part_hint), timeout=args.timeout))
        elif args.cmd == "capture":
            _print(capture(_require_part_hint(args.part_hint), args.ila, args.csv, args.samples, timeout=args.timeout))
        else:
            p.error(f"unknown command: {args.cmd}")
    except VivadoError as e:
        print(str(e), file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
