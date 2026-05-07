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
- Persistent Vivado simulation sessions for project-backed flows

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
# optional env
export FDEV_NAME=u280
export FPGA_BDF=0000:c1:00.0
export FPGA_PART_HINT=xcu280
export FPGA_BITSTREAM=/path/to/cyt_top.bit
export FPGA_LTX=/path/to/cyt_top.ltx

# show targets
xdb targets

# program board (uses FPGA_BITSTREAM/FPGA_LTX by default)
xdb program

# list ILAs
xdb ilas

# capture
xdb capture \
  --ila hw_ila_1 \
  --csv ./ila.csv \
  --samples 2048

# launch a persistent simulation session from a Vivado project
xdb sim launch --project ./myproj.xpr --top tb_top

# or rely on environment defaults exported by an integration shell
export XDB_SIM_PACKAGE_PROJECT=/nix/store/.../project/sim/myproj.xpr
export XDB_SIM_WORKSPACE=$PWD/.build/sim-workspace
export XDB_SIM_PROJECT=$XDB_SIM_WORKSPACE/sim/myproj.xpr
export XDB_SIM_SIMSET=sim_1
export XDB_SIM_TOP=tb_top
export XDB_SIM_MODE=behavioral
export XDB_SIM_SESSION=myproj
xdb sim launch

# query the same live session without relaunching Vivado
xdb sim time
xdb sim get /tb_top/dut/state
xdb sim run 100 ns
xdb sim scopes /tb_top
xdb sim objects /tb_top/dut
xdb sim wave add /tb_top/dut/*

# close the session when done
xdb sim close
```

## Notes

- `FPGA_PART_HINT` is used by default to select the hardware target.
  Matching is done against `PART=` from `get_property NAME`.
- `FPGA_BITSTREAM` and `FPGA_LTX` are used by default for `xdb program`.
- You can override with `--part-hint`/`--fpga-part-hint`, `--bit`, and `--ltx`.
- `FDEV_NAME` and `FPGA_BDF` are accepted as optional context flags.
- Captures are one-shot and blocking (with timeout).
- `xdb sim` is project-backed in v1: launch from an existing `.xpr` and optional simset/top.
- `xdb sim launch` resolves missing flags from these environment variables when present: `XDB_SIM_PROJECT`, `XDB_SIM_PACKAGE_PROJECT`, `XDB_SIM_WORKSPACE`, `XDB_SIM_SIMSET`, `XDB_SIM_MODE`, `XDB_SIM_TOP`, `XDB_SIM_SESSION`.
- When `XDB_SIM_PACKAGE_PROJECT` and `XDB_SIM_WORKSPACE` are set, `xdb sim launch` materializes the packaged project into the writable workspace before opening `XDB_SIM_PROJECT`.
- `xdb sim launch` starts a persistent background session; later `xdb sim ...` commands talk to that live Vivado process.
- Output is intentionally minimal and script-friendly.
