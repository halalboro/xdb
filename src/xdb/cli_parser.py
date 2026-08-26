from __future__ import annotations

import argparse
import os

from xdb import __version__


def _add_debug_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--debug",
        "--verbose",
        dest="debug",
        action="store_true",
        default=argparse.SUPPRESS,
        help="print tracebacks and detailed backend/tool diagnostics on failure",
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="xdb", description="Generic FPGA ILA debug toolkit")
    p.add_argument("--version", action="version", version=f"xdb {__version__}")
    p.add_argument("--config", default=None, help="path to a project-selected xdb TOML config file")
    _add_debug_flag(p)

    visible_commands = [
        "targets",
        "program",
        "ilas",
        "capture",
        "reports",
        "util",
        "timing",
        "vivado",
        "instruments",
        "hls",
        "sim",
    ]
    sub = p.add_subparsers(
        dest="cmd",
        required=True,
        metavar="{" + ",".join(visible_commands) + "}",
    )

    s_targets = sub.add_parser("targets")
    _add_debug_flag(s_targets)
    s_targets.add_argument("--part-hint", "--fpga-part-hint", dest="part_hint", default=None)
    s_targets.add_argument("--fdev-name", default=os.environ.get("FDEV_NAME"))
    s_targets.add_argument("--fpga-bdf", default=os.environ.get("FPGA_BDF"))
    s_targets.add_argument("--timeout", type=int, default=120)

    s_program = sub.add_parser("program")
    _add_debug_flag(s_program)
    s_program.add_argument("--bit", default=None)
    s_program.add_argument("--ltx", default=None)
    s_program.add_argument("--part-hint", "--fpga-part-hint", dest="part_hint", default=None)
    s_program.add_argument("--fdev-name", default=os.environ.get("FDEV_NAME"))
    s_program.add_argument("--fpga-bdf", default=os.environ.get("FPGA_BDF"))
    s_program.add_argument("--timeout", type=int, default=300)

    s_ilas = sub.add_parser("ilas")
    _add_debug_flag(s_ilas)
    s_ilas.add_argument("--part-hint", "--fpga-part-hint", dest="part_hint", default=None)
    s_ilas.add_argument("--ltx", default=None)
    s_ilas.add_argument("--fdev-name", default=os.environ.get("FDEV_NAME"))
    s_ilas.add_argument("--fpga-bdf", default=os.environ.get("FPGA_BDF"))
    s_ilas.add_argument("--timeout", type=int, default=180)

    s_capture = sub.add_parser("capture")
    _add_debug_flag(s_capture)
    s_capture.add_argument("--part-hint", "--fpga-part-hint", dest="part_hint", default=None)
    s_capture.add_argument("--ltx", default=None)
    s_capture.add_argument("--fdev-name", default=os.environ.get("FDEV_NAME"))
    s_capture.add_argument("--fpga-bdf", default=os.environ.get("FPGA_BDF"))
    s_capture.add_argument("--ila", required=True)
    s_capture.add_argument("--csv", required=True)
    s_capture.add_argument("--samples", type=int, default=2048, help="samples per capture window")
    s_capture.add_argument("--windows", type=int, default=1, help="number of capture windows")
    s_capture.add_argument(
        "--trigger-position",
        type=int,
        default=None,
        help="trigger sample index in each window (default: middle)",
    )
    s_capture.add_argument(
        "--trigger",
        action="append",
        nargs=3,
        default=[],
        metavar=("PROBE", "OPERATOR", "VALUE"),
        help="basic probe comparison; repeat to combine comparisons with AND",
    )
    s_capture.add_argument("--timeout", type=int, default=120)

    def add_reports_utilization_args(sp: argparse.ArgumentParser) -> None:
        _add_debug_flag(sp)
        sp.add_argument("paths", nargs="+", help="report file or build/package output directory")
        sp.add_argument(
            "--report",
            default=None,
            help=reports_report_selection_help,
        )
        sp.add_argument("--json", action="store_true", help="emit machine-readable JSON")
        sp.add_argument("--csv", action="store_true", help="emit CSV rows")
        sp.add_argument("--all", action="store_true", help="include every parsed utilization row")
        sp.add_argument(
            "--resource",
            action="append",
            default=None,
            help="resource key to include; may be repeated",
        )
        sp.add_argument(
            "--name",
            action="append",
            default=None,
            help="build label for compact multi-report output; may be repeated",
        )

    reports_report_selection_help = (
        "when <path> is a directory, select report alias or relative report path"
    )
    reports_utilization_epilog = """\
Report selection when each positional path is a build/package directory:
  --report shell            routed top-level Coyote design utilization
                            (reports/shell_utilization.rpt)
  --report user             synthesized vFPGA/user design utilization
                            (reports/config_0/user_synthed_c0_0.rpt)
  --report <relative-path>  same report path under each build/package directory

If the positional path is already a report file, omit --report.

Examples:
  xdb reports utilization result --report shell
  xdb reports utilization result --report user
  xdb reports utilization result/reports/shell_utilization.rpt
"""

    s_reports = sub.add_parser("reports", help="inspect FPGA build reports")
    _add_debug_flag(s_reports)
    reports_sub = s_reports.add_subparsers(
        dest="reports_cmd",
        required=True,
        metavar="{utilization,compare,cips,floorplan}",
    )
    s_reports_utilization = reports_sub.add_parser(
        "utilization",
        aliases=["util"],
        help="summarize Vivado utilization reports",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=reports_utilization_epilog,
    )
    add_reports_utilization_args(s_reports_utilization)

    reports_compare_epilog = """\
Compares one baseline utilization report against one or more new reports. Paths
may be report files or build/package directories. Use --report to select the
same report under every directory.

Examples:
  xdb reports compare results/fpga-builds/helios-d13-v80 results/fpga-builds/helios-d17-v80
  xdb reports compare d13 d15 d17 --report user --old-name d13 --new-name d15 --new-name d17
"""
    s_reports_compare = reports_sub.add_parser(
        "compare",
        help="compare two Vivado utilization reports",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=reports_compare_epilog,
    )
    _add_debug_flag(s_reports_compare)
    s_reports_compare.add_argument(
        "old", help="baseline report file or build/package output directory"
    )
    s_reports_compare.add_argument(
        "new", nargs="+", help="new report file or build/package output directory"
    )
    s_reports_compare.add_argument("--report", default=None, help=reports_report_selection_help)
    s_reports_compare.add_argument("--old-name", default=None, help="baseline label")
    s_reports_compare.add_argument(
        "--new-name", action="append", default=None, help="new-build label; may be repeated"
    )
    s_reports_compare.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    s_reports_compare.add_argument("--csv", action="store_true", help="emit CSV rows")
    s_reports_compare.add_argument(
        "--resource",
        action="append",
        default=None,
        help="resource key to include; may be repeated",
    )

    reports_cips_epilog = """\
Inspect a Vivado DCP, a Versal BIF, or a build/package directory containing
checkpoints and bitstreams. DCP inspection launches Vivado in batch mode but
never connects to hardware. BIF-only inspection requires no Xilinx tools.

Examples:
  xdb reports cips result
  xdb reports cips result/bitstreams/cyt_top.bif
  xdb reports cips result --dcp checkpoints/shell_routed.dcp --json
"""
    s_reports_cips = reports_sub.add_parser(
        "cips",
        help="inspect Versal CIPS connectivity and boot partitions",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=reports_cips_epilog,
    )
    _add_debug_flag(s_reports_cips)
    s_reports_cips.add_argument("path", help="DCP, BIF, or build/package output directory")
    s_reports_cips.add_argument("--dcp", default=None, help="checkpoint path, relative to <path>")
    s_reports_cips.add_argument("--bif", default=None, help="BIF path, relative to <path>")
    s_reports_cips.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    s_reports_cips.add_argument(
        "--timeout", type=int, default=1800, help="Vivado timeout in seconds"
    )

    reports_floorplan_epilog = """\
Opens a routed checkpoint in Vivado batch mode, extracts physical site and
placement data, then writes a deterministic SVG without launching the GUI.
Occupied sites are colored by hierarchy; device resource types remain visible
in the background. Existing pblocks are drawn as dashed outlines by default.

Examples:
  xdb reports floorplan result --out figures/floorplan.svg
  xdb reports floorplan result --dcp checkpoints/shell_routed.dcp \\
    --hierarchy-depth 2 --out figures/floorplan.svg
"""
    s_reports_floorplan = reports_sub.add_parser(
        "floorplan",
        help="render routed FPGA placement as publication-ready SVG",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=reports_floorplan_epilog,
    )
    _add_debug_flag(s_reports_floorplan)
    s_reports_floorplan.add_argument(
        "path",
        help="routed DCP or build/package output directory",
    )
    s_reports_floorplan.add_argument(
        "--dcp",
        default=None,
        help="checkpoint path relative to <path> when <path> is a directory",
    )
    s_reports_floorplan.add_argument(
        "--out",
        required=True,
        help="output SVG path",
    )
    s_reports_floorplan.add_argument(
        "--hierarchy-depth",
        type=int,
        default=1,
        help="hierarchy components used for placement colors (default: 1)",
    )
    s_reports_floorplan.add_argument(
        "--max-groups",
        type=int,
        default=32,
        help="maximum hierarchy color groups before refusing to render (default: 32)",
    )
    s_reports_floorplan.add_argument(
        "--title",
        default=None,
        help="figure title (default: design name)",
    )
    s_reports_floorplan.add_argument(
        "--no-pblocks",
        dest="show_pblocks",
        action="store_false",
        help="omit pblock outlines",
    )
    s_reports_floorplan.set_defaults(show_pblocks=True)
    s_reports_floorplan.add_argument(
        "--force",
        action="store_true",
        help="replace an existing output file",
    )
    s_reports_floorplan.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable render metadata",
    )
    s_reports_floorplan.add_argument(
        "--timeout",
        type=int,
        default=1800,
        help="Vivado timeout in seconds",
    )

    s_util = sub.add_parser(
        "util",
        help="summarize Vivado utilization reports",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=reports_utilization_epilog,
    )
    add_reports_utilization_args(s_util)

    def add_timing_common_args(sp: argparse.ArgumentParser) -> None:
        _add_debug_flag(sp)
        sp.add_argument("path", nargs="?", help="build/package output directory or routed DCP")
        sp.add_argument("--dcp", default=None, help="routed DCP checkpoint")
        sp.add_argument("--reports", default=None, help="Vivado reports directory")
        sp.add_argument("--json", action="store_true", help="emit machine-readable JSON")
        sp.add_argument("--timeout", type=int, default=1800, help="Vivado timeout in seconds")

    s_timing = sub.add_parser("timing", help="inspect routed FPGA timing reports/checkpoints")
    _add_debug_flag(s_timing)
    timing_sub = s_timing.add_subparsers(dest="timing_cmd", required=True)

    s_timing_summary = timing_sub.add_parser("summary")
    add_timing_common_args(s_timing_summary)
    s_timing_summary.add_argument("--max-paths", type=int, default=10)

    s_timing_paths = timing_sub.add_parser("paths")
    add_timing_common_args(s_timing_paths)
    s_timing_paths.add_argument("--max-paths", type=int, default=20)
    s_timing_paths.add_argument("--delay-type", choices=["max", "min"], default="max")

    s_timing_clocks = timing_sub.add_parser("clocks")
    add_timing_common_args(s_timing_clocks)

    s_timing_drc = timing_sub.add_parser("drc")
    add_timing_common_args(s_timing_drc)

    s_timing_net = timing_sub.add_parser("net")
    add_timing_common_args(s_timing_net)
    s_timing_net.add_argument("--net", required=True, help="hierarchical net name or pattern")
    s_timing_net.add_argument("--log", default=None, help="Vivado log for related warning lookup")

    s_timing_triage = timing_sub.add_parser("triage")
    add_timing_common_args(s_timing_triage)
    s_timing_triage.add_argument(
        "--log", default=None, help="Vivado log for critical warning extraction"
    )
    s_timing_triage.add_argument("--max-paths", type=int, default=20)
    s_timing_triage.add_argument("--hierarchy-depth", type=int, default=4)

    s_timing_compare = timing_sub.add_parser("compare")
    _add_debug_flag(s_timing_compare)
    s_timing_compare.add_argument("--old", required=True, help="known-good build/report directory")
    s_timing_compare.add_argument("--new", required=True, help="new/bad build/report directory")
    s_timing_compare.add_argument("--old-name", default="old")
    s_timing_compare.add_argument("--new-name", default="new")
    s_timing_compare.add_argument("--hierarchy-depth", type=int, default=4)
    s_timing_compare.add_argument("--timeout", type=int, default=1800)
    s_timing_compare.add_argument("--json", action="store_true", help="emit machine-readable JSON")

    s_vivado = sub.add_parser("vivado", help="inspect Vivado logs and artifacts")
    _add_debug_flag(s_vivado)
    vivado_sub = s_vivado.add_subparsers(dest="vivado_cmd", required=True)

    s_vivado_summarize_log = vivado_sub.add_parser(
        "summarize-log",
        help="summarize Vivado diagnostics from a log file or stdin",
    )
    _add_debug_flag(s_vivado_summarize_log)
    s_vivado_summarize_log.add_argument("log", help="Vivado log file, or '-' to read stdin")
    s_vivado_summarize_log.add_argument(
        "--json", action="store_true", help="emit machine-readable JSON"
    )
    s_vivado_summarize_log.add_argument(
        "--max-items",
        type=int,
        default=10,
        help="maximum diagnostics to show per section in text output",
    )
    s_vivado_summarize_log.add_argument(
        "--full",
        "--no-compact",
        dest="full",
        action="store_true",
        help="show full diagnostic messages in text output; also enabled by --verbose",
    )

    s_vivado_ip_info = vivado_sub.add_parser(
        "ip-info",
        help="inspect Vivado XCI IP metadata",
    )
    _add_debug_flag(s_vivado_ip_info)
    s_vivado_ip_info.add_argument("path", help="XCI file or directory to search for .xci files")
    s_vivado_ip_info.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    s_vivado_ip_info.add_argument(
        "--all",
        action="store_true",
        help="include all parsed parameters instead of only likely user/key parameters",
    )
    s_vivado_ip_info.add_argument(
        "--param",
        action="append",
        default=None,
        help="parameter name glob/substring to include; may be repeated",
    )

    s_instruments = sub.add_parser("instruments")
    instruments_sub = s_instruments.add_subparsers(dest="instruments_cmd", required=True)
    s_instruments_list = instruments_sub.add_parser("list")
    _add_debug_flag(s_instruments_list)
    s_instruments_list.add_argument(
        "--part-hint", "--fpga-part-hint", dest="part_hint", default=None
    )
    s_instruments_list.add_argument("--timeout", type=int, default=180)

    hls_epilog = """\
Run finite, packaged Vitis HLS C simulations in a writable staged workspace.
This command family is separate from persistent RTL `xdb sim`; it provides no
RTL signals, Tcl state, simulator time, synthesis, or co-simulation.

Examples:
  xdb hls sim result-hls-csim --case empty --summary
  xdb hls sim result-hls-csim --all --continue-on-failure
  xdb hls provenance result-hls-csim --summary
  xdb hls doctor result-hls-csim --summary
  xdb hls bundle result-hls-csim --out failure-001
"""
    s_hls = sub.add_parser(
        "hls",
        help="run and inspect packaged HLS C simulation",
        description="Finite packaged Vitis HLS C-simulation orchestration",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=hls_epilog,
    )
    _add_debug_flag(s_hls)
    hls_sub = s_hls.add_subparsers(
        dest="hls_cmd",
        required=True,
        metavar="{sim,provenance,doctor,bundle}",
    )

    def add_hls_runtime_args(sp: argparse.ArgumentParser) -> None:
        sp.add_argument(
            "package_runtime",
            nargs="?",
            default=None,
            help=(
                "HLS C-simulation package, runtime directory, or xdb-hls-csim.json; "
                "overrides XDB_HLS_PACKAGE_RUNTIME"
            ),
        )
        sp.add_argument(
            "--workspace",
            default=None,
            help="writable staged workspace; overrides XDB_HLS_WORKSPACE",
        )

    s_hls_sim = hls_sub.add_parser("sim", help="run finite packaged HLS C simulation")
    _add_debug_flag(s_hls_sim)
    add_hls_runtime_args(s_hls_sim)
    hls_case_selection = s_hls_sim.add_mutually_exclusive_group()
    hls_case_selection.add_argument("--case", default=None, help="named manifest test case")
    hls_case_selection.add_argument(
        "--all",
        action="store_true",
        help="run all manifest cases in deterministic name order",
    )
    s_hls_sim.add_argument(
        "--continue-on-failure",
        action="store_true",
        help="run remaining selected cases after a failure (default: fail fast)",
    )
    s_hls_sim.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="per-tool/prepare/case wall-clock timeout in seconds (default: manifest)",
    )
    s_hls_sim.add_argument(
        "--restage",
        action="store_true",
        help="discard and recreate the writable workspace before running",
    )
    s_hls_sim.add_argument(
        "--summary",
        action="store_true",
        help="print concise human-readable output instead of JSON",
    )

    s_hls_provenance = hls_sub.add_parser(
        "provenance", help="report HLS package, staging, invocation, and result provenance"
    )
    _add_debug_flag(s_hls_provenance)
    add_hls_runtime_args(s_hls_provenance)
    s_hls_provenance.add_argument("--case", default=None, help="named manifest test case")
    s_hls_provenance.add_argument("--summary", action="store_true")

    s_hls_doctor = hls_sub.add_parser(
        "doctor", help="diagnose HLS package, workspace, tool, and prior-run health"
    )
    _add_debug_flag(s_hls_doctor)
    add_hls_runtime_args(s_hls_doctor)
    s_hls_doctor.add_argument("--case", default=None, help="named manifest test case")
    s_hls_doctor.add_argument("--summary", action="store_true")

    s_hls_bundle = hls_sub.add_parser("bundle", help="export a bounded HLS failure bundle")
    _add_debug_flag(s_hls_bundle)
    add_hls_runtime_args(s_hls_bundle)
    s_hls_bundle.add_argument(
        "--out",
        required=True,
        help="bundle directory name/path; relative names are under the staged workspace",
    )
    s_hls_bundle.add_argument(
        "--max-bytes",
        type=int,
        default=16 * 1024 * 1024,
        help="maximum copied log/artifact payload bytes (default: 16777216)",
    )

    s_sim = sub.add_parser("sim", description="Persistent Vivado simulation session control")
    _add_debug_flag(s_sim)
    sim_sub = s_sim.add_subparsers(dest="sim_cmd", required=True)

    def add_sim_session_arg(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--session", default=None)

    s_sim_launch = sim_sub.add_parser("launch")
    _add_debug_flag(s_sim_launch)
    add_sim_session_arg(s_sim_launch)
    s_sim_launch.add_argument(
        "package_runtime",
        nargs="?",
        default=None,
        help="packaged simulation output, runtime directory, or xdb-runtime.json; overrides XDB_SIM_PACKAGE_RUNTIME",
    )
    s_sim_launch.add_argument("--simset", default=None)
    s_sim_launch.add_argument(
        "--mode",
        choices=["behavioral", "post-synth", "post-impl"],
        default=None,
    )
    s_sim_launch.add_argument("--top", default=None)
    s_sim_launch.add_argument("--replace", action="store_true")
    s_sim_launch.add_argument("--timeout", type=int, default=300)

    s_sim_relaunch = sim_sub.add_parser("relaunch")
    _add_debug_flag(s_sim_relaunch)
    add_sim_session_arg(s_sim_relaunch)
    s_sim_relaunch.add_argument("--simset", default=None)
    s_sim_relaunch.add_argument(
        "--mode",
        choices=["behavioral", "post-synth", "post-impl"],
        default=None,
    )
    s_sim_relaunch.add_argument("--top", default=None)
    s_sim_relaunch.add_argument("--fresh", action="store_true", default=False)
    s_sim_relaunch.add_argument("--timeout", type=int, default=300)

    s_sim_restage = sim_sub.add_parser("restage")
    _add_debug_flag(s_sim_restage)
    add_sim_session_arg(s_sim_restage)

    s_sim_provenance = sim_sub.add_parser("provenance")
    _add_debug_flag(s_sim_provenance)
    add_sim_session_arg(s_sim_provenance)
    s_sim_provenance.add_argument(
        "--summary", action="store_true", help="print compact human-readable output"
    )

    s_sim_doctor = sim_sub.add_parser("doctor", help="diagnose simulation session/runtime health")
    _add_debug_flag(s_sim_doctor)
    add_sim_session_arg(s_sim_doctor)
    s_sim_doctor.add_argument(
        "--timeout", type=float, default=1.0, help="daemon status probe timeout in seconds"
    )
    s_sim_doctor.add_argument(
        "--summary", action="store_true", help="print compact human-readable output"
    )

    s_sim_run = sim_sub.add_parser("run")
    _add_debug_flag(s_sim_run)
    add_sim_session_arg(s_sim_run)
    s_sim_run.add_argument(
        "--timeout", type=float, default=30.0, help="wall-clock daemon response timeout in seconds"
    )
    s_sim_run.add_argument("time", nargs="*")

    s_sim_restart = sim_sub.add_parser("restart")
    _add_debug_flag(s_sim_restart)
    add_sim_session_arg(s_sim_restart)

    s_sim_close = sim_sub.add_parser("close")
    _add_debug_flag(s_sim_close)
    add_sim_session_arg(s_sim_close)
    s_sim_close.add_argument(
        "--force",
        action="store_true",
        help="terminate cached daemon PID if the session is unresponsive",
    )
    s_sim_close.add_argument(
        "--timeout", type=float, default=5.0, help="wall-clock daemon response timeout in seconds"
    )

    s_sim_time = sim_sub.add_parser("time")
    _add_debug_flag(s_sim_time)
    add_sim_session_arg(s_sim_time)

    s_sim_status = sim_sub.add_parser("status", help="current simulation daemon status")
    _add_debug_flag(s_sim_status)
    add_sim_session_arg(s_sim_status)

    s_sim_describe = sim_sub.add_parser("describe", help="summarize the current simulation session")
    _add_debug_flag(s_sim_describe)
    add_sim_session_arg(s_sim_describe)

    s_sim_get = sim_sub.add_parser("get")
    _add_debug_flag(s_sim_get)
    add_sim_session_arg(s_sim_get)
    s_sim_get.add_argument("signal")

    s_sim_get_many = sim_sub.add_parser("get-many")
    _add_debug_flag(s_sim_get_many)
    add_sim_session_arg(s_sim_get_many)
    s_sim_get_many.add_argument("pattern")

    s_sim_read = sim_sub.add_parser("read", help="read several named signals in one request")
    _add_debug_flag(s_sim_read)
    add_sim_session_arg(s_sim_read)
    s_sim_read.add_argument("signals", nargs="+")

    s_sim_scopes = sim_sub.add_parser("scopes")
    _add_debug_flag(s_sim_scopes)
    add_sim_session_arg(s_sim_scopes)
    s_sim_scopes.add_argument("scope", nargs="?", default=None)

    s_sim_objects = sim_sub.add_parser("objects")
    _add_debug_flag(s_sim_objects)
    add_sim_session_arg(s_sim_objects)
    s_sim_objects.add_argument("scope")

    s_sim_top = sim_sub.add_parser("top")
    _add_debug_flag(s_sim_top)
    add_sim_session_arg(s_sim_top)
    s_sim_top.add_argument("module")

    s_sim_snapshot = sim_sub.add_parser(
        "snapshot", help="capture a structured snapshot of a scope subtree"
    )
    _add_debug_flag(s_sim_snapshot)
    add_sim_session_arg(s_sim_snapshot)
    s_sim_snapshot.add_argument("scope")
    s_sim_snapshot.add_argument("--name", default=None)

    s_sim_diff_snapshot = sim_sub.add_parser("diff-snapshot", help="compare two named snapshots")
    _add_debug_flag(s_sim_diff_snapshot)
    add_sim_session_arg(s_sim_diff_snapshot)
    s_sim_diff_snapshot.add_argument("before")
    s_sim_diff_snapshot.add_argument("after")

    s_sim_watch_changes = sim_sub.add_parser(
        "watch-changes", help="snapshot a scope, run, and diff the result"
    )
    _add_debug_flag(s_sim_watch_changes)
    add_sim_session_arg(s_sim_watch_changes)
    s_sim_watch_changes.add_argument("scope")
    s_sim_watch_changes.add_argument("--for", dest="duration", nargs="+", required=True)

    s_sim_wave = sim_sub.add_parser("wave")
    _add_debug_flag(s_sim_wave)
    sim_wave_sub = s_sim_wave.add_subparsers(dest="sim_wave_cmd", required=True)
    s_sim_wave_add = sim_wave_sub.add_parser("add")
    _add_debug_flag(s_sim_wave_add)
    add_sim_session_arg(s_sim_wave_add)
    s_sim_wave_add.add_argument("pattern")

    s_sim_vcd = sim_sub.add_parser("vcd", help="control persistent VCD dumping")
    _add_debug_flag(s_sim_vcd)
    sim_vcd_sub = s_sim_vcd.add_subparsers(dest="sim_vcd_cmd", required=True)
    s_sim_vcd_start = sim_vcd_sub.add_parser("start")
    _add_debug_flag(s_sim_vcd_start)
    add_sim_session_arg(s_sim_vcd_start)
    s_sim_vcd_start.add_argument("file")
    s_sim_vcd_start.add_argument("scope", nargs="?", default=None)
    s_sim_vcd_stop = sim_vcd_sub.add_parser("stop")
    _add_debug_flag(s_sim_vcd_stop)
    add_sim_session_arg(s_sim_vcd_stop)
    s_sim_vcd_status = sim_vcd_sub.add_parser("status")
    _add_debug_flag(s_sim_vcd_status)
    add_sim_session_arg(s_sim_vcd_status)

    s_sim_step = sim_sub.add_parser("step")
    _add_debug_flag(s_sim_step)
    add_sim_session_arg(s_sim_step)
    s_sim_step.add_argument("arg", nargs="*", default=[])

    s_sim_until = sim_sub.add_parser(
        "until",
        aliases=["wait", "wait-on-condition"],
        help="run in steps until a Tcl expression becomes true",
        description=(
            "Run the simulator in repeated time steps until the given Tcl expression "
            "evaluates true. The default step is '10 ns'. Use --timeout and/or "
            "--max-iterations to bound the wait. Example: xdb sim until "
            "'{[get_value /tb_top/done] eq \"1\"}'"
        ),
    )
    _add_debug_flag(s_sim_until)
    add_sim_session_arg(s_sim_until)
    s_sim_until.add_argument(
        "--step",
        nargs=2,
        default=["10", "ns"],
        metavar="STEP",
        help="simulation time step between condition checks, default: 10 ns",
    )
    s_sim_until.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="maximum wall-clock seconds to wait before failing",
    )
    s_sim_until.add_argument(
        "--max-iterations",
        type=int,
        default=None,
        help="maximum number of run/check iterations before failing",
    )
    s_sim_until.add_argument(
        "expr",
        nargs="+",
        metavar="TCL_EXPR",
        help="Tcl expr body, e.g. '{[get_value /tb_top/done] eq \"1\"}'",
    )

    s_sim_until_signal = sim_sub.add_parser(
        "until-signal",
        aliases=["wait-signal", "wait-on-signal"],
        help="run in steps until a signal reaches an exact value",
        description=(
            "Run the simulator in repeated time steps until get_value <signal> equals "
            "the expected value exactly. The default step is '10 ns'. Use --timeout "
            "and/or --max-iterations to bound the wait. Example: xdb sim until-signal "
            "/tb_top/done 1"
        ),
    )
    _add_debug_flag(s_sim_until_signal)
    add_sim_session_arg(s_sim_until_signal)
    s_sim_until_signal.add_argument(
        "--step",
        nargs=2,
        default=["10", "ns"],
        metavar="STEP",
        help="simulation time step between value checks, default: 10 ns",
    )
    s_sim_until_signal.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="maximum wall-clock seconds to wait before failing",
    )
    s_sim_until_signal.add_argument(
        "--max-iterations",
        type=int,
        default=None,
        help="maximum number of run/check iterations before failing",
    )
    s_sim_until_signal.add_argument("signal", help="hierarchical signal path")
    s_sim_until_signal.add_argument("value", help="exact expected get_value result")

    s_sim_assert_signal = sim_sub.add_parser(
        "assert-signal", help="assert a signal has an exact value now"
    )
    _add_debug_flag(s_sim_assert_signal)
    add_sim_session_arg(s_sim_assert_signal)
    s_sim_assert_signal.add_argument("signal")
    s_sim_assert_signal.add_argument("value")

    s_sim_assert_tcl = sim_sub.add_parser("assert-tcl", help="assert a Tcl expression is true now")
    _add_debug_flag(s_sim_assert_tcl)
    add_sim_session_arg(s_sim_assert_tcl)
    s_sim_assert_tcl.add_argument("expr", nargs="+")

    s_sim_expect_signal = sim_sub.add_parser(
        "expect-signal", help="expect a signal to reach a value within a simulation time bound"
    )
    _add_debug_flag(s_sim_expect_signal)
    add_sim_session_arg(s_sim_expect_signal)
    s_sim_expect_signal.add_argument("--within", nargs=2, required=True)
    s_sim_expect_signal.add_argument("signal")
    s_sim_expect_signal.add_argument("value")

    s_sim_expect_change = sim_sub.add_parser(
        "expect-change", help="expect a signal to change within a simulation time bound"
    )
    _add_debug_flag(s_sim_expect_change)
    add_sim_session_arg(s_sim_expect_change)
    s_sim_expect_change.add_argument("--within", nargs=2, required=True)
    s_sim_expect_change.add_argument("signal")

    s_sim_expect_condition = sim_sub.add_parser(
        "expect-condition",
        help="expect a Tcl expression to become true within a simulation time bound",
    )
    _add_debug_flag(s_sim_expect_condition)
    add_sim_session_arg(s_sim_expect_condition)
    s_sim_expect_condition.add_argument("--within", nargs=2, required=True)
    s_sim_expect_condition.add_argument("expr", nargs="+")

    s_sim_expect_stream_output = sim_sub.add_parser(
        "expect-stream-output",
        help="expect at least one AXIS handshake within a simulation time bound",
    )
    _add_debug_flag(s_sim_expect_stream_output)
    add_sim_session_arg(s_sim_expect_stream_output)
    s_sim_expect_stream_output.add_argument("--within", nargs=2, required=True)
    s_sim_expect_stream_output.add_argument("--step", nargs=2, default=["1", "ns"])
    s_sim_expect_stream_output.add_argument("--decode-bytes", action="store_true")
    s_sim_expect_stream_output.add_argument(
        "--lane-order",
        choices=["low-to-high", "high-to-low"],
        default="low-to-high",
    )
    s_sim_expect_stream_output.add_argument("path")

    s_sim_breakpoint = sim_sub.add_parser("breakpoint")
    _add_debug_flag(s_sim_breakpoint)
    sim_bp_sub = s_sim_breakpoint.add_subparsers(dest="sim_bp_cmd", required=True)
    s_sim_breakpoint_add = sim_bp_sub.add_parser("add")
    _add_debug_flag(s_sim_breakpoint_add)
    add_sim_session_arg(s_sim_breakpoint_add)
    s_sim_breakpoint_add.add_argument(
        "--poll-step",
        default=None,
        help="polling interval for fallback-polled breakpoints, e.g. '1 ns'",
    )
    s_sim_breakpoint_add.add_argument("condition", nargs="+")
    s_sim_breakpoint_list = sim_bp_sub.add_parser("list")
    _add_debug_flag(s_sim_breakpoint_list)
    add_sim_session_arg(s_sim_breakpoint_list)
    s_sim_breakpoint_remove = sim_bp_sub.add_parser("remove")
    _add_debug_flag(s_sim_breakpoint_remove)
    add_sim_session_arg(s_sim_breakpoint_remove)
    s_sim_breakpoint_remove.add_argument("breakpoint_id", type=int)
    s_sim_breakpoint_clear = sim_bp_sub.add_parser("clear")
    _add_debug_flag(s_sim_breakpoint_clear)
    add_sim_session_arg(s_sim_breakpoint_clear)

    s_sim_tcl = sim_sub.add_parser("tcl")
    _add_debug_flag(s_sim_tcl)
    add_sim_session_arg(s_sim_tcl)
    s_sim_tcl.add_argument("--file", default=None)
    s_sim_tcl.add_argument("script", nargs="*")

    s_sim_source = sim_sub.add_parser("source")
    _add_debug_flag(s_sim_source)
    add_sim_session_arg(s_sim_source)
    s_sim_source.add_argument("path")

    s_sim_force = sim_sub.add_parser("force")
    _add_debug_flag(s_sim_force)
    add_sim_session_arg(s_sim_force)
    s_sim_force.add_argument("--radix", default=None)
    s_sim_force.add_argument("--repeat-every", default=None)
    s_sim_force.add_argument("--cancel-after", default=None)
    s_sim_force.add_argument("signal")
    s_sim_force.add_argument("values", nargs="+")

    s_sim_axis = sim_sub.add_parser("axis", help="AXI Stream helpers")
    _add_debug_flag(s_sim_axis)
    sim_axis_sub = s_sim_axis.add_subparsers(dest="sim_axis_cmd", required=True)
    s_sim_axis_trace = sim_axis_sub.add_parser("trace")
    _add_debug_flag(s_sim_axis_trace)
    add_sim_session_arg(s_sim_axis_trace)
    s_sim_axis_trace.add_argument(
        "--profile", default=None, help="trace profile name from .xdb-trace.json"
    )
    s_sim_axis_trace.add_argument(
        "--profile-file", default=None, help="explicit trace profile JSON file"
    )
    s_sim_axis_trace.add_argument("paths", nargs="*")
    s_sim_axis_trace.add_argument("--for", dest="duration", nargs="+")
    s_sim_axis_trace.add_argument("--step", nargs="+", default=None)
    s_sim_axis_trace.add_argument("--decode-bytes", action="store_true", default=None)
    s_sim_axis_trace.add_argument(
        "--lane-order",
        choices=["low-to-high", "high-to-low"],
        default=None,
    )
    s_sim_axis_trace.add_argument("--include-idle", action="store_true", default=None)
    s_sim_axis_trace.add_argument("--only-handshakes", action="store_true", default=None)
    s_sim_axis_trace.add_argument("--ndjson", action="store_true")
    s_sim_axis_trace.add_argument("--out", default=None, help="write trace output to a file")

    s_sim_trace = sim_sub.add_parser("trace", help="simulation trace helpers")
    _add_debug_flag(s_sim_trace)
    sim_trace_sub = s_sim_trace.add_subparsers(dest="sim_trace_cmd", required=True)
    s_sim_trace_transactions = sim_trace_sub.add_parser("transactions")
    _add_debug_flag(s_sim_trace_transactions)
    add_sim_session_arg(s_sim_trace_transactions)
    s_sim_trace_transactions.add_argument("--for", dest="duration", nargs="+", required=True)
    s_sim_trace_transactions.add_argument("--opcode", default=None)
    s_sim_trace_transactions.add_argument(
        "--out", default=None, help="write trace output to a file"
    )
    s_sim_trace_profiles = sim_trace_sub.add_parser(
        "profiles", help="list available named trace profiles"
    )
    _add_debug_flag(s_sim_trace_profiles)
    s_sim_trace_profiles.add_argument(
        "--profile-file", default=None, help="explicit trace profile JSON file"
    )

    s_sim_exec = sim_sub.add_parser(
        "exec", help="run a host command against the live simulation session"
    )
    _add_debug_flag(s_sim_exec)
    add_sim_session_arg(s_sim_exec)
    s_sim_exec.add_argument("--cwd", default=None, help="working directory for the command")
    s_sim_exec.add_argument("--env", dest="env_overrides", action="append", default=[])
    s_sim_exec.add_argument(
        "--timeout", type=float, default=None, help="wall-clock timeout in seconds"
    )
    s_sim_exec.add_argument("--expect-exit-code", type=int, default=0)
    s_sim_exec.add_argument(
        "--clean-env", action="store_true", help="do not inherit the current process environment"
    )
    s_sim_exec.add_argument(
        "--stream",
        action="store_true",
        help="stream host stdout/stderr live to stderr while still capturing final JSON",
    )
    s_sim_exec.add_argument("command", nargs=argparse.REMAINDER)

    s_sim_with_trace = sim_sub.add_parser("with-trace", help="run a command with scoped tracing")
    _add_debug_flag(s_sim_with_trace)
    add_sim_session_arg(s_sim_with_trace)
    s_sim_with_trace.add_argument(
        "--profile", default=None, help="trace profile name from .xdb-trace.json"
    )
    s_sim_with_trace.add_argument(
        "--profile-file", default=None, help="explicit trace profile JSON file"
    )
    s_sim_with_trace.add_argument("--transactions", action="store_true", default=None)
    s_sim_with_trace.add_argument("--axis", dest="axis_paths", action="append", default=[])
    s_sim_with_trace.add_argument(
        "--exec",
        dest="exec_mode",
        action="store_true",
        help="wrap an external host command instead of an xdb sim subcommand",
    )
    s_sim_with_trace.add_argument(
        "--exec-until-exit",
        action="store_true",
        help="with --exec, trace until the host command exits instead of requiring --for",
    )
    s_sim_with_trace.add_argument(
        "--cwd", default=None, help="working directory for --exec command"
    )
    s_sim_with_trace.add_argument("--env", dest="exec_env_overrides", action="append", default=[])
    s_sim_with_trace.add_argument(
        "--timeout", type=float, default=None, help="wall-clock timeout for --exec command"
    )
    s_sim_with_trace.add_argument("--expect-exit-code", type=int, default=0)
    s_sim_with_trace.add_argument(
        "--clean-env",
        action="store_true",
        help="do not inherit current environment for --exec command",
    )
    s_sim_with_trace.add_argument(
        "--stream",
        action="store_true",
        help="with --exec, stream host stdout/stderr live to stderr while tracing",
    )
    s_sim_with_trace.add_argument("--for", dest="duration", nargs="+")
    s_sim_with_trace.add_argument("--step", nargs="+", default=None)
    s_sim_with_trace.add_argument("--decode-bytes", action="store_true", default=None)
    s_sim_with_trace.add_argument(
        "--lane-order",
        choices=["low-to-high", "high-to-low"],
        default=None,
    )
    s_sim_with_trace.add_argument("--include-idle", action="store_true", default=None)
    s_sim_with_trace.add_argument("--only-handshakes", action="store_true", default=None)
    s_sim_with_trace.add_argument(
        "--correlate-by",
        choices=["nearest", "opcode", "addr"],
        default=None,
        help="focus transaction-to-AXIS correlation links",
    )
    s_sim_with_trace.add_argument(
        "--correlate-window",
        nargs="+",
        default=None,
        help="maximum simulator-time delta for correlation links",
    )
    s_sim_with_trace_output = s_sim_with_trace.add_mutually_exclusive_group()
    s_sim_with_trace_output.add_argument("--ndjson", action="store_true")
    s_sim_with_trace_output.add_argument("--summary", action="store_true")
    s_sim_with_trace.add_argument("--out", default=None, help="write trace output to a file")
    s_sim_with_trace.add_argument(
        "--bundle",
        default=None,
        nargs="?",
        const="",
        help="write a trace artifact bundle under XDB_ROOT/artifacts/bundles",
    )
    s_sim_with_trace.add_argument("command", nargs=argparse.REMAINDER)

    s_sim_bundle = sim_sub.add_parser("bundle", help="export a simulation debug artifact bundle")
    _add_debug_flag(s_sim_bundle)
    add_sim_session_arg(s_sim_bundle)
    s_sim_bundle.add_argument(
        "--out",
        default=None,
        help="bundle directory name/path; relative paths are under XDB_ROOT/artifacts/bundles",
    )

    s_sim_release = sim_sub.add_parser("release")
    _add_debug_flag(s_sim_release)
    add_sim_session_arg(s_sim_release)
    s_sim_release.add_argument("--all", action="store_true")
    s_sim_release.add_argument("signal", nargs="?", default=None)

    s_sim_csr = sim_sub.add_parser("csr", help="Coyote CSR access")
    _add_debug_flag(s_sim_csr)
    sim_csr_sub = s_sim_csr.add_subparsers(dest="sim_csr_cmd", required=True)
    s_sim_csr_read = sim_csr_sub.add_parser("read")
    _add_debug_flag(s_sim_csr_read)
    add_sim_session_arg(s_sim_csr_read)
    s_sim_csr_read.add_argument("addr")
    s_sim_csr_read.add_argument("--timeout", type=float, default=None)
    s_sim_csr_write = sim_csr_sub.add_parser("write")
    _add_debug_flag(s_sim_csr_write)
    add_sim_session_arg(s_sim_csr_write)
    s_sim_csr_write.add_argument("addr")
    s_sim_csr_write.add_argument("value")

    s_sim_service_csr = sim_sub.add_parser(
        "service-csr", help="resident dynamic-service CSR access"
    )
    _add_debug_flag(s_sim_service_csr)
    sim_service_csr_sub = s_sim_service_csr.add_subparsers(
        dest="sim_service_csr_cmd", required=True
    )
    s_sim_service_csr_read = sim_service_csr_sub.add_parser("read")
    _add_debug_flag(s_sim_service_csr_read)
    add_sim_session_arg(s_sim_service_csr_read)
    s_sim_service_csr_read.add_argument("addr")
    s_sim_service_csr_read.add_argument("--timeout", type=float, default=None)
    s_sim_service_csr_write = sim_service_csr_sub.add_parser("write")
    _add_debug_flag(s_sim_service_csr_write)
    add_sim_session_arg(s_sim_service_csr_write)
    s_sim_service_csr_write.add_argument("addr")
    s_sim_service_csr_write.add_argument("value")

    s_sim_mem = sim_sub.add_parser("mem", help="Coyote host memory access")
    _add_debug_flag(s_sim_mem)
    sim_mem_sub = s_sim_mem.add_subparsers(dest="sim_mem_cmd", required=True)
    s_sim_mem_map = sim_mem_sub.add_parser("map")
    _add_debug_flag(s_sim_mem_map)
    add_sim_session_arg(s_sim_mem_map)
    s_sim_mem_map.add_argument("space")
    s_sim_mem_map.add_argument("addr")
    s_sim_mem_map.add_argument("size")
    s_sim_mem_unmap = sim_mem_sub.add_parser("unmap")
    _add_debug_flag(s_sim_mem_unmap)
    add_sim_session_arg(s_sim_mem_unmap)
    s_sim_mem_unmap.add_argument("space")
    s_sim_mem_unmap.add_argument("addr")
    s_sim_mem_list = sim_mem_sub.add_parser("list")
    _add_debug_flag(s_sim_mem_list)
    add_sim_session_arg(s_sim_mem_list)
    s_sim_mem_list.add_argument("space", nargs="?", default="host")
    s_sim_mem_reset = sim_mem_sub.add_parser("reset")
    _add_debug_flag(s_sim_mem_reset)
    add_sim_session_arg(s_sim_mem_reset)
    s_sim_mem_reset.add_argument("space", nargs="?", default="host")
    s_sim_mem_read = sim_mem_sub.add_parser("read")
    _add_debug_flag(s_sim_mem_read)
    add_sim_session_arg(s_sim_mem_read)
    s_sim_mem_read.add_argument("space")
    s_sim_mem_read.add_argument("addr")
    s_sim_mem_read.add_argument("size")
    s_sim_mem_dump = sim_mem_sub.add_parser("dump")
    _add_debug_flag(s_sim_mem_dump)
    add_sim_session_arg(s_sim_mem_dump)
    s_sim_mem_dump.add_argument("space")
    s_sim_mem_dump.add_argument("addr")
    s_sim_mem_dump.add_argument("--size", required=True)
    s_sim_mem_dump.add_argument("--out", required=True)
    s_sim_mem_diff = sim_mem_sub.add_parser("diff")
    _add_debug_flag(s_sim_mem_diff)
    s_sim_mem_diff.add_argument("before")
    s_sim_mem_diff.add_argument("after")
    s_sim_mem_write = sim_mem_sub.add_parser("write")
    _add_debug_flag(s_sim_mem_write)
    add_sim_session_arg(s_sim_mem_write)
    s_sim_mem_write.add_argument("space")
    s_sim_mem_write.add_argument("addr")
    mem_payload_group = s_sim_mem_write.add_mutually_exclusive_group(required=True)
    mem_payload_group.add_argument("--hex", dest="hex_data", default=None)
    mem_payload_group.add_argument("--text", dest="text_data", default=None)
    mem_payload_group.add_argument("--file", default=None)

    s_sim_invoke = sim_sub.add_parser("invoke", help="Coyote high-level invoke")
    _add_debug_flag(s_sim_invoke)
    add_sim_session_arg(s_sim_invoke)
    s_sim_invoke.add_argument("opcode")
    s_sim_invoke.add_argument("--addr", default=None)
    s_sim_invoke.add_argument("--len", dest="length", default=None)
    s_sim_invoke.add_argument("--stream", default="host")
    s_sim_invoke.add_argument("--dest", default="0")
    s_sim_invoke.add_argument("--last", action=argparse.BooleanOptionalAction, default=True)
    s_sim_invoke.add_argument("--src-addr", default=None)
    s_sim_invoke.add_argument("--src-len", default=None)
    s_sim_invoke.add_argument("--src-stream", default="host")
    s_sim_invoke.add_argument("--src-dest", default="0")
    s_sim_invoke.add_argument("--dst-addr", default=None)
    s_sim_invoke.add_argument("--dst-len", default=None)
    s_sim_invoke.add_argument("--dst-stream", default="host")
    s_sim_invoke.add_argument("--dst-dest", default="0")

    s_sim_completed = sim_sub.add_parser("completed", help="Coyote completion counters")
    _add_debug_flag(s_sim_completed)
    add_sim_session_arg(s_sim_completed)
    s_sim_completed.add_argument("opcode")
    s_sim_completed.add_argument("--count", type=int, default=None)
    s_sim_completed.add_argument("--timeout", type=float, default=None)

    s_sim_clear_completed = sim_sub.add_parser("clear-completed")
    _add_debug_flag(s_sim_clear_completed)
    add_sim_session_arg(s_sim_clear_completed)

    s_sim_irq = sim_sub.add_parser("irq", help="Coyote IRQ handling")
    _add_debug_flag(s_sim_irq)
    sim_irq_sub = s_sim_irq.add_subparsers(dest="sim_irq_cmd", required=True)
    s_sim_irq_wait = sim_irq_sub.add_parser("wait")
    _add_debug_flag(s_sim_irq_wait)
    add_sim_session_arg(s_sim_irq_wait)
    s_sim_irq_wait.add_argument("--timeout", type=float, default=None)

    s_sim_coyote_status = sim_sub.add_parser("coyote-status")
    _add_debug_flag(s_sim_coyote_status)
    add_sim_session_arg(s_sim_coyote_status)

    s_simd = sub.add_parser("_simd", help=argparse.SUPPRESS)
    _add_debug_flag(s_simd)
    s_simd.add_argument("--anchor-dir", required=True)
    s_simd.add_argument("--session", default=None)
    s_simd.add_argument("--project", default="")
    s_simd.add_argument("--simset", required=True)
    s_simd.add_argument("--mode", required=True)
    s_simd.add_argument("--top", default="")
    s_simd.add_argument("--package-runtime", default="")
    s_simd.add_argument("--runtime-root", default="")
    s_simd.add_argument("--work-dir", default="")
    s_simd.add_argument("--compile-script", default="")
    s_simd.add_argument("--elaborate-script", default="")
    s_simd.add_argument("--simulate-script", default="")

    # argparse.SUPPRESS hides the subparser's detail row in some Python versions
    # but still leaks a literal "==SUPPRESS==" pseudo-action in others. Keep the
    # private command parseable while removing it from the user-facing help table.
    sub._choices_actions = [  # noqa: SLF001
        action
        for action in sub._choices_actions
        if action.dest != "_simd"  # noqa: SLF001
    ]

    return p
