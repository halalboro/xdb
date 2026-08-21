# Packaged HLS C-simulation runtime schema

`xdb hls` uses a finite-process runtime that is deliberately separate from the
persistent RTL simulator controlled by `xdb sim`. The consuming project owns
source selection, fixtures, the HLS top, tool version, and the package-local
prepare/run launchers. XDB owns validation, writable staging, bounded execution,
results, provenance, diagnostics, and failure bundles.

The version-1 manifest is named `xdb-hls-csim.json`. It may be at the runtime
root or at `project/hls/xdb-hls-csim.json` in a package output. Unknown fields
are rejected so a producer cannot silently depend on unsupported behavior. A
machine-readable companion schema is available at
[`schemas/xdb-hls-csim-v1.schema.json`](../schemas/xdb-hls-csim-v1.schema.json).

## Example package

```text
runtime/
├── xdb-hls-csim.json
├── bin/
│   ├── prepare
│   └── run
└── fixtures/
    └── smoke.tsv
```

```json
{
  "schema_version": 1,
  "runtime_kind": "hls-csim",
  "project": "decoder-d3",
  "top": "decoderTop",
  "prepare": {
    "path": "bin/prepare",
    "args": []
  },
  "run": {
    "path": "bin/run",
    "args": []
  },
  "cases": [
    {
      "name": "smoke",
      "args": ["--fixture", "fixtures/smoke.tsv"],
      "fixtures": ["fixtures/smoke.tsv"]
    }
  ],
  "default_case": "smoke",
  "default_timeout_seconds": 300,
  "expected_exit_code": 0,
  "success_marker": "DECODER_CSIM_PASS",
  "tool": {
    "family": "vitis_hls",
    "version": "2023.2",
    "executable": "vitis_hls",
    "version_args": ["-version"],
    "version_regex": "v([0-9]+\\.[0-9]+)"
  },
  "environment": {
    "pass": ["PATH", "LD_LIBRARY_PATH", "XILINX_VITIS", "XILINX_HLS"],
    "set": {
      "XILINX_LOCAL_USER_DATA": "no"
    }
  },
  "provenance": {
    "source_revision": "0123456789abcdef0123456789abcdef01234567",
    "source_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "configuration_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
  },
  "compile_flags": ["-DCODE_DISTANCE=3"],
  "artifacts": [
    {
      "path": "project/solution/csim/report/decoderTop_csim.log",
      "required": false,
      "max_bytes": 4194304
    }
  ]
}
```

## Contract

- `schema_version` must be `1` and `runtime_kind` must be `hls-csim`.
- `project`, case names, and the tool family are stable logical identifiers.
- `prepare.path` and `run.path` are executable package-local files. Preparation
  runs once per XDB invocation; the run entry point runs once per selected case.
  Entry-point arguments precede each case's `args`.
- `cases` is nonempty and case names are unique. `--all` sorts cases by name.
  The default policy is fail-fast; `--continue-on-failure` is explicit.
- A case may override `expected_exit_code` and `success_marker`. Otherwise the
  top-level values apply. A marker is checked in addition to the exit status.
- The per-process timeout is bounded to 1–86400 seconds. XDB terminates the
  complete process group on timeout or interruption.
- `tool.version_regex` contains exactly one capture group. The observed value
  must equal `tool.version` before preparation starts.
- The execution environment is clean. Only names in `environment.pass`, values
  in `environment.set`, and XDB-managed runtime variables are present. `PATH`
  must be explicitly passed or set. XDB supplies isolated writable `HOME` and
  `TMPDIR` directories in the staged workspace.
- Source and configuration hashes are lowercase SHA-256 values. The consuming
  package is responsible for computing them over its declared inputs.
- Declared artifacts are workspace-relative files. `max_bytes` bounds each file
  when copied into a bundle. Runtime-created symlinks may not escape the staged
  workspace.
- All manifest paths reject absolute paths and traversal. Package symlinks may
  not escape the runtime root. The package is never edited.

XDB injects these variables into prepare and run entry points:

```text
HOME
TMPDIR
XDB_HLS_PACKAGE_RUNTIME
XDB_HLS_WORKSPACE
XDB_HLS_PROJECT
XDB_HLS_TOP
XDB_HLS_CASE       # run entry point only
```

## Staging and results

The package is fingerprinted by sorted path, type, executable bit, symlink
target, and file content. XDB copies it into a deterministic writable workspace
and records `.xdb-hls-stage.json`. A package-content change makes the workspace
stale. `xdb hls sim --restage` forces replacement.

If `--workspace` and `XDB_HLS_WORKSPACE` are absent, the default is:

```text
<XDB_ROOT>/hls/workspaces/<project>-<package-fingerprint-prefix>
```

Normalized results and raw process output are retained under:

```text
<workspace>/.xdb-hls/runs/<run-id>/
<workspace>/.xdb-hls/last-result.json
```

The result records the manifest contract, package/staging identity, tool probe,
prepare process, ordered cases, commands, permitted environment, timestamps,
durations, exit/signal/timeout state, marker checks, artifacts, and log paths.

## Commands

```bash
xdb hls sim PACKAGE --case smoke --summary
xdb hls sim PACKAGE --all --continue-on-failure
xdb hls provenance PACKAGE --summary
xdb hls doctor PACKAGE --summary
xdb hls bundle PACKAGE --out failure-001
```

`PACKAGE` may be omitted when `XDB_HLS_PACKAGE_RUNTIME` or
`[hls].package_runtime` in the selected XDB TOML config supplies it. Workspace
selection follows `--workspace`, `XDB_HLS_WORKSPACE`, `[hls].workspace`, then
the deterministic default.

Bundles contain the runtime manifest, normalized result, provenance, doctor
output, process logs, declared bounded artifacts, XDB version, and invocation.
They do not recursively include source trees or generated HLS databases.

This schema does not authorize HLS synthesis, RTL export, HLS co-simulation,
Vivado synthesis/implementation, hardware access, or persistent RTL simulator
operations. A future `xdb hls cosim` is a separate capability.
