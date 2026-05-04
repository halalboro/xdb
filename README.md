# xdb

Generic FPGA debug toolkit with a minimal ILA workflow.

This project is independent and is not affiliated with, endorsed by, or sponsored by Xilinx (AMD).

Current focus: **U280 via Vivado Tcl backend**.

## Scope

This repo provides a standalone CLI so ILA debug automation is not tied to any one project/flake.

## Features (MVP)

- List hardware targets
- Program bitstream + probes (`.ltx`)
- List ILAs and probe metadata
- Capture from an ILA and export CSV

## Requirements

- Python 3.10+
- `vivado` available in `PATH` (typically through your cluster `xilinx-shell`)

## Install (editable)

```bash
cd /Users/taugoust/Research/fpgas/xdb
python -m pip install -e .
```

## Usage

```bash
# show targets
xdb targets --part-hint xcu280

# program board
xdb program --bit /path/to/cyt_top.bit --ltx /path/to/cyt_top.ltx --part-hint xcu280

# list ILAs
xdb ilas --part-hint xcu280

# capture
xdb capture \
  --ila hw_ila_1 \
  --csv ./ila.csv \
  --samples 2048 \
  --part-hint xcu280
```

## Notes

- `--part-hint` is used to select the hardware target by matching `PART=` from `get_property NAME`.
- Captures are one-shot and blocking (with timeout).
- Output is intentionally minimal and script-friendly.
