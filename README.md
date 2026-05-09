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
- Persistent XSim/Vivado simulation sessions for packaged runtime-backed flows
- Coyote-aware simulation commands for CSR access, host memory, invoke/completion, and IRQs

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

# launch a persistent simulation session from a packaged runtime bundle
# exported by an integration shell
export XDB_SIM_PACKAGE_RUNTIME=/nix/store/.../project/sim
export XDB_SIM_WORKSPACE=$PWD/.build/sim-workspace
export XDB_SIM_SIMSET=sim_1
export XDB_SIM_TOP=tb_top
export XDB_SIM_MODE=behavioral
export XDB_SIM_SESSION=myproj
xdb sim launch
xdb sim provenance

# refresh the staged workspace without launching a new simulator
xdb sim restage

# discard the staged workspace and relaunch a fresh simulator session
xdb sim relaunch --fresh

# query the same live session without relaunching Vivado
xdb sim time
xdb sim describe
xdb sim get /tb_top/dut/state
xdb sim read /tb_top/dut/state /tb_top/dut/done /tb_top/clk
xdb sim run 100 ns
xdb sim until '{[get_value /tb_top/done] eq "1"}'
xdb sim wait '{[get_value /tb_top/done] eq "1"}'
xdb sim until --step 1 ns '{[get_value /tb_top/done] eq "1"}'
xdb sim until --timeout 5 --max-iterations 1000 '{[get_value /tb_top/done] eq "1"}'
xdb sim until-signal /tb_top/done 1
xdb sim assert-signal /tb_top/resetn 1
xdb sim assert-tcl '{[get_value /tb_top/resetn] eq "1"}'
xdb sim expect-signal --within 1 us /tb_top/done 1
xdb sim expect-change --within 100 ns /tb_top/dut/state
xdb sim wait-on-signal /tb_top/done 1
xdb sim until-signal --step 100 ps --timeout 2 /tb_top/done 1
xdb sim scopes /tb_top
xdb sim objects /tb_top/dut
xdb sim snapshot /tb_top/dut --name before
xdb sim run 100 ns
xdb sim snapshot /tb_top/dut --name after
xdb sim diff-snapshot before after
xdb sim watch-changes /tb_top/dut --for 100 ns
xdb sim tcl current_time
xdb sim source ./sim/helpers.tcl
xdb sim force /tb_top/reset 1
xdb sim release /tb_top/reset
xdb sim wave add /tb_top/dut/*
xdb sim vcd start ./waves/fail.vcd /tb_top/dut
xdb sim vcd status
xdb sim vcd stop
xdb sim axis trace /tb_top/dut/axis_in --for 100 ns --decode-bytes
xdb sim trace transactions --for 200 ns
xdb sim with-trace --transactions --axis /tb_top/dut/axis_in --for 50 ns -- \
  xdb sim invoke local-transfer --src-addr 0x1000 --dst-addr 0x2000 --len 4

# Coyote-aware transactional commands (when the packaged simulation runtime exposes Coyote)
xdb sim coyote-status
xdb sim csr write 0x0 0x1234
xdb sim csr read 0x0
xdb sim mem write host 0x1000 --hex deadbeef
xdb sim mem list
xdb sim mem read host 0x1000 4
xdb sim invoke local-transfer --src-addr 0x1000 --dst-addr 0x2000 --len 4
xdb sim completed local-transfer --count 1 --timeout 5
xdb sim mem read host 0x2000 4
xdb sim irq wait --timeout 5

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
- `xdb sim` currently supports the packaged runtime-backed flow, not the direct
  project-backed `.xpr` flow.
- Direct project launch via `xdb sim launch --project ...` is not supported yet.
- `xdb sim launch` resolves missing flags from these environment variables when
  present: `XDB_SIM_PACKAGE_RUNTIME`, `XDB_SIM_WORKSPACE`, `XDB_SIM_SIMSET`,
  `XDB_SIM_MODE`, `XDB_SIM_TOP`, `XDB_SIM_SESSION`.
- In the runtime-backed flow, `xdb sim launch` stages the packaged simulation
  runtime into the writable workspace, runs the packaged compile/elaborate
  scripts there, and then starts a persistent `xsim` session.
- `xdb sim launch` starts a persistent background session; later
  `xdb sim ...` commands talk to that live simulator process.
- `xdb sim provenance` reports the current requested runtime inputs, staged
  workspace state, and any live session metadata so you can see whether the
  active session matches the current packaged runtime.
- `xdb sim restage` refreshes the writable workspace from the packaged runtime
  without launching a simulator. It refuses to run while a live simulation
  session exists for the same repo/session.
- `xdb sim relaunch --fresh` closes any live session for the current repo and
  session, discards the staged workspace, stages a fresh copy of the packaged
  runtime, and launches a new simulator process.
- `xdb sim until <tcl expr>` runs the simulator in repeated time steps
  (default `10 ns`) until the Tcl expression becomes true.
  Aliases: `xdb sim wait`, `xdb sim wait-on-condition`.
  Use `--timeout <seconds>` and/or `--max-iterations <count>` to bound the wait.
  Examples: `xdb sim until '{[get_value /tb_top/done] eq "1"}'`,
  `xdb sim until --step 1 ns '{[get_value /tb_top/done] eq "1"}'`,
  `xdb sim until --timeout 5 --max-iterations 1000 '{[get_value /tb_top/done] eq "1"}'`.
- `xdb sim until-signal <signal> <value>` is a convenience wrapper for waiting
  until a signal reaches an exact value, using the same stepped execution.
  Aliases: `xdb sim wait-signal`, `xdb sim wait-on-signal`.
  Use `--timeout <seconds>` and/or `--max-iterations <count>` to bound the wait.
  Example: `xdb sim until-signal --step 100 ps --timeout 2 /tb_top/done 1`.
- `xdb sim tcl ...` evaluates arbitrary Tcl in the live simulator session and
  returns the Tcl result string plus the current simulation time.
- `xdb sim source <file.tcl>` loads a Tcl file into the live simulator session
  with Tcl `source`, preserving file-based error locations and proc
  definitions.
- `xdb sim describe` summarizes the live session with the inferred top scope,
  likely DUT scope, known clocks, known resets, common scopes, time, and
  runtime metadata.
- `xdb sim assert-signal <path> <value>` and `xdb sim assert-tcl <expr>`
  provide immediate assertion-style checks with explicit pass/fail behavior.
- `xdb sim expect-signal --within <duration> <path> <value>` waits for a signal
  to reach an expected value within the given simulation time bound.
- `xdb sim expect-change --within <duration> <path>` waits for a signal to
  change from its current value within the given simulation time bound.
- `xdb sim get`, `xdb sim get-many`, `xdb sim read`, `xdb sim objects`, and
  `xdb sim scopes` now include richer machine-readable metadata such as
  `kind`, `width`, `parent_scope`, and `value` where applicable.
- `xdb sim snapshot <scope>` captures a structured subtree snapshot and stores
  it under a session-local snapshot name. Use `--name <id>` to choose the
  identifier explicitly.
- `xdb sim diff-snapshot <before> <after>` compares two stored snapshots and
  reports added, removed, and changed objects.
- `xdb sim watch-changes <scope> --for <duration>` captures a snapshot, runs the
  simulation for the given duration, captures another snapshot, and returns the
  diff directly.
- `xdb sim vcd start <file> [scope]` starts persistent VCD dumping. If `scope`
  is omitted, the whole design is logged recursively. Use
  `xdb sim vcd status` and `xdb sim vcd stop` to inspect or stop capture.
- `xdb sim axis trace <path...> --for <duration>` samples AXI Stream interface
  signals over time and records beats where `tvalid && tready`. Use `--step`
  to control sampling cadence, `--decode-bytes` to decode `tdata`/`tkeep` into
  lane-ordered bytes, `--include-idle` to keep non-handshake samples,
  `--ndjson` for one JSON object per traced record, and `--out <file>` to write
  the trace instead of printing it.
- `xdb sim force <signal> <value...>` wraps `add_force`; use `--radix`,
  `--repeat-every`, and `--cancel-after` for common options.
- `xdb sim release <signal>` releases forces created through `xdb sim force`.
  Use `xdb sim release --all` to clear all forces from the simulator.
- `xdb sim csr ...`, `xdb sim mem ...`, `xdb sim invoke ...`,
  `xdb sim completed ...`, `xdb sim clear-completed`, `xdb sim irq wait`, and
  `xdb sim coyote-status` wrap the Coyote interactive simulation protocol when
  the runtime bundle contains `lynx_pkg.sv`.
- `xdb sim mem list` reports the current host-memory mappings and local Coyote
  accounting state.
- `xdb sim mem reset` unmaps every host-memory segment tracked by `xdb` and
  clears the local host read/write counters and last protocol error string.
  It does not clear pending IRQ events or completion counters.
- `xdb sim trace transactions --for <duration>` captures protocol-level Coyote
  activity observed during that simulation window, including host reads/writes,
  IRQs, completion checks/results, and `xdb`-issued invoke or memory commands.
  Use `--opcode <name>` to keep only events associated with a specific local
  Coyote opcode and `--out <file>` to write the JSON result.
- `xdb sim with-trace -- ...` currently supports a wrapped subset of `xdb sim`
  subcommands and executes them under daemon-side tracing so AXIS and
  transaction traces cover the same command-and-observation window. Use
  `--transactions`, repeat `--axis <path>`, and `--for <duration>` to choose
  the collected artifacts. When both modes are enabled, the result includes a
  `correlation` section with an ordered transaction/AXIS timeline and nearest
  transaction-to-AXIS links. Use `--correlate-window <duration>` to discard
  links outside a simulator-time window, and `--correlate-by nearest|opcode|addr`
  to focus links on all transaction events, opcode-bearing events, or
  address-bearing events. Output defaults to pretty JSON; use `--ndjson` for one
  event/record per line, `--summary` for a compact human-readable report, and
  `--out <file>` to write the chosen format.
- Current Coyote support is intentionally limited to the local host-memory
  protocol implemented by the upstream Coyote simulation target. Remote RDMA and
  TCP commands are not supported yet.
- CSR addresses are byte addresses in the simulation protocol.
- `xdb sim mem write host ...` accepts `--hex`, `--text`, or `--file`.
- Output is intentionally minimal and script-friendly.
