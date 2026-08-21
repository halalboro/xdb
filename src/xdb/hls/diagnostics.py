from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Mapping, cast

from xdb.errors import XdbError
from xdb.hls.runner import _execute_process, load_last_hls_result
from xdb.hls.runtime import (
    CONTROL_DIR,
    HlsRuntime,
    is_hls_stage_stamp,
    manifest_summary,
    read_stage_stamp,
    resolve_hls_runtime,
    select_hls_cases,
)


def _check(
    name: str,
    ok: bool,
    *,
    severity: str = "error",
    detail: str = "",
    data: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {"name": name, "ok": ok, "severity": severity}
    if detail:
        result["detail"] = detail
    if data is not None:
        result["data"] = dict(data)
    return result


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        proc_stat = Path(f"/proc/{pid}/stat")
        if proc_stat.is_file():
            state = proc_stat.read_text(encoding="utf-8").rsplit(")", 1)[1].strip()[0]
            if state == "Z":
                return False
        os.kill(pid, 0)
    except (OSError, IndexError):
        return False
    return True


def _load_json_object(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _reported_environment(runtime: HlsRuntime, case_name: str | None) -> dict[str, str]:
    manifest = runtime.manifest
    result = {key: os.environ[key] for key in manifest.environment.passed if key in os.environ}
    result.update(manifest.environment.injected)
    result.update(
        {
            "HOME": str(runtime.workspace / CONTROL_DIR / "home"),
            "TMPDIR": str(runtime.workspace / CONTROL_DIR / "tmp"),
            "XDB_HLS_PACKAGE_RUNTIME": str(runtime.package_root),
            "XDB_HLS_WORKSPACE": str(runtime.workspace),
            "XDB_HLS_PROJECT": manifest.project,
            "XDB_HLS_TOP": manifest.top,
        }
    )
    if case_name is not None:
        result["XDB_HLS_CASE"] = case_name
    return dict(sorted(result.items()))


def _selected_case_name(runtime: HlsRuntime, case_name: str | None) -> str:
    return select_hls_cases(
        runtime.manifest,
        case_name=case_name,
        all_cases=False,
    )[0].name


def hls_provenance(
    package: str | None,
    *,
    workspace: str | None = None,
    case_name: str | None = None,
) -> dict[str, Any]:
    runtime = resolve_hls_runtime(package, workspace=workspace, stage=False)
    selected = _selected_case_name(runtime, case_name)
    case = runtime.manifest.case_map()[selected]
    stamp = read_stage_stamp(runtime.workspace)
    last_result = load_last_hls_result(runtime)
    expected_exit = (
        case.expected_exit_code
        if case.expected_exit_code is not None
        else runtime.manifest.expected_exit_code
    )
    marker = case.success_marker or runtime.manifest.success_marker
    return {
        "schema": "xdb-hls-csim-provenance-v1",
        "package_runtime": str(runtime.package_root),
        "package_fingerprint": runtime.package_fingerprint,
        "manifest_path": str(runtime.manifest.path),
        "workspace": str(runtime.workspace),
        "workspace_exists": runtime.workspace.is_dir(),
        "workspace_fresh": not runtime.needs_stage,
        "stage_stamp": stamp,
        "manifest": manifest_summary(runtime.manifest),
        "selected_case": selected,
        "effective": {
            "argv": [
                str((runtime.workspace / runtime.manifest.run.path).resolve()),
                *runtime.manifest.run.args,
                *case.args,
            ],
            "cwd": str(runtime.workspace),
            "timeout_seconds": runtime.manifest.default_timeout_seconds,
            "expected_exit_code": expected_exit,
            "success_marker": marker,
            "environment": _reported_environment(runtime, selected),
        },
        "observed_tool_version": None
        if last_result is None or not isinstance(last_result.get("tool"), dict)
        else last_result["tool"].get("observed_version"),
        "last_result": None
        if last_result is None
        else {
            "run_id": last_result.get("run_id"),
            "ok": last_result.get("ok"),
            "status": last_result.get("status"),
            "selected_cases": last_result.get("selected_cases"),
            "started_at": last_result.get("started_at"),
            "finished_at": last_result.get("finished_at"),
            "result_path": last_result.get("result_path"),
            "cases": last_result.get("cases"),
        },
    }


def _doctor_tool_probe(runtime: HlsRuntime) -> dict[str, Any]:
    manifest = runtime.manifest
    reported = _reported_environment(runtime, None)
    environment = dict(reported)
    with tempfile.TemporaryDirectory(prefix="xdb-hls-doctor-") as temporary:
        root = Path(temporary)
        environment["HOME"] = str(root / "home")
        environment["TMPDIR"] = str(root / "tmp")
        Path(environment["HOME"]).mkdir()
        Path(environment["TMPDIR"]).mkdir()
        process = _execute_process(
            [manifest.tool.executable, *manifest.tool.version_args],
            cwd=root,
            environment=environment,
            reported_environment=environment,
            timeout_seconds=10.0,
            expected_exit_code=0,
            stdout_path=root / "stdout.log",
            stderr_path=root / "stderr.log",
            success_marker=None,
        )
        text = (root / "stdout.log").read_text(encoding="utf-8", errors="replace") + (
            root / "stderr.log"
        ).read_text(encoding="utf-8", errors="replace")
    match = re.search(manifest.tool.version_regex, text)
    observed = match.group(1) if match is not None else None
    return {
        "process_status": process["status"],
        "ok": bool(process["ok"]) and observed == manifest.tool.version,
        "expected_version": manifest.tool.version,
        "observed_version": observed,
        "version_matches": observed == manifest.tool.version,
        "error": process.get("launch_error"),
    }


def hls_doctor(
    package: str | None,
    *,
    workspace: str | None = None,
    case_name: str | None = None,
    probe_tool: bool = True,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    suggestions: list[str] = []
    try:
        runtime = resolve_hls_runtime(package, workspace=workspace, stage=False)
    except XdbError as error:
        checks.append(_check("runtime_manifest", False, detail=str(error)))
        return {
            "schema": "xdb-hls-csim-doctor-v1",
            "ok": False,
            "checks": checks,
            "suggestions": ["fix the packaged runtime manifest before running xdb hls sim"],
            "runtime": None,
            "last_result": None,
        }

    checks.append(
        _check(
            "runtime_manifest",
            True,
            data={
                "path": str(runtime.manifest.path),
                "runtime_kind": runtime.manifest.runtime_kind,
                "schema_version": runtime.manifest.schema_version,
            },
        )
    )
    workspace_nonempty = runtime.workspace.is_dir() and any(runtime.workspace.iterdir())
    workspace_owned = not workspace_nonempty or is_hls_stage_stamp(
        read_stage_stamp(runtime.workspace)
    )
    checks.append(
        _check(
            "workspace_owned",
            workspace_owned,
            detail="selected nonempty workspace was not staged by xdb"
            if not workspace_owned
            else "",
            data={"workspace": str(runtime.workspace)},
        )
    )
    checks.append(
        _check(
            "workspace_fresh",
            not runtime.needs_stage,
            severity="warning",
            detail="workspace is absent or stale" if runtime.needs_stage else "",
            data={"workspace": str(runtime.workspace)},
        )
    )
    if not workspace_owned:
        suggestions.append("select a new/empty --workspace; do not delete unrelated files")
    elif runtime.needs_stage:
        suggestions.append(
            f"run: xdb hls sim {runtime.package_root} --workspace {runtime.workspace} --restage"
        )

    selected: str | None = None
    try:
        selected = _selected_case_name(runtime, case_name)
        checks.append(_check("selected_case", True, data={"case": selected}))
    except XdbError as error:
        checks.append(_check("selected_case", False, detail=str(error)))

    path_value = runtime.manifest.environment.injected.get("PATH")
    if path_value is None and "PATH" in runtime.manifest.environment.passed:
        path_value = os.environ.get("PATH")
    executable = shutil.which(runtime.manifest.tool.executable, path=path_value or "")
    checks.append(
        _check(
            "tool_executable",
            executable is not None,
            detail=f"{runtime.manifest.tool.executable} is not available in the permitted PATH"
            if executable is None
            else "",
            data={"executable": runtime.manifest.tool.executable, "resolved": executable or ""},
        )
    )
    tool_probe: dict[str, Any] | None = None
    if executable is None:
        suggestions.append("enter the consuming project's flake-provided Xilinx HLS shell")
    elif probe_tool:
        tool_probe = _doctor_tool_probe(runtime)
        checks.append(
            _check(
                "tool_version",
                bool(tool_probe["ok"]),
                detail=(
                    f"expected {tool_probe['expected_version']}, observed "
                    f"{tool_probe['observed_version'] or 'unparseable'}"
                )
                if not tool_probe["ok"]
                else "",
                data=tool_probe,
            )
        )
        if not tool_probe["ok"]:
            suggestions.append(
                f"enter the shell providing {runtime.manifest.tool.family} "
                f"{runtime.manifest.tool.version}"
            )

    active = _load_json_object(runtime.active_path)
    if active is not None:
        active_pid = int(active.get("pid") or 0)
        alive = _pid_alive(active_pid)
        checks.append(
            _check(
                "active_run",
                False,
                severity="error" if alive else "warning",
                detail="an HLS C-simulation run is active"
                if alive
                else "stale active-run metadata indicates an interrupted process",
                data={"pid": active_pid, "alive": alive, "run_id": active.get("run_id")},
            )
        )
        if not alive:
            suggestions.append(f"remove stale metadata after inspection: {runtime.active_path}")

    last_result = load_last_hls_result(runtime)
    if last_result is None:
        checks.append(
            _check(
                "last_result",
                False,
                severity="warning",
                detail="no prior HLS C-simulation result is available",
            )
        )
    else:
        checks.append(
            _check(
                "last_result",
                True,
                data={
                    "run_id": last_result.get("run_id"),
                    "ok": bool(last_result.get("ok")),
                    "status": str(last_result.get("status") or ""),
                },
            )
        )
        status = str(last_result.get("status") or "")
        if "timed_out" in status or status == "interrupted":
            checks.append(
                _check(
                    "prior_abnormal_termination",
                    False,
                    severity="warning",
                    detail=f"the prior run ended with status {status}",
                )
            )
            suggestions.append(
                "inspect the retained result logs or create: xdb hls bundle --out <path>"
            )
        log_paths: list[Path] = []
        for section in (last_result.get("tool"), last_result.get("prepare")):
            if isinstance(section, dict):
                log_paths.extend(
                    Path(str(section[key]))
                    for key in ("stdout_path", "stderr_path")
                    if section.get(key)
                )
        for case in list(last_result.get("cases") or []):
            if isinstance(case, dict):
                log_paths.extend(
                    Path(str(case[key])) for key in ("stdout_path", "stderr_path") if case.get(key)
                )
        missing_logs = sorted(str(path) for path in log_paths if not path.is_file())
        checks.append(
            _check(
                "result_logs",
                not missing_logs,
                severity="warning",
                detail="one or more prior result logs are missing" if missing_logs else "",
                data={"missing": missing_logs},
            )
        )

    unique_suggestions: list[str] = []
    for suggestion in suggestions:
        if suggestion not in unique_suggestions:
            unique_suggestions.append(suggestion)
    ok = all(check.get("ok") or check.get("severity") != "error" for check in checks)
    return {
        "schema": "xdb-hls-csim-doctor-v1",
        "ok": ok,
        "checks": checks,
        "suggestions": unique_suggestions,
        "runtime": {
            "package_runtime": str(runtime.package_root),
            "package_fingerprint": runtime.package_fingerprint,
            "manifest_path": str(runtime.manifest.path),
            "workspace": str(runtime.workspace),
            "workspace_fresh": not runtime.needs_stage,
            "selected_case": selected,
            "tool_probe": tool_probe,
        },
        "last_result": last_result,
    }


def format_hls_provenance_summary(result: Mapping[str, Any]) -> str:
    manifest = cast(
        dict[str, Any], result.get("manifest") if isinstance(result.get("manifest"), dict) else {}
    )
    tool = cast(
        dict[str, Any], manifest.get("tool") if isinstance(manifest.get("tool"), dict) else {}
    )
    last = cast(
        dict[str, Any],
        result.get("last_result") if isinstance(result.get("last_result"), dict) else {},
    )
    return "\n".join(
        [
            "HLS C-simulation provenance",
            f"project: {manifest.get('project', '?')}  top: {manifest.get('top', '?')}",
            f"package: {result.get('package_runtime', '?')}",
            f"fingerprint: {result.get('package_fingerprint', '?')}",
            f"workspace: {result.get('workspace', '?')}",
            f"workspace fresh: {'yes' if result.get('workspace_fresh') else 'no'}",
            f"selected case: {result.get('selected_case', '?')}",
            f"tool: {tool.get('family', '?')} "
            f"{tool.get('version', '?')} "
            f"(observed {result.get('observed_tool_version') or 'n/a'})",
            f"last result: {last.get('status', 'n/a')} ({last.get('run_id', 'n/a')})",
        ]
    )


def format_hls_doctor_summary(result: Mapping[str, Any]) -> str:
    checks = [item for item in list(result.get("checks") or []) if isinstance(item, dict)]
    failures = [item for item in checks if not item.get("ok")]
    lines = [
        "HLS C-simulation doctor",
        f"ok: {'yes' if result.get('ok') else 'no'}",
        f"checks: {len(checks)} total, {len(failures)} issue(s)",
    ]
    for item in failures:
        detail = f" - {item.get('detail')}" if item.get("detail") else ""
        lines.append(f"  {item.get('severity', 'error')}: {item.get('name', '?')}{detail}")
    suggestions = [str(item) for item in list(result.get("suggestions") or [])]
    if suggestions:
        lines.append("suggestions:")
        lines.extend(f"  {item}" for item in suggestions)
    return "\n".join(lines)
