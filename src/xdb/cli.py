from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import sys
import traceback
from xdb.cli_output import (
    _emit_json,
    _emit_text,
    _format_doctor_summary,
    _format_provenance_summary,
    _format_with_trace_ndjson,
    _format_with_trace_summary,
    _print,
)
from xdb.config import set_config_file
from xdb.cli_parser import build_parser
from xdb.backend.base import Capability, IlaTriggerConfig, ProbeTrigger
from xdb.backend.select import select_backend
from xdb.errors import UnsupportedOperationError, XdbError
from xdb.hls import (
    create_hls_bundle,
    format_hls_doctor_summary,
    format_hls_provenance_summary,
    format_hls_sim_summary,
    hls_doctor,
    hls_provenance,
    run_hls_sim,
)
from xdb.sim.client import (
    add_breakpoint,
    add_wave,
    axis_trace_session,
    assert_signal_session,
    assert_tcl_session,
    clear_breakpoints,
    close_session,
    list_breakpoints,
    coyote_clear_completed_session,
    coyote_completed_session,
    coyote_csr_read_session,
    coyote_csr_write_session,
    coyote_service_csr_read_session,
    coyote_service_csr_write_session,
    coyote_invoke_session,
    coyote_irq_wait_session,
    coyote_mem_list_session,
    coyote_mem_map_session,
    coyote_mem_read_session,
    coyote_mem_reset_session,
    coyote_mem_unmap_session,
    coyote_mem_write_session,
    coyote_status_session,
    expect_change_session,
    expect_condition_session,
    expect_signal_session,
    expect_stream_output_session,
    exec_session,
    describe_session,
    doctor_session,
    force_session,
    get_many_signals,
    get_objects,
    get_scopes,
    get_signal,
    read_signals,
    launch_session,
    provenance_session,
    relaunch_session,
    release_session,
    remove_breakpoint,
    restage_session,
    restart_session,
    run_session,
    set_top,
    source_session,
    snapshot_session,
    status_session,
    step_session,
    time_session,
    trace_transactions_session,
    vcd_start_session,
    with_trace_session,
    vcd_status_session,
    vcd_stop_session,
    tcl_session,
    wait_until_session,
    wait_until_signal_session,
    diff_snapshot_session,
    watch_changes_session,
)
from xdb.sim.bundles import create_sim_bundle
from xdb.sim.coyote import parse_hex_bytes
from xdb.sim.daemon import run_daemon
from xdb.sim.mem_tools import diff_memory_files, dump_memory_session
from xdb.sim.trace_profiles import get_trace_profile, list_trace_profiles
from xdb.reports.cips import format_cips_report, inspect_cips
from xdb.reports.floorplan import format_floorplan_report, generate_floorplan_svg
from xdb.reports.utilization import (
    discover_utilization_report,
    format_utilization_comparison,
    format_utilization_csv,
    format_utilization_delta_comparison,
    format_utilization_delta_csv,
    format_utilization_table,
    parse_utilization_report,
    utilization_compare_data,
)
from xdb.vivado_ip import format_vivado_ip_info, vivado_ip_info
from xdb.vivado_log import format_vivado_log_summary, summarize_vivado_log_text
from xdb.timing.analysis import (
    format_timing_clocks,
    format_timing_compare,
    format_timing_drc,
    format_timing_net,
    format_timing_paths,
    format_timing_summary,
    format_timing_triage,
    timing_clocks,
    timing_compare,
    timing_drc,
    timing_net,
    timing_paths,
    timing_summary,
    timing_triage,
)


def _resolve_part_hint(cli_value: str | None) -> str | None:
    return cli_value or os.environ.get("FPGA_PART_HINT")


def _require_part_hint(cli_value: str | None) -> str:
    part_hint = _resolve_part_hint(cli_value)
    if not part_hint:
        raise XdbError(
            "missing FPGA part hint: pass --part-hint (or --fpga-part-hint) or set FPGA_PART_HINT"
        )
    return part_hint


def _resolve_bitstream(cli_value: str | None) -> str:
    bit = cli_value or os.environ.get("FPGA_BITSTREAM")
    if not bit:
        raise XdbError("missing bitstream: pass --bit or set FPGA_BITSTREAM")
    if not os.path.isfile(bit):
        raise XdbError(f"bitstream not found: {bit}")
    return bit


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_optional_ltx(cli_value: str | None) -> str | None:
    ltx = cli_value or os.environ.get("FPGA_LTX")
    if not ltx:
        return None
    if not os.path.isfile(ltx):
        raise XdbError(f"ltx not found: {ltx}")
    return ltx


def _is_power_of_two(v: int) -> bool:
    return v > 0 and (v & (v - 1)) == 0


def _validate_samples(samples: int) -> int:
    if samples <= 0:
        raise XdbError("--samples must be > 0")
    if not _is_power_of_two(samples):
        raise XdbError("--samples must be a power of two (e.g., 128, 256, 512, 1024)")
    return samples


def _validate_windows(windows: int) -> int:
    if windows <= 0:
        raise XdbError("--windows must be > 0")
    return windows


def _validate_trigger_position(position: int | None, samples: int) -> int | None:
    if position is not None and not 0 <= position < samples:
        raise XdbError(f"--trigger-position must be between 0 and {samples - 1}")
    return position


def _parse_trigger_value(value: str) -> str | int:
    unsigned = value[1:] if value.startswith("-") else value
    if unsigned == "0" or (unsigned.isdigit() and not unsigned.startswith("0")):
        return int(value, 10)
    return value


def _resolve_tsm(path: str | None) -> str | None:
    if path is None:
        return None
    if not os.path.isfile(path):
        raise XdbError(f"trigger state-machine file not found: {path}")
    return path


def _parse_probe_triggers(values: list[list[str]]) -> list[ProbeTrigger]:
    operators = {"==", "!=", ">", "<", ">=", "<=", "||"}
    triggers: list[ProbeTrigger] = []
    for probe, operator, value in values:
        if operator not in operators:
            raise XdbError(
                f"unsupported trigger operator {operator}: expected one of "
                + ", ".join(sorted(operators))
            )
        triggers.append(
            {
                "probe": probe,
                "operator": operator,
                "value": _parse_trigger_value(value),
            }
        )
    return triggers


def _unsupported_operation_message(
    operation: str,
    backend_name: str,
    target_hint: str | None,
) -> str:
    suggestion = ""
    if backend_name == "vivado":
        suggestion = " try XDB_BACKEND=chipscopy for Versal features"
    elif backend_name == "chipscopy":
        suggestion = " try XDB_BACKEND=vivado for UltraScale+ ILA workflows"
    tgt = target_hint or "<unspecified>"
    return (
        f"operation not supported: operation={operation} backend={backend_name} "
        f"target={tgt}.{suggestion}".strip()
    )


def _require_capability(
    backend,
    capability: Capability,
    operation: str,
    target_hint: str | None,
) -> None:
    caps = backend.capabilities()
    if capability not in caps:
        raise UnsupportedOperationError(
            _unsupported_operation_message(operation, backend.name, target_hint)
        )


def _join_tokens(values: list[str]) -> str | None:
    if not values:
        return None
    return " ".join(values).strip() or None


def _resolve_tcl_script(values: list[str], file_path: str | None) -> str:
    if file_path and values:
        raise XdbError("pass either Tcl tokens or --file, not both")
    if file_path:
        if file_path == "-":
            return sys.stdin.read()
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                return f.read()
        except OSError as e:
            raise XdbError(f"failed to read Tcl script: {file_path}") from e
    script = _join_tokens(values)
    if script == "-":
        return sys.stdin.read()
    if not script:
        raise XdbError("missing Tcl script")
    return script


def _env_debug_enabled() -> bool:
    for name in ("XDB_DEBUG", "XDB_VERBOSE"):
        value = os.environ.get(name)
        if value is None:
            continue
        if value.strip().lower() in {"", "0", "false", "no", "off"}:
            continue
        return True
    return False


def _resolve_sim_step_tokens(values: list[str] | None) -> list[str]:
    if not values:
        raise XdbError("missing step duration")
    tokens = [value.strip() for value in values if value.strip()]
    if not tokens:
        raise XdbError("missing step duration")
    return tokens


def _validate_positive_timeout_seconds(value: float | None) -> float | None:
    if value is None:
        return None
    if value <= 0:
        raise XdbError("--timeout must be > 0")
    return value


def _validate_positive_iteration_limit(value: int | None) -> int | None:
    if value is None:
        return None
    if value <= 0:
        raise XdbError("--max-iterations must be > 0")
    return value


def _profile_tokens(profile: dict, *names: str) -> list[str] | None:
    for name in names:
        value = profile.get(name)
        if value is None:
            continue
        if isinstance(value, str):
            tokens = value.split()
        elif isinstance(value, list):
            tokens = [str(item).strip() for item in value if str(item).strip()]
        else:
            raise XdbError(f"trace profile field {name!r} must be a string or list")
        if not tokens:
            raise XdbError(f"trace profile field {name!r} must not be empty")
        return tokens
    return None


def _profile_string_list(profile: dict, *names: str) -> list[str]:
    for name in names:
        value = profile.get(name)
        if value is None:
            continue
        if isinstance(value, str):
            return [value]
        if isinstance(value, list):
            return [str(item) for item in value if str(item)]
        raise XdbError(f"trace profile field {name!r} must be a string or list")
    return []


def _profile_bool(profile: dict, cli_value: bool | None, name: str, default: bool = False) -> bool:
    if cli_value is not None:
        return bool(cli_value)
    value = profile.get(name)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise XdbError(f"trace profile field {name!r} must be boolean")
    return value


def _profile_str(profile: dict, cli_value: str | None, name: str, default: str) -> str:
    if cli_value is not None:
        return cli_value
    value = profile.get(name)
    if value is None:
        return default
    if not isinstance(value, str):
        raise XdbError(f"trace profile field {name!r} must be a string")
    return value


def _load_cli_trace_profile(name: str | None, profile_file: str | None) -> dict:
    if name is None and profile_file is None:
        return {"name": None, "source": None, "config": {}}
    profile = get_trace_profile(name, profile_file)
    config = profile.get("config")
    if not isinstance(config, dict):
        raise XdbError("invalid trace profile config")
    return profile


def _parse_int(value: str, *, what: str) -> int:
    text = value.strip()
    if not text:
        raise XdbError(f"missing {what}")
    try:
        return int(text, 0)
    except ValueError as e:
        raise XdbError(f"invalid {what}: {value}") from e


def _read_binary_file(path: str) -> bytes:
    resolved = path if path != "-" else None
    try:
        if resolved is None:
            return sys.stdin.buffer.read()
        with open(path, "rb") as f:
            return f.read()
    except OSError as e:
        raise XdbError(f"failed to read binary payload: {path}") from e


def _resolve_mem_payload(args: argparse.Namespace) -> bytes:
    if args.hex_data is not None:
        return parse_hex_bytes(args.hex_data)
    if args.text_data is not None:
        return args.text_data.encode("utf-8")
    if args.file is not None:
        return _read_binary_file(args.file)
    raise XdbError("provide one of --hex, --text, or --file")


def _configure_diagnostics(debug: bool) -> None:
    effective_debug = debug or _env_debug_enabled()
    if effective_debug:
        os.environ["XDB_DEBUG"] = "1"
        os.environ["XDB_VERBOSE"] = "1"
    else:
        os.environ.pop("XDB_DEBUG", None)
        os.environ.pop("XDB_VERBOSE", None)


def _print_error(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)


def _run_reports_utilization(args: argparse.Namespace) -> None:
    names = list(args.name or [])
    paths = list(args.paths or [])
    if names and len(names) != len(paths):
        raise XdbError("--name must be passed once per input path")
    parsed_reports = [
        parse_utilization_report(discover_utilization_report(path, report=args.report))
        for path in paths
    ]
    if args.json:
        if len(parsed_reports) == 1:
            _emit_json(parsed_reports[0])
        else:
            _emit_json({"reports": parsed_reports})
    elif args.csv:
        _emit_text(format_utilization_csv(parsed_reports, names or None, args.resource))
    elif len(parsed_reports) == 1:
        _emit_text(
            format_utilization_table(
                parsed_reports[0],
                args.resource,
                all_rows=bool(args.all),
            )
        )
    else:
        _emit_text(format_utilization_comparison(parsed_reports, names or None, args.resource))


def _run_reports_cips(args: argparse.Namespace) -> None:
    if args.timeout <= 0:
        raise XdbError("--timeout must be > 0")
    data = inspect_cips(
        args.path,
        dcp=args.dcp,
        bif=args.bif,
        timeout=args.timeout,
    )
    _emit_json(data) if args.json else _emit_text(format_cips_report(data))


def _run_reports_floorplan(args: argparse.Namespace) -> None:
    if args.hierarchy_depth <= 0:
        raise XdbError("--hierarchy-depth must be > 0")
    if args.timeout <= 0:
        raise XdbError("--timeout must be > 0")
    if args.max_groups <= 0:
        raise XdbError("--max-groups must be > 0")
    data = generate_floorplan_svg(
        args.path,
        output=args.out,
        dcp=args.dcp,
        hierarchy_depth=args.hierarchy_depth,
        title=args.title,
        show_pblocks=bool(args.show_pblocks),
        max_groups=args.max_groups,
        force=bool(args.force),
        timeout=args.timeout,
    )
    _emit_json(data) if args.json else _emit_text(format_floorplan_report(data))


def _default_report_input_name(path: str) -> str:
    name = os.path.basename(os.path.normpath(path))
    return name or path


def _format_multi_utilization_delta_csv(comparisons: list[dict]) -> str:
    lines = [
        "baseline,new,resource,old_used,new_used,delta_used,delta_used_percent,old_util_percent,new_util_percent,delta_util_percent_points"
    ]
    for comparison in comparisons:
        old_info_raw = comparison.get("old")
        new_info_raw = comparison.get("new")
        old_info = old_info_raw if isinstance(old_info_raw, dict) else {}
        new_info = new_info_raw if isinstance(new_info_raw, dict) else {}
        old_name = str(old_info.get("name") or "old")
        new_name = str(new_info.get("name") or "new")
        body = format_utilization_delta_csv(comparison).splitlines()[1:]
        lines.extend(f"{old_name},{new_name},{line}" for line in body)
    return "\n".join(lines)


def _run_reports_compare(args: argparse.Namespace) -> None:
    new_paths = list(args.new or [])
    new_names = list(args.new_name or [])
    if new_names and len(new_names) != len(new_paths):
        raise XdbError("--new-name must be passed once per new input path")

    old = parse_utilization_report(discover_utilization_report(args.old, report=args.report))
    old_name = args.old_name or _default_report_input_name(args.old)
    comparisons = []
    for index, new_path in enumerate(new_paths):
        new = parse_utilization_report(discover_utilization_report(new_path, report=args.report))
        comparisons.append(
            utilization_compare_data(
                old,
                new,
                old_name=old_name,
                new_name=new_names[index] if new_names else _default_report_input_name(new_path),
                resources=args.resource,
            )
        )

    if len(comparisons) == 1:
        data = comparisons[0]
        if args.json:
            _emit_json(data)
        elif args.csv:
            _emit_text(format_utilization_delta_csv(data))
        else:
            _emit_text(format_utilization_delta_comparison(data))
        return

    if args.json:
        _emit_json({"comparisons": comparisons})
    elif args.csv:
        _emit_text(_format_multi_utilization_delta_csv(comparisons))
    else:
        _emit_text(
            "\n\n".join(
                format_utilization_delta_comparison(comparison) for comparison in comparisons
            )
        )


def _read_text_input(path: str, description: str) -> tuple[str, str]:
    if path == "-":
        return sys.stdin.read(), "stdin"
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(), path
    except OSError as e:
        raise XdbError(f"failed to read {description}: {path}") from e


def _run_vivado(args: argparse.Namespace) -> None:
    if args.vivado_cmd == "summarize-log":
        if args.max_items < 0:
            raise XdbError("--max-items must be >= 0")
        text, source = _read_text_input(args.log, "Vivado log")
        data = summarize_vivado_log_text(text, source=source)
        if data.get("looks_like_vivado_log") is False:
            print("not a Vivado log", file=sys.stderr)
            raise SystemExit(2)
        full_text = bool(args.full or args.debug)
        _emit_json(data) if args.json else _emit_text(
            format_vivado_log_summary(
                data,
                max_items=None if full_text else args.max_items,
                verbose=full_text,
            )
        )
        return
    if args.vivado_cmd == "ip-info":
        data = vivado_ip_info(
            args.path,
            include_all=bool(args.all),
            param_patterns=args.param,
        )
        _emit_json(data) if args.json else _emit_text(format_vivado_ip_info(data))
        return
    raise XdbError(f"unknown vivado command: {args.vivado_cmd}")


def _run_timing(args: argparse.Namespace) -> None:
    cmd = args.timing_cmd
    if cmd == "summary":
        data = timing_summary(
            args.path,
            dcp=args.dcp,
            reports=args.reports,
            max_paths=args.max_paths,
            timeout=args.timeout,
        )
        _emit_json(data) if args.json else _emit_text(format_timing_summary(data))
    elif cmd == "paths":
        data = timing_paths(
            args.path,
            dcp=args.dcp,
            max_paths=args.max_paths,
            delay_type=args.delay_type,
            timeout=args.timeout,
        )
        _emit_json(data) if args.json else _emit_text(format_timing_paths(data))
    elif cmd == "clocks":
        data = timing_clocks(args.path, dcp=args.dcp, reports=args.reports, timeout=args.timeout)
        _emit_json(data) if args.json else _emit_text(format_timing_clocks(data))
    elif cmd == "drc":
        data = timing_drc(args.path, dcp=args.dcp, reports=args.reports, timeout=args.timeout)
        _emit_json(data) if args.json else _emit_text(format_timing_drc(data))
    elif cmd == "net":
        data = timing_net(
            args.path,
            dcp=args.dcp,
            net=args.net,
            log=args.log,
            reports=args.reports,
            timeout=args.timeout,
        )
        _emit_json(data) if args.json else _emit_text(format_timing_net(data))
    elif cmd == "triage":
        data = timing_triage(
            args.path,
            dcp=args.dcp,
            reports=args.reports,
            log=args.log,
            max_paths=args.max_paths,
            hierarchy_depth=args.hierarchy_depth,
            timeout=args.timeout,
        )
        _emit_json(data) if args.json else _emit_text(format_timing_triage(data))
    elif cmd == "compare":
        data = timing_compare(
            args.old,
            args.new,
            old_name=args.old_name,
            new_name=args.new_name,
            hierarchy_depth=args.hierarchy_depth,
            timeout=args.timeout,
        )
        _emit_json(data) if args.json else _emit_text(format_timing_compare(data))
    else:
        raise XdbError(f"unknown timing command: {cmd}")


def main() -> None:  # pyright: ignore[reportGeneralTypeIssues]
    try:
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    except (AttributeError, ValueError):
        pass
    p = build_parser()
    args = p.parse_args()
    if not hasattr(args, "debug"):
        args.debug = False
    set_config_file(args.config)
    _configure_diagnostics(bool(args.debug))

    try:
        if args.cmd == "_simd":
            run_daemon(
                anchor_dir=args.anchor_dir,
                session_name=args.session,
                project=args.project,
                simset=args.simset,
                mode=args.mode,
                top=args.top,
                package_runtime=args.package_runtime,
                runtime_root=args.runtime_root,
                work_dir=args.work_dir,
                compile_script=args.compile_script,
                elaborate_script=args.elaborate_script,
                simulate_script=args.simulate_script,
            )
            return

        if args.cmd == "hls":
            if args.hls_cmd == "sim":
                result = run_hls_sim(
                    args.package_runtime,
                    workspace=args.workspace,
                    case_name=args.case,
                    all_cases=bool(args.all),
                    continue_on_failure=bool(args.continue_on_failure),
                    timeout_seconds=_validate_positive_timeout_seconds(args.timeout),
                    force_restage=bool(args.restage),
                )
                if args.summary:
                    _emit_text(format_hls_sim_summary(result))
                else:
                    _print(result)
                if not result.get("ok", False):
                    raise SystemExit(130 if result.get("status") == "interrupted" else 1)
            elif args.hls_cmd == "provenance":
                result = hls_provenance(
                    args.package_runtime,
                    workspace=args.workspace,
                    case_name=args.case,
                )
                if args.summary:
                    _emit_text(format_hls_provenance_summary(result))
                else:
                    _print(result)
            elif args.hls_cmd == "doctor":
                result = hls_doctor(
                    args.package_runtime,
                    workspace=args.workspace,
                    case_name=args.case,
                )
                if args.summary:
                    _emit_text(format_hls_doctor_summary(result))
                else:
                    _print(result)
                if not result.get("ok", False):
                    raise SystemExit(1)
            elif args.hls_cmd == "bundle":
                _print(
                    create_hls_bundle(
                        args.package_runtime,
                        workspace=args.workspace,
                        out=args.out,
                        max_bytes=args.max_bytes,
                    )
                )
            else:
                p.error("unknown hls command")
            return

        if args.cmd == "sim":
            if args.sim_cmd == "launch":
                _print(
                    launch_session(
                        simset=args.simset,
                        mode=args.mode,
                        top=args.top,
                        session_name=args.session,
                        replace=args.replace,
                        timeout=args.timeout,
                        package_runtime=args.package_runtime,
                    )
                )
            elif args.sim_cmd == "relaunch":
                _print(
                    relaunch_session(
                        simset=args.simset,
                        mode=args.mode,
                        top=args.top,
                        session_name=args.session,
                        timeout=args.timeout,
                        fresh=bool(args.fresh),
                    )
                )
            elif args.sim_cmd == "restage":
                _print(restage_session(args.session))
            elif args.sim_cmd == "provenance":
                result = provenance_session(args.session)
                if args.summary:
                    _emit_text(_format_provenance_summary(result))
                else:
                    _print(result)
            elif args.sim_cmd == "doctor":
                result = doctor_session(
                    args.session,
                    timeout_seconds=_validate_positive_timeout_seconds(args.timeout) or 1.0,
                )
                if args.summary:
                    _emit_text(_format_doctor_summary(result))
                else:
                    _print(result)
            elif args.sim_cmd == "run":
                _print(
                    run_session(
                        args.session,
                        args.time,
                        timeout_seconds=_validate_positive_timeout_seconds(args.timeout),
                    )
                )
            elif args.sim_cmd == "restart":
                _print(restart_session(args.session))
            elif args.sim_cmd == "close":
                _print(
                    close_session(
                        args.session,
                        force=bool(args.force),
                        timeout_seconds=_validate_positive_timeout_seconds(args.timeout),
                    )
                )
            elif args.sim_cmd == "time":
                _print(time_session(args.session))
            elif args.sim_cmd == "status":
                _print(status_session(args.session))
            elif args.sim_cmd == "describe":
                _print(describe_session(args.session))
            elif args.sim_cmd == "get":
                _print(get_signal(args.session, args.signal))
            elif args.sim_cmd == "get-many":
                _print(get_many_signals(args.session, args.pattern))
            elif args.sim_cmd == "read":
                _print(read_signals(args.session, args.signals))
            elif args.sim_cmd == "scopes":
                _print(get_scopes(args.session, args.scope))
            elif args.sim_cmd == "objects":
                _print(get_objects(args.session, args.scope))
            elif args.sim_cmd == "top":
                _print(set_top(args.session, args.module))
            elif args.sim_cmd == "snapshot":
                _print(snapshot_session(args.session, args.scope, name=args.name))
            elif args.sim_cmd == "diff-snapshot":
                _print(diff_snapshot_session(args.session, args.before, args.after))
            elif args.sim_cmd == "watch-changes":
                _print(
                    watch_changes_session(
                        args.session,
                        args.scope,
                        _resolve_sim_step_tokens(args.duration),
                    )
                )
            elif args.sim_cmd == "wave" and args.sim_wave_cmd == "add":
                _print(add_wave(args.session, args.pattern))
            elif args.sim_cmd == "vcd" and args.sim_vcd_cmd == "start":
                _print(vcd_start_session(args.session, args.file, args.scope))
            elif args.sim_cmd == "vcd" and args.sim_vcd_cmd == "stop":
                _print(vcd_stop_session(args.session))
            elif args.sim_cmd == "vcd" and args.sim_vcd_cmd == "status":
                _print(vcd_status_session(args.session))
            elif args.sim_cmd == "step":
                _print(step_session(args.session, _join_tokens(args.arg)))
            elif args.sim_cmd in {"until", "wait", "wait-on-condition"}:
                _print(
                    wait_until_session(
                        args.session,
                        _join_tokens(args.expr) or "",
                        _resolve_sim_step_tokens(args.step),
                        timeout_seconds=_validate_positive_timeout_seconds(args.timeout),
                        max_iterations=_validate_positive_iteration_limit(args.max_iterations),
                    )
                )
            elif args.sim_cmd in {"until-signal", "wait-signal", "wait-on-signal"}:
                _print(
                    wait_until_signal_session(
                        args.session,
                        args.signal,
                        args.value,
                        _resolve_sim_step_tokens(args.step),
                        timeout_seconds=_validate_positive_timeout_seconds(args.timeout),
                        max_iterations=_validate_positive_iteration_limit(args.max_iterations),
                    )
                )
            elif args.sim_cmd == "assert-signal":
                _print(assert_signal_session(args.session, args.signal, args.value))
            elif args.sim_cmd == "assert-tcl":
                _print(assert_tcl_session(args.session, _join_tokens(args.expr) or ""))
            elif args.sim_cmd == "expect-signal":
                _print(
                    expect_signal_session(
                        args.session,
                        args.signal,
                        args.value,
                        _resolve_sim_step_tokens(args.within),
                    )
                )
            elif args.sim_cmd == "expect-change":
                _print(
                    expect_change_session(
                        args.session,
                        args.signal,
                        _resolve_sim_step_tokens(args.within),
                    )
                )
            elif args.sim_cmd == "expect-condition":
                _print(
                    expect_condition_session(
                        args.session,
                        _join_tokens(args.expr) or "",
                        _resolve_sim_step_tokens(args.within),
                    )
                )
            elif args.sim_cmd == "expect-stream-output":
                _print(
                    expect_stream_output_session(
                        args.session,
                        args.path,
                        _resolve_sim_step_tokens(args.within),
                        step_tokens=_resolve_sim_step_tokens(args.step),
                        decode_bytes=bool(args.decode_bytes),
                        lane_order=args.lane_order,
                    )
                )
            elif args.sim_cmd == "breakpoint" and args.sim_bp_cmd == "add":
                _print(
                    add_breakpoint(
                        args.session,
                        _join_tokens(args.condition) or "",
                        poll_step_tokens=None if args.poll_step is None else args.poll_step.split(),
                    )
                )
            elif args.sim_cmd == "breakpoint" and args.sim_bp_cmd == "list":
                _print(list_breakpoints(args.session))
            elif args.sim_cmd == "breakpoint" and args.sim_bp_cmd == "remove":
                _print(remove_breakpoint(args.session, args.breakpoint_id))
            elif args.sim_cmd == "breakpoint" and args.sim_bp_cmd == "clear":
                _print(clear_breakpoints(args.session))
            elif args.sim_cmd == "tcl":
                _print(tcl_session(args.session, _resolve_tcl_script(args.script, args.file)))
            elif args.sim_cmd == "source":
                _print(source_session(args.session, args.path))
            elif args.sim_cmd == "force":
                _print(
                    force_session(
                        args.session,
                        args.signal,
                        args.values,
                        radix=args.radix,
                        repeat_every=args.repeat_every,
                        cancel_after=args.cancel_after,
                    )
                )
            elif args.sim_cmd == "axis" and args.sim_axis_cmd == "trace":
                profile = _load_cli_trace_profile(args.profile, args.profile_file)
                profile_config = profile["config"]
                profile_paths = _profile_string_list(profile_config, "axis", "axis_paths", "paths")
                axis_paths = [*profile_paths, *list(args.paths or [])]
                if not axis_paths:
                    raise XdbError("missing AXIS interface path")
                duration_tokens = args.duration or _profile_tokens(
                    profile_config, "duration", "for"
                )
                if duration_tokens is None:
                    raise XdbError(
                        "missing AXIS trace duration: pass --for or use a profile with 'duration'"
                    )
                step_tokens = args.step or _profile_tokens(profile_config, "step") or ["1", "ns"]
                result = axis_trace_session(
                    args.session,
                    axis_paths,
                    _resolve_sim_step_tokens(duration_tokens),
                    step_tokens=_resolve_sim_step_tokens(step_tokens),
                    decode_bytes=_profile_bool(profile_config, args.decode_bytes, "decode_bytes"),
                    lane_order=_profile_str(
                        profile_config, args.lane_order, "lane_order", "low-to-high"
                    ),
                    include_idle=_profile_bool(profile_config, args.include_idle, "include_idle"),
                    only_handshakes=_profile_bool(
                        profile_config, args.only_handshakes, "only_handshakes"
                    ),
                )
                if args.profile:
                    result["profile"] = {"name": profile["name"], "source": profile["source"]}
                if args.ndjson:
                    _emit_text(
                        "\n".join(
                            json.dumps(record, sort_keys=False)
                            for record in list(result.get("records") or [])
                            if isinstance(record, dict)
                        ),
                        args.out,
                    )
                else:
                    _emit_json(result, args.out)
            elif args.sim_cmd == "trace" and args.sim_trace_cmd == "transactions":
                _emit_json(
                    trace_transactions_session(
                        args.session,
                        _resolve_sim_step_tokens(args.duration),
                        opcode=args.opcode,
                    ),
                    args.out,
                )
            elif args.sim_cmd == "trace" and args.sim_trace_cmd == "profiles":
                _print(list_trace_profiles(args.profile_file))
            elif args.sim_cmd == "exec":
                _print(
                    exec_session(
                        args.session,
                        args.command,
                        cwd=args.cwd,
                        env_overrides=list(args.env_overrides or []),
                        timeout_seconds=args.timeout,
                        expect_exit_code=args.expect_exit_code,
                        clean_env=bool(args.clean_env),
                        stream_output=bool(args.stream),
                    )
                )
            elif args.sim_cmd == "with-trace":
                profile = _load_cli_trace_profile(args.profile, args.profile_file)
                profile_config = profile["config"]
                profile_axis_paths = _profile_string_list(profile_config, "axis", "axis_paths")
                duration_tokens = (
                    args.duration or _profile_tokens(profile_config, "duration", "for") or []
                )
                step_tokens = args.step or _profile_tokens(profile_config, "step") or ["1", "ns"]
                correlate_window_tokens = args.correlate_window or _profile_tokens(
                    profile_config, "correlate_window", "correlation_window"
                )
                result = with_trace_session(
                    args.session,
                    args.command,
                    [] if not duration_tokens else _resolve_sim_step_tokens(duration_tokens),
                    step_tokens=_resolve_sim_step_tokens(step_tokens),
                    transactions=_profile_bool(profile_config, args.transactions, "transactions"),
                    axis_paths=[*profile_axis_paths, *list(args.axis_paths or [])],
                    decode_bytes=_profile_bool(profile_config, args.decode_bytes, "decode_bytes"),
                    lane_order=_profile_str(
                        profile_config, args.lane_order, "lane_order", "low-to-high"
                    ),
                    include_idle=_profile_bool(profile_config, args.include_idle, "include_idle"),
                    only_handshakes=_profile_bool(
                        profile_config, args.only_handshakes, "only_handshakes"
                    ),
                    correlate_by=_profile_str(
                        profile_config, args.correlate_by, "correlate_by", "nearest"
                    ),
                    correlate_window_tokens=(
                        None
                        if correlate_window_tokens is None
                        else _resolve_sim_step_tokens(correlate_window_tokens)
                    ),
                    exec_mode=bool(args.exec_mode),
                    exec_until_exit=bool(args.exec_until_exit),
                    exec_cwd=args.cwd,
                    exec_env_overrides=list(args.exec_env_overrides or []),
                    exec_timeout_seconds=args.timeout,
                    exec_expect_exit_code=args.expect_exit_code,
                    exec_clean_env=bool(args.clean_env),
                    exec_stream_output=bool(args.stream),
                )
                if args.profile:
                    result["profile"] = {"name": profile["name"], "source": profile["source"]}
                if args.bundle is not None:
                    bundle = create_sim_bundle(
                        args.session,
                        out=args.bundle or None,
                        trace_result=result,
                    )
                    result["bundle"] = bundle
                if args.ndjson:
                    _emit_text(_format_with_trace_ndjson(result), args.out)
                elif args.summary:
                    _emit_text(_format_with_trace_summary(result), args.out)
                else:
                    _emit_json(result, args.out)
            elif args.sim_cmd == "bundle":
                _print(create_sim_bundle(args.session, out=args.out))
            elif args.sim_cmd == "release":
                _print(release_session(args.session, args.signal, all_forces=args.all))
            elif args.sim_cmd == "csr" and args.sim_csr_cmd == "read":
                _print(
                    coyote_csr_read_session(
                        args.session,
                        _parse_int(args.addr, what="CSR address"),
                        timeout_seconds=_validate_positive_timeout_seconds(args.timeout),
                    )
                )
            elif args.sim_cmd == "csr" and args.sim_csr_cmd == "write":
                _print(
                    coyote_csr_write_session(
                        args.session,
                        _parse_int(args.addr, what="CSR address"),
                        _parse_int(args.value, what="CSR value"),
                    )
                )
            elif args.sim_cmd == "service-csr" and args.sim_service_csr_cmd == "read":
                _print(
                    coyote_service_csr_read_session(
                        args.session,
                        _parse_int(args.addr, what="resident-service CSR address"),
                        timeout_seconds=_validate_positive_timeout_seconds(args.timeout),
                    )
                )
            elif args.sim_cmd == "service-csr" and args.sim_service_csr_cmd == "write":
                _print(
                    coyote_service_csr_write_session(
                        args.session,
                        _parse_int(args.addr, what="resident-service CSR address"),
                        _parse_int(args.value, what="resident-service CSR value"),
                    )
                )
            elif args.sim_cmd == "mem" and args.sim_mem_cmd == "map":
                _print(
                    coyote_mem_map_session(
                        args.session,
                        args.space,
                        _parse_int(args.addr, what="memory address"),
                        _parse_int(args.size, what="memory size"),
                    )
                )
            elif args.sim_cmd == "mem" and args.sim_mem_cmd == "unmap":
                _print(
                    coyote_mem_unmap_session(
                        args.session,
                        args.space,
                        _parse_int(args.addr, what="memory address"),
                    )
                )
            elif args.sim_cmd == "mem" and args.sim_mem_cmd == "list":
                _print(coyote_mem_list_session(args.session, args.space))
            elif args.sim_cmd == "mem" and args.sim_mem_cmd == "reset":
                _print(coyote_mem_reset_session(args.session, args.space))
            elif args.sim_cmd == "mem" and args.sim_mem_cmd == "write":
                _print(
                    coyote_mem_write_session(
                        args.session,
                        args.space,
                        _parse_int(args.addr, what="memory address"),
                        _resolve_mem_payload(args).hex(),
                    )
                )
            elif args.sim_cmd == "mem" and args.sim_mem_cmd == "read":
                _print(
                    coyote_mem_read_session(
                        args.session,
                        args.space,
                        _parse_int(args.addr, what="memory address"),
                        _parse_int(args.size, what="memory size"),
                    )
                )
            elif args.sim_cmd == "mem" and args.sim_mem_cmd == "dump":
                _print(
                    dump_memory_session(
                        args.session,
                        args.space,
                        _parse_int(args.addr, what="memory address"),
                        _parse_int(args.size, what="memory dump size"),
                        args.out,
                    )
                )
            elif args.sim_cmd == "mem" and args.sim_mem_cmd == "diff":
                _print(diff_memory_files(args.before, args.after))
            elif args.sim_cmd == "invoke":
                _print(
                    coyote_invoke_session(
                        args.session,
                        opcode=args.opcode,
                        addr=None if args.addr is None else _parse_int(args.addr, what="address"),
                        length=None
                        if args.length is None
                        else _parse_int(args.length, what="length"),
                        stream_name=args.stream,
                        dest=_parse_int(args.dest, what="destination stream"),
                        last=bool(args.last),
                        src_addr=None
                        if args.src_addr is None
                        else _parse_int(args.src_addr, what="source address"),
                        src_length=None
                        if args.src_len is None
                        else _parse_int(args.src_len, what="source length"),
                        src_stream_name=args.src_stream,
                        src_dest=_parse_int(args.src_dest, what="source destination stream"),
                        dst_addr=None
                        if args.dst_addr is None
                        else _parse_int(args.dst_addr, what="destination address"),
                        dst_length=None
                        if args.dst_len is None
                        else _parse_int(args.dst_len, what="destination length"),
                        dst_stream_name=args.dst_stream,
                        dst_dest=_parse_int(args.dst_dest, what="destination stream"),
                    )
                )
            elif args.sim_cmd == "completed":
                _print(
                    coyote_completed_session(
                        args.session,
                        args.opcode,
                        target_count=args.count,
                        timeout_seconds=_validate_positive_timeout_seconds(args.timeout),
                    )
                )
            elif args.sim_cmd == "clear-completed":
                _print(coyote_clear_completed_session(args.session))
            elif args.sim_cmd == "irq" and args.sim_irq_cmd == "wait":
                _print(
                    coyote_irq_wait_session(
                        args.session,
                        timeout_seconds=_validate_positive_timeout_seconds(args.timeout),
                    )
                )
            elif args.sim_cmd == "coyote-status":
                _print(coyote_status_session(args.session))
            else:
                p.error("unknown sim command")
            return

        if args.cmd == "reports":
            if args.reports_cmd in {"utilization", "util"}:
                _run_reports_utilization(args)
            elif args.reports_cmd == "compare":
                _run_reports_compare(args)
            elif args.reports_cmd == "cips":
                _run_reports_cips(args)
            elif args.reports_cmd == "floorplan":
                _run_reports_floorplan(args)
            else:
                p.error("unknown reports command")
            return

        if args.cmd == "util":
            _run_reports_utilization(args)
            return

        if args.cmd == "timing":
            _run_timing(args)
            return

        if args.cmd == "vivado":
            _run_vivado(args)
            return

        if args.cmd == "hw-session":
            from xdb.hw_session import (
                close_hardware_session,
                hardware_session_status,
                launch_hardware_session,
            )

            if args.hw_session_cmd == "launch":
                _print(launch_hardware_session(args.name))
            elif args.hw_session_cmd == "status":
                _print(hardware_session_status(args.name))
            elif args.hw_session_cmd == "close":
                _print(close_hardware_session(args.name, force=args.force))
            else:
                p.error(f"unknown hw-session command: {args.hw_session_cmd}")
            return

        backend = select_backend()
        if args.cmd == "targets":
            _require_capability(
                backend,
                Capability.TARGETS,
                operation="targets",
                target_hint=_resolve_part_hint(args.part_hint),
            )
            _print(backend.list_targets(_resolve_part_hint(args.part_hint), timeout=args.timeout))
        elif args.cmd == "program":
            part_hint = _require_part_hint(args.part_hint)
            _require_capability(
                backend,
                Capability.PROGRAM,
                operation="program",
                target_hint=part_hint,
            )
            bit = _resolve_bitstream(args.bit)
            ltx = _resolve_optional_ltx(args.ltx)
            backend_result = backend.program(
                bit,
                ltx,
                part_hint,
                timeout=args.timeout,
            )
            result = dict(backend_result)
            result["backend"] = backend.name
            result["bitstream"] = bit
            result["bitstream_sha256"] = _sha256_file(bit)
            result["ltx"] = ltx
            _print(result)
        elif args.cmd == "ilas":
            part_hint = _require_part_hint(args.part_hint)
            _require_capability(
                backend,
                Capability.ILA_LIST,
                operation="ilas",
                target_hint=part_hint,
            )
            _print(
                backend.list_ilas(
                    part_hint,
                    timeout=args.timeout,
                    ltx=_resolve_optional_ltx(args.ltx),
                )
            )
        elif args.cmd == "ila":
            part_hint = _require_part_hint(args.part_hint)
            _require_capability(
                backend,
                Capability.ILA_CONTROL,
                operation=f"ila {args.ila_cmd}",
                target_hint=part_hint,
            )
            ltx = _resolve_optional_ltx(args.ltx)
            if args.ila_cmd == "arm":
                samples = _validate_samples(args.samples)
                windows = _validate_windows(args.windows)
                trigger_position = _validate_trigger_position(args.trigger_position, samples)
                triggers = _parse_probe_triggers(args.trigger)
                capture_values = _parse_probe_triggers(args.capture_value)
                tsm_path = _resolve_tsm(args.tsm)
                if tsm_path and triggers:
                    raise XdbError("--tsm and --trigger are mutually exclusive")
                advanced_requested = bool(
                    tsm_path
                    or capture_values
                    or args.trigger_condition != "and"
                    or args.capture_condition != "and"
                    or args.trig_in != "disabled"
                    or args.trig_out != "disabled"
                )
                advanced_trigger: IlaTriggerConfig | None = None
                if advanced_requested:
                    _require_capability(
                        backend,
                        Capability.ILA_ADVANCED_TRIGGER,
                        operation="ila arm advanced trigger",
                        target_hint=part_hint,
                    )
                    advanced_trigger = {
                        "trigger_condition": args.trigger_condition,
                        "capture_condition": args.capture_condition,
                        "capture_values": capture_values,
                        "trig_in": args.trig_in,
                        "trig_out": args.trig_out,
                    }
                    if tsm_path is not None:
                        advanced_trigger["tsm_path"] = tsm_path
                if windows != 1:
                    _require_capability(
                        backend,
                        Capability.ILA_MULTI_WINDOW_CAPTURE,
                        operation="ila arm --windows",
                        target_hint=part_hint,
                    )
                if trigger_position is not None:
                    _require_capability(
                        backend,
                        Capability.ILA_CAPTURE_POSITION,
                        operation="ila arm --trigger-position",
                        target_hint=part_hint,
                    )
                if triggers:
                    _require_capability(
                        backend,
                        Capability.ILA_BASIC_TRIGGER,
                        operation="ila arm --trigger",
                        target_hint=part_hint,
                    )
                result = backend.arm_ila(
                    part_hint,
                    args.ila,
                    samples,
                    timeout=args.timeout,
                    ltx=ltx,
                    windows=windows,
                    trigger_position=trigger_position,
                    triggers=triggers,
                    advanced_trigger=advanced_trigger,
                )
            elif args.ila_cmd == "compile-trigger":
                _require_capability(
                    backend,
                    Capability.ILA_ADVANCED_TRIGGER,
                    operation="ila compile-trigger",
                    target_hint=part_hint,
                )
                result = backend.compile_ila_trigger(
                    part_hint,
                    args.ila,
                    _resolve_tsm(args.tsm) or "",
                    timeout=args.timeout,
                    ltx=ltx,
                )
                if result["error_count"] != 0:
                    _print(result)
                    raise XdbError(
                        f"trigger state-machine compilation failed with {result['error_count']} errors"
                    )
            elif args.ila_cmd == "status":
                result = backend.ila_status(part_hint, args.ila, timeout=args.timeout, ltx=ltx)
            elif args.ila_cmd == "wait":
                result = backend.wait_ila(part_hint, args.ila, timeout=args.timeout, ltx=ltx)
            elif args.ila_cmd == "upload":
                result = backend.upload_ila(
                    part_hint, args.ila, args.csv, timeout=args.timeout, ltx=ltx
                )
            else:
                p.error(f"unknown ila command: {args.ila_cmd}")
            _print(result)
        elif args.cmd == "capture":
            part_hint = _require_part_hint(args.part_hint)
            _require_capability(
                backend,
                Capability.ILA_BASIC_CAPTURE,
                operation="capture",
                target_hint=part_hint,
            )
            samples = _validate_samples(args.samples)
            windows = _validate_windows(args.windows)
            trigger_position = _validate_trigger_position(args.trigger_position, samples)
            triggers = _parse_probe_triggers(args.trigger)
            if windows != 1:
                _require_capability(
                    backend,
                    Capability.ILA_MULTI_WINDOW_CAPTURE,
                    operation="capture --windows",
                    target_hint=part_hint,
                )
            if trigger_position is not None:
                _require_capability(
                    backend,
                    Capability.ILA_CAPTURE_POSITION,
                    operation="capture --trigger-position",
                    target_hint=part_hint,
                )
            if triggers:
                _require_capability(
                    backend,
                    Capability.ILA_BASIC_TRIGGER,
                    operation="capture --trigger",
                    target_hint=part_hint,
                )
            _print(
                backend.capture(
                    part_hint,
                    args.ila,
                    args.csv,
                    samples,
                    timeout=args.timeout,
                    ltx=_resolve_optional_ltx(args.ltx),
                    windows=windows,
                    trigger_position=trigger_position,
                    triggers=triggers,
                )
            )
        elif args.cmd == "instruments" and args.instruments_cmd == "list":
            part_hint = _require_part_hint(args.part_hint)
            _require_capability(
                backend,
                Capability.INSTRUMENTS_LIST,
                operation="instruments list",
                target_hint=part_hint,
            )
            _print(backend.list_instruments(part_hint, timeout=args.timeout))
        else:
            p.error(f"unknown command: {args.cmd}")
    except XdbError as e:
        _print_error(str(e))
        if args.debug:
            traceback.print_exc()
        sys.exit(2)
    except KeyboardInterrupt:
        _print_error("interrupted")
        if args.debug:
            traceback.print_exc()
        sys.exit(130)
    except Exception as e:
        if args.debug:
            traceback.print_exc()
        else:
            _print_error(f"unexpected internal error: {e}. Re-run with --debug for a traceback.")
        sys.exit(2)


if __name__ == "__main__":
    main()
