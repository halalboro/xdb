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
# shared env (same names as helios-coyote)
export FDEV_NAME=u280
export FPGA_BDF=0000:c1:00.0
export FPGA_PART_HINT=xcu280

# show targets
xdb targets

# program board
xdb program --bit /path/to/cyt_top.bit --ltx /path/to/cyt_top.ltx

# list ILAs
xdb ilas

# capture
xdb capture \
  --ila hw_ila_1 \
  --csv ./ila.csv \
  --samples 2048
```

## Notes

- `FPGA_PART_HINT` is used by default to select the hardware target by matching `PART=` from `get_property NAME`.
- You can override with `--part-hint` (or `--fpga-part-hint`).
- `FDEV_NAME` and `FPGA_BDF` are accepted for CLI consistency with helios-coyote.
- Captures are one-shot and blocking (with timeout).
- Output is intentionally minimal and script-friendly.
