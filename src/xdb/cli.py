from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from .vivado import VivadoError, capture, list_ilas, list_targets, program


def _print(data: dict) -> None:
    print(json.dumps(data, indent=2, sort_keys=False))


def main() -> None:
    p = argparse.ArgumentParser(prog="xdb", description="Generic FPGA ILA debug toolkit")
    p.add_argument("--version", action="version", version=f"xdb {__version__}")

    sub = p.add_subparsers(dest="cmd", required=True)

    s_targets = sub.add_parser("targets")
    s_targets.add_argument("--part-hint", default=None)
    s_targets.add_argument("--timeout", type=int, default=120)

    s_program = sub.add_parser("program")
    s_program.add_argument("--bit", required=True)
    s_program.add_argument("--ltx", default=None)
    s_program.add_argument("--part-hint", required=True)
    s_program.add_argument("--timeout", type=int, default=300)

    s_ilas = sub.add_parser("ilas")
    s_ilas.add_argument("--part-hint", required=True)
    s_ilas.add_argument("--timeout", type=int, default=180)

    s_capture = sub.add_parser("capture")
    s_capture.add_argument("--part-hint", required=True)
    s_capture.add_argument("--ila", required=True)
    s_capture.add_argument("--csv", required=True)
    s_capture.add_argument("--samples", type=int, default=2048)
    s_capture.add_argument("--timeout", type=int, default=120)

    args = p.parse_args()

    try:
        if args.cmd == "targets":
            _print(list_targets(args.part_hint, timeout=args.timeout))
        elif args.cmd == "program":
            _print(program(args.bit, args.ltx, args.part_hint, timeout=args.timeout))
        elif args.cmd == "ilas":
            _print(list_ilas(args.part_hint, timeout=args.timeout))
        elif args.cmd == "capture":
            _print(capture(args.part_hint, args.ila, args.csv, args.samples, timeout=args.timeout))
        else:
            p.error(f"unknown command: {args.cmd}")
    except VivadoError as e:
        print(str(e), file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
