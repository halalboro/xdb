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
- Headless, publication-ready SVG floorplans from routed Vivado checkpoints
- Finite, packaged Vitis HLS C-simulation orchestration with provenance and bundles

## Requirements

- Python 3.10+
- `vivado` available in `PATH` for FPGA/RTL operations
- `vitis_hls` available in `PATH` for `xdb hls` operations, with the exact version declared by the runtime package

Both tools should be provided by the consuming project's pinned Xilinx/Nix shell.

## Install (editable)

```bash
cd /Users/taugoust/Research/fpgas/xdb
python -m pip install -e .
```

## Development

Run the reproducible Treefmt wrapper through the flake:

```bash
nix fmt
```

It formats Python with `ruff-format` and Nix with `nixfmt-rfc-style`.
The same formatting policy is included in `nix flake check`.

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

# program board (uses FPGA_BITSTREAM; FPGA_LTX is optional)
xdb program

# list ILAs (uses FPGA_LTX by default when set; override with --ltx)
xdb ilas

# capture (uses FPGA_LTX by default when set; override with --ltx)
xdb capture \
  --ila hw_ila_1 \
  --csv ./ila.csv \
  --samples 2048

# ChipScoPy: decouple arm, status/wait, and waveform upload
xdb ila arm --ila hw_ila_1 --samples 256 --windows 4 --trigger-position 32
xdb ila status --ila hw_ila_1
xdb ila wait --ila hw_ila_1 --timeout 120
xdb ila upload --ila hw_ila_1 --csv ./ila.csv

# ChipScoPy: the same setup as one blocking capture
xdb capture \
  --ila hw_ila_1 \
  --csv ./ila.csv \
  --samples 256 \
  --windows 4 \
  --trigger-position 32 \
  --trigger state '==' 3 \
  --trigger valid '==' 1

# launch a persistent simulation session from a packaged runtime bundle
# exported by an integration shell
export XDB_SIM_WORKSPACE=$PWD/.build/sim-workspace
export XDB_SIM_SIMSET=sim_1
export XDB_SIM_TOP=tb_top
export XDB_SIM_MODE=behavioral
export XDB_SIM_SESSION=myproj
xdb sim launch /nix/store/...-my-sim-package
xdb sim provenance --summary
xdb sim doctor --summary

# refresh the staged workspace without launching a new simulator
xdb sim restage

# discard the staged workspace and relaunch a fresh simulator session
xdb sim relaunch --fresh

# query the same live session without relaunching Vivado
xdb sim time
xdb sim status
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
xdb sim expect-condition --within 1 us '{[get_value /tb_top/done] eq "1" && [get_value /tb_top/error] eq "0"}'
xdb sim expect-stream-output --within 1 us /tb_top/dut/axis_out
xdb sim breakpoint add --poll-step "1 ns" '{[get_value /tb_top/done] eq "1"}'
xdb sim breakpoint list
xdb sim breakpoint remove 1
xdb sim breakpoint clear
xdb sim wait-on-signal /tb_top/done 1
xdb sim until-signal --step 100 ps --timeout 2 /tb_top/done 1
xdb sim scopes /tb_top
xdb sim objects /tb_top/dut
xdb sim snapshot /tb_top/dut --name before
xdb sim run 100 ns
xdb sim run --timeout 30 500 ns
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
xdb sim trace profiles
xdb sim axis trace --profile host-rx --for 100 ns
xdb sim trace transactions --for 200 ns
xdb sim with-trace --transactions --axis /tb_top/dut/axis_in --for 50 ns -- \
  xdb sim invoke local-transfer --src-addr 0x1000 --dst-addr 0x2000 --len 4
xdb sim with-trace --profile smoke -- \
  xdb sim invoke local-transfer --src-addr 0x1000 --dst-addr 0x2000 --len 4
xdb sim exec --stream -- ./host-test --input 01020304
xdb sim with-trace --transactions --axis /tb_top/dut/axis_in --for 50 ns --exec -- \
  ./host-test --input 01020304
xdb sim with-trace --bundle fail-001 --transactions --axis /tb_top/dut/axis_in --for 50 ns --exec -- \
  ./host-test --input 01020304
xdb sim bundle --out current-session
xdb sim with-trace --transactions --axis /tb_top/dut/axis_in --exec-until-exit --exec -- \
  ./host-test --input 01020304

# Coyote-aware transactional commands (when the packaged simulation runtime exposes Coyote)
xdb sim coyote-status
xdb sim csr write 0x0 0x1234
xdb sim csr read 0x0
xdb sim service-csr write 0x100 0x1234
xdb sim service-csr read 0x100 --timeout 5
xdb sim mem write host 0x1000 --hex deadbeef
xdb sim mem list
xdb sim mem read host 0x1000 4
xdb sim mem dump host 0x1000 --size 4096 --out before.bin
xdb sim mem diff before.bin after.bin
xdb sim invoke local-transfer --src-addr 0x1000 --dst-addr 0x2000 --len 4
xdb sim completed local-transfer --count 1 --timeout 5
xdb sim mem read host 0x2000 4
xdb sim irq wait --timeout 5

# close the session when done
xdb sim close
# force cleanup if the daemon is unresponsive
xdb sim close --force
```

## HLS C simulation

HLS C simulation is a finite packaged process and is intentionally separate
from persistent RTL `xdb sim`:

```bash
# stage the immutable package and run its default case
xdb hls sim result-hls-csim --summary

# select one named case or run every case in deterministic order
xdb hls sim result-hls-csim --case empty
xdb hls sim result-hls-csim --all --continue-on-failure

# inspect reproducibility and health evidence
xdb hls provenance result-hls-csim --summary
xdb hls doctor result-hls-csim --summary

# export bounded logs, results, provenance, and declared artifacts
xdb hls bundle result-hls-csim --out failure-001
```

The consuming project supplies an immutable `xdb-hls-csim.json`, package-local
prepare/run entry points, fixtures, exact tool version, flags, permitted
environment, and source/configuration hashes. XDB validates the contract,
stages it into a writable fingerprinted workspace, verifies the observed tool
version, enforces per-process timeouts with process-group cleanup, and retains
normalized results plus raw logs. See
[`docs/hls-csim-runtime-v1.md`](docs/hls-csim-runtime-v1.md) for the complete
version-1 schema and package layout. This command does not run HLS synthesis,
RTL export/co-simulation, Vivado implementation, deployment, or hardware.

## Build reports

```bash
# routed top-level Coyote design utilization
xdb reports utilization result --report shell

# synthesized vFPGA/user design utilization
xdb reports utilization result --report user

# explicit report file also works
xdb reports utilization result/reports/shell_utilization.rpt

# for build/package directories, --report can also select the same relative
# report path under each directory
xdb reports utilization result --report reports/config_0/user_synthed_c0_0.rpt

# compare builds with deltas; first path is the baseline
xdb reports compare results/fpga-builds/helios-d13-v80 results/fpga-builds/helios-d17-v80
xdb reports compare d13 d15 d17 --report user --old-name d13 --new-name d15 --new-name d17

# inspect Versal CIPS connectivity and boot-image partitions
xdb reports cips result
xdb reports cips result/bitstreams/cyt_top.bif
xdb reports cips result --dcp checkpoints/shell_routed.dcp --json

# render the routed device resources and placement as deterministic SVG
xdb reports floorplan result --out figures/floorplan.svg

# use deeper hierarchy names for finer-grained placement colors
xdb reports floorplan result \
  --dcp checkpoints/shell_routed.dcp \
  --hierarchy-depth 2 \
  --out figures/floorplan.svg \
  --force
```

`xdb reports floorplan` opens a routed DCP in Vivado batch mode and asks Vivado
for its physical site grid, placed primitive locations, hierarchy, and pblocks.
XDB then renders a deterministic SVG itself; it does not require the Vivado GUI
or a display server. Available CLB, BRAM, URAM, DSP, I/O, transceiver, clocking,
and hard-IP sites form the muted device background. Occupied sites are colored
by hierarchy and listed in a legend. Pblocks are shown as dashed outlines when
their site ranges can be resolved; pass `--no-pblocks` to omit them. Use
`--hierarchy-depth` to control how many leading instance-name components define
a color group. To prevent accidentally producing an enormous, unreadable
legend, rendering stops above 32 groups by default; use `--max-groups` to raise
that explicit limit. The input must be a routed checkpoint; XDB rejects a
checkpoint when Vivado reports routing errors.

`xdb reports utilization` prints compact one-or-many report summaries. `xdb
reports compare` compares one baseline against one or more new reports and
includes absolute deltas, relative deltas, and utilization percentage-point
deltas.

`xdb reports cips` accepts a Vivado DCP, a Versal BIF, or a build/package
directory containing those artifacts. DCP inspection launches Vivado in batch
mode without connecting to hardware and reports retained CIPS properties and
implemented pin connectivity. BIF inspection is tool-free and distinguishes
A72/R5 application partitions from PLM/PSM management firmware. A
`not_observed` result means the selected artifacts contain no evidence; it does
not prove that the silicon or card lacks the feature.

`--report shell` resolves to `reports/shell_utilization.rpt`. `--report user`
resolves to `reports/config_0/user_synthed_c0_0.rpt`. Use `--report` only when
the positional path is a build/package directory; if the positional path is
already a report file, omit `--report`.

## Notes

- `FPGA_PART_HINT` is used by default to select the hardware target.
  Matching is done against `PART=` from `get_property NAME`.
- `FPGA_BITSTREAM` is used by default for `xdb program`; programming does not require an LTX file. Program results include the selected backend, target, part, server context, and SHA-256 identity of the programmed artifact.
- `FPGA_LTX` is optional for programming and is used for ILA discovery/capture when supplied. You can override with `--part-hint`/`--fpga-part-hint`, `--bit`, and `--ltx`.
- The ChipScoPy backend reads `HW_SERVER_URL` and `CS_SERVER_URL`, selects Versal devices by part, and uses `FPGA_JTAG_TARGET` to disambiguate. A part matching multiple devices without an explicit target is rejected rather than selecting the first device.
- `xdb ilas` and `xdb capture` apply the selected LTX before discovering debug cores.
- `FDEV_NAME` and `FPGA_BDF` are accepted as optional context flags.
- `xdb ila arm`, `status`, `wait`, and `upload` expose a decoupled ChipScoPy capture lifecycle. Each finite command currently reconnects and rediscovers the selected ILA; hardware capture state remains in the core between commands. The blocking `xdb capture` convenience command remains available.
- Captures use a wall-clock timeout. ChipScoPy supports bounded multi-window capture, an explicit per-window trigger position, and repeated basic probe comparisons combined with AND. Supported comparison operators are `==`, `!=`, `>`, `<`, `>=`, `<=`, and `||`; decimal values are passed as integers while hexadecimal and bit-pattern values are preserved as strings. Backends advertise these capabilities, and XDB rejects unsupported Vivado-backend options before connecting to hardware.
- `xdb sim` currently supports the packaged runtime-backed flow, not the direct
  project-backed `.xpr` flow.
- Direct project launch via `xdb sim launch --project ...` is not supported yet.
- `xdb sim launch [package-runtime]` accepts a packaged simulation output,
  runtime directory, or `xdb-runtime.json` as an optional positional argument.
  Package output roots are expected to contain `project/sim/xdb-runtime.json`.
  If omitted, it falls back to `XDB_SIM_PACKAGE_RUNTIME`.
- `xdb sim launch` resolves missing flags from these environment variables when
  present: `XDB_SIM_PACKAGE_RUNTIME`, `XDB_SIM_WORKSPACE`, `XDB_SIM_SIMSET`,
  `XDB_SIM_MODE`, `XDB_SIM_TOP`, `XDB_SIM_SESSION`.
- `xdb --config <path>` or `XDB_CONFIG_FILE=<path>` loads a project-selected
  TOML config file. Paths inside the config are resolved relative to the config
  file's directory. Project inputs are not discovered under `XDB_ROOT` by
  default.
- `XDB_ROOT` controls project-local `xdb` outputs and defaults to
  `<repo>/.xdb`. Simulation session metadata and inspectable daemon/Vivado logs
  live under `XDB_ROOT/sessions/<session-id>/`.
- `XDB_CACHE_ROOT` controls machine-local ephemeral IPC paths and defaults to
  `${XDG_CACHE_HOME}/xdb` or `~/.cache/xdb`. Simulation control sockets live
  under `XDB_CACHE_ROOT/sockets/` to avoid long Unix socket paths in deep repos.
- In the runtime-backed flow, `xdb sim launch` stages the packaged simulation
  runtime into the writable workspace, runs the packaged compile/elaborate
  scripts there, and then starts a persistent `xsim` session through the
  packaged simulate script. Environment setup performed by that script is
  retained; only its `-tclbatch <script>` arguments are omitted so the session
  remains interactive.
- `xdb sim launch` starts a persistent background session; later
  `xdb sim ...` commands talk to that live simulator process.
- `xdb sim close` asks the daemon to shut down and has a wall-clock response
  timeout, defaulting to 5 seconds. Use `xdb sim close --force` to terminate
  the cached daemon process group and remove stale session state when the daemon
  is unresponsive. Normal close preserves project-local session logs for later
  inspection; force close removes the session directory.
- `xdb sim run <duration>` has a wall-clock daemon response timeout, defaulting
  to 30 seconds. Use `--timeout <seconds>` for slow simulations. If it times
  out, the daemon may still be busy inside Vivado; check responsiveness with
  `xdb sim time` or recover with `xdb sim close` / `xdb sim relaunch --fresh`.
- `xdb sim provenance` reports the current requested runtime inputs, staged
  workspace state, and any live session metadata so you can see whether the
  active session matches the current packaged runtime. Use `--summary` for
  compact human-readable output.
- `xdb sim doctor` diagnoses simulation session health without requiring a
  responsive daemon. It checks project-local metadata/logs, daemon PID/socket
  state, daemon responsiveness, runtime/workspace freshness, and daemon/Vivado
  log availability, then returns suggested recovery commands. Use `--summary`
  for compact human-readable output.
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
- `xdb sim status` reports the current live simulation daemon status and time.
- `xdb sim describe` summarizes the live session with the inferred top scope,
  likely DUT scope, known clocks, known resets, common scopes, time, and
  runtime metadata.
- `xdb sim assert-signal <path> <value>` and `xdb sim assert-tcl <expr>`
  provide immediate assertion-style checks with explicit pass/fail behavior.
- `xdb sim expect-signal --within <duration> <path> <value>` waits for a signal
  to reach an expected value within the given simulation time bound.
- `xdb sim expect-change --within <duration> <path>` waits for a signal to
  change from its current value within the given simulation time bound.
- `xdb sim expect-condition --within <duration> <tcl expr>` waits for a Tcl
  expression to become true within a simulation time bound.
- `xdb sim expect-stream-output --within <duration> <axis-path>` waits for at
  least one AXI Stream handshake on an interface. Use `--step`,
  `--decode-bytes`, and `--lane-order` like `xdb sim axis trace`.
- `xdb sim breakpoint add <tcl expr>` adds a simulator breakpoint. Vivado
  `when` is used when available; otherwise `xdb` falls back to polling during
  `xdb`-controlled `run`/`step` operations. Use `--poll-step "1 ns"` to choose
  the fallback polling interval. Use `xdb sim breakpoint list`,
  `xdb sim breakpoint remove <id>`, and `xdb sim breakpoint clear` to manage
  breakpoint lifecycle.
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
  the trace instead of printing it. Use `--profile <name>` with
  `--profile-file <path>` or `XDB_TRACE_PROFILE_FILE` to load defaults from a
  project-chosen trace profile file.
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
- `xdb sim mem dump <space> <addr> --size <bytes> --out <file>` reads live
  simulation memory and writes raw bytes to a binary file with JSON metadata on
  stdout. `xdb sim mem diff <before.bin> <after.bin>` compares two dump files
  and reports changed byte ranges.
- `xdb sim trace transactions --for <duration>` captures protocol-level Coyote
  activity observed during that simulation window, including host reads/writes,
  IRQs, completion checks/results, and `xdb`-issued invoke or memory commands.
  Use `--opcode <name>` to keep only events associated with a specific local
  Coyote opcode and `--out <file>` to write the JSON result.
- `xdb sim exec -- <command...>` runs a host-side command against the active
  simulation session. It injects session-aware environment variables including
  `XDB_SIM_SESSION`, `XDB_SIM_RUNTIME_ROOT`, `XDB_SIM_WORK_DIR`,
  `XDB_SIM_SOCKET`, and `COYOTE_SIM_DIR=<runtime_root>`, captures
  stdout/stderr/exit code, and returns structured JSON. Use `--cwd <dir>`,
  repeated `--env KEY=VALUE`, `--timeout <seconds>`, `--expect-exit-code <n>`,
  and `--clean-env` as needed. Use `--stream` to mirror host stdout/stderr live
  to the terminal on stderr while preserving final JSON on stdout.
- `xdb sim bundle --out <name-or-path>` exports a debug artifact bundle with
  `manifest.json`, `doctor.json`, `provenance.json`, `metadata.json`, and
  daemon/Vivado logs when present. Relative bundle paths are created under
  `XDB_ROOT/artifacts/bundles/`. `xdb sim with-trace --bundle [name] ...`
  additionally includes `trace.json` and host stdout/stderr for traced host
  executions.
- `xdb sim trace profiles` lists named trace profiles from an explicit
  `--profile-file`, `XDB_TRACE_PROFILE_FILE`, or `trace_profile_file` in the
  TOML config file. Profile files are project inputs and are not discovered
  under `XDB_ROOT` by default; projects choose where checked-in profile files
  live. Profile fields may include
  `transactions`, `axis`, `duration`, `step`, `decode_bytes`, `lane_order`,
  `include_idle`, `only_handshakes`, `correlate_by`, and `correlate_window`.
  CLI options extend or override profile defaults.
- `xdb sim with-trace -- ...` currently supports a wrapped subset of `xdb sim`
  subcommands, including Coyote operations plus `run`, `step`, `until`, and
  `until-signal`. `xdb sim with-trace --exec -- <command...>` runs an external
  host command while the daemon advances and samples the simulator. Both modes
  execute under daemon-side tracing so AXIS and transaction traces cover the
  same command-and-observation window. Use
  `--transactions`, repeat `--axis <path>`, and `--for <duration>` to choose
  the collected artifacts. With `--exec`, use `--exec-until-exit` to trace while
  advancing the simulator until the host command exits without requiring
  `--for`. When both modes are enabled, the result includes a
  `correlation` section with an ordered transaction/AXIS timeline and nearest
  transaction-to-AXIS links. Use `--correlate-window <duration>` to discard
  links outside a simulator-time window, and `--correlate-by nearest|opcode|addr`
  to focus links on all transaction events, opcode-bearing events, or
  address-bearing events. Output defaults to pretty JSON; use `--ndjson` for one
  event/record per line, `--summary` for a compact human-readable report, and
  `--out <file>` to write the chosen format. With `--exec`, the options
  `--cwd`, `--env KEY=VALUE`, `--timeout`, `--expect-exit-code`, and
  `--clean-env` control the wrapped host command. Use `--stream` with `--exec`
  to mirror host stdout/stderr live to the client on stderr while preserving the
  final trace JSON on stdout.
- Current Coyote data-movement support is intentionally limited to the local
  host-memory protocol implemented by the upstream Coyote simulation target.
  Remote RDMA and TCP commands are not supported yet.
- `xdb sim csr` addresses the vFPGA/application AXI-Lite interface.
  `xdb sim service-csr` separately addresses the registered resident dynamic
  service and requires a Coyote runtime with controlled external-service
  simulation support. The two spaces never alias.
- CSR addresses are byte addresses in the simulation protocol. Resident-service
  addresses are relative to its rebased `0x000`–`0xfff` page, must be 8-byte
  aligned, and must not include the production host BAR offset `0x1000`.
- `xdb sim mem write host ...` accepts `--hex`, `--text`, or `--file`.
- Output is intentionally minimal and script-friendly.
