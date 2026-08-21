from __future__ import annotations

import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, cast

from xdb import __version__
from xdb.errors import XdbError
from xdb.hls.runtime import (
    HlsCase,
    HlsRuntime,
    RESULT_SCHEMA,
    manifest_summary,
    resolve_hls_runtime,
    select_hls_cases,
    stage_hls_runtime,
)


_TERMINATE_GRACE_SECONDS = 2.0
_VERSION_TIMEOUT_SECONDS = 30.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run_id() -> str:
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    return f"{now}-{os.getpid()}-{time.time_ns() % 1_000_000:06d}"


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(data, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _read_text(path: Path, *, max_bytes: int | None = None) -> str:
    try:
        with path.open("rb") as stream:
            if max_bytes is not None:
                data = stream.read(max_bytes + 1)
                if len(data) > max_bytes:
                    data = data[:max_bytes]
            else:
                data = stream.read()
    except OSError:
        return ""
    return data.decode("utf-8", errors="replace")


def _linux_descendants(parent_pid: int) -> list[int]:
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return []
    children: dict[int, list[int]] = {}
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat_text = (entry / "stat").read_text(encoding="utf-8")
            after_name = stat_text.rsplit(")", 1)[1].strip().split()
            pid = int(entry.name)
            ppid = int(after_name[1])
        except (OSError, ValueError, IndexError):
            continue
        children.setdefault(ppid, []).append(pid)
    descendants: list[int] = []
    pending = list(children.get(parent_pid, []))
    while pending:
        pid = pending.pop()
        descendants.append(pid)
        pending.extend(children.get(pid, []))
    return descendants


def _file_contains_marker(path: Path, marker: str) -> bool:
    needle = marker.encode("utf-8")
    overlap = b""
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(64 * 1024), b""):
                data = overlap + chunk
                if needle in data:
                    return True
                overlap = data[-(len(needle) - 1) :] if len(needle) > 1 else b""
    except OSError:
        return False
    return False


def _process_group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    pgid = process.pid
    descendants = _linux_descendants(process.pid)
    for pid in reversed(descendants):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    if descendants:
        time.sleep(0.1)
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError:
        try:
            process.terminate()
        except OSError:
            return
    try:
        process.wait(timeout=_TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass
    if _process_group_exists(pgid):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            try:
                process.kill()
            except OSError:
                pass
    try:
        process.wait(timeout=_TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            pass
        process.wait()


@contextmanager
def _translate_termination_to_interrupt() -> Iterator[None]:
    def interrupt(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt

    try:
        previous = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, interrupt)
    except (AttributeError, ValueError):
        previous = None
    try:
        yield
    finally:
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)


def _effective_environment(
    runtime: HlsRuntime, case_name: str | None
) -> tuple[dict[str, str], dict[str, str]]:
    manifest = runtime.manifest
    environment: dict[str, str] = {}
    reported: dict[str, str] = {}
    for key in manifest.environment.passed:
        if key in os.environ:
            environment[key] = os.environ[key]
            reported[key] = os.environ[key]
    for key, value in manifest.environment.injected.items():
        environment[key] = value
        reported[key] = value

    home = runtime.control_dir / "home"
    temporary = runtime.control_dir / "tmp"
    home.mkdir(parents=True, exist_ok=True)
    temporary.mkdir(parents=True, exist_ok=True)
    managed = {
        "HOME": str(home),
        "TMPDIR": str(temporary),
        "XDB_HLS_PACKAGE_RUNTIME": str(runtime.package_root),
        "XDB_HLS_WORKSPACE": str(runtime.workspace),
        "XDB_HLS_PROJECT": manifest.project,
        "XDB_HLS_TOP": manifest.top,
    }
    if case_name is not None:
        managed["XDB_HLS_CASE"] = case_name
    environment.update(managed)
    reported.update(managed)
    return environment, reported


def _execute_process(
    argv: list[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    reported_environment: Mapping[str, str],
    timeout_seconds: float,
    expected_exit_code: int,
    stdout_path: Path,
    stderr_path: Path,
    success_marker: str | None,
) -> dict[str, Any]:
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    started_at = _now_iso()
    started = time.monotonic()
    timed_out = False
    interrupted = False
    launch_error: str | None = None
    process: subprocess.Popen[bytes] | None = None
    with stdout_path.open("wb") as stdout_stream, stderr_path.open("wb") as stderr_stream:
        try:
            process = subprocess.Popen(
                argv,
                cwd=cwd,
                env=dict(environment),
                stdout=stdout_stream,
                stderr=stderr_stream,
                start_new_session=True,
            )
            try:
                process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                _terminate_process_group(process)
            except KeyboardInterrupt:
                interrupted = True
                _terminate_process_group(process)
        except FileNotFoundError:
            launch_error = f"command not found: {argv[0]}"
        except PermissionError:
            launch_error = f"command is not executable: {argv[0]}"
        except OSError as error:
            launch_error = f"failed to launch {argv[0]}: {error}"

    exit_code = None if process is None else process.returncode
    termination_signal = -exit_code if exit_code is not None and exit_code < 0 else None
    marker_found = None
    if success_marker is not None:
        marker_found = _file_contains_marker(stdout_path, success_marker) or _file_contains_marker(
            stderr_path, success_marker
        )
    ok = (
        launch_error is None
        and not timed_out
        and not interrupted
        and exit_code == expected_exit_code
        and marker_found is not False
    )
    if launch_error is not None:
        status = "launch_failed"
    elif interrupted:
        status = "interrupted"
    elif timed_out:
        status = "timed_out"
    elif exit_code != expected_exit_code:
        status = "unexpected_exit"
    elif marker_found is False:
        status = "missing_success_marker"
    else:
        status = "passed"
    return {
        "ok": ok,
        "status": status,
        "argv": argv,
        "cwd": str(cwd),
        "environment": dict(sorted(reported_environment.items())),
        "timeout_seconds": timeout_seconds,
        "expected_exit_code": expected_exit_code,
        "exit_code": exit_code,
        "termination_signal": termination_signal,
        "timed_out": timed_out,
        "interrupted": interrupted,
        "launch_error": launch_error,
        "success_marker": success_marker,
        "success_marker_found": marker_found,
        "started_at": started_at,
        "finished_at": _now_iso(),
        "duration_seconds": max(0.0, time.monotonic() - started),
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
    }


def _entrypoint_argv(runtime: HlsRuntime, path: str, args: tuple[str, ...]) -> list[str]:
    return [str((runtime.workspace / path).resolve()), *args]


def _probe_tool(runtime: HlsRuntime, run_dir: Path, timeout_seconds: float) -> dict[str, Any]:
    tool = runtime.manifest.tool
    environment, reported = _effective_environment(runtime, None)
    process = _execute_process(
        [tool.executable, *tool.version_args],
        cwd=runtime.workspace,
        environment=environment,
        reported_environment=reported,
        timeout_seconds=min(timeout_seconds, _VERSION_TIMEOUT_SECONDS),
        expected_exit_code=0,
        stdout_path=run_dir / "tool-version.stdout.log",
        stderr_path=run_dir / "tool-version.stderr.log",
        success_marker=None,
    )
    combined = _read_text(Path(process["stdout_path"]), max_bytes=1024 * 1024) + _read_text(
        Path(process["stderr_path"]), max_bytes=1024 * 1024
    )
    match = re.search(tool.version_regex, combined)
    observed = match.group(1) if match is not None else None
    version_matches = observed == tool.version
    result = {
        **process,
        "family": tool.family,
        "expected_version": tool.version,
        "observed_version": observed,
        "version_matches": version_matches,
    }
    if process["ok"] and observed is None:
        result["ok"] = False
        result["status"] = "version_unparseable"
    elif process["ok"] and not version_matches:
        result["ok"] = False
        result["status"] = "version_mismatch"
    return result


def _case_expectations(runtime: HlsRuntime, case: HlsCase) -> tuple[int, str | None]:
    expected_exit = (
        case.expected_exit_code
        if case.expected_exit_code is not None
        else runtime.manifest.expected_exit_code
    )
    marker = (
        case.success_marker if case.success_marker is not None else runtime.manifest.success_marker
    )
    return expected_exit, marker


def _artifact_state(runtime: HlsRuntime) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for artifact in runtime.manifest.artifacts:
        path = (runtime.workspace / artifact.path).resolve()
        try:
            path.relative_to(runtime.workspace)
            safe = True
        except ValueError:
            safe = False
        exists = safe and path.is_file()
        size = path.stat().st_size if exists else None
        result.append(
            {
                "path": artifact.path,
                "absolute_path": str(path),
                "required": artifact.required,
                "max_bytes": artifact.max_bytes,
                "safe": safe,
                "exists": exists,
                "size_bytes": size,
                "within_declared_limit": None if size is None else size <= artifact.max_bytes,
            }
        )
    return result


def _overall_status(result: Mapping[str, Any]) -> str:
    tool = result.get("tool")
    if isinstance(tool, dict) and not tool.get("ok", False):
        status = str(tool.get("status") or "tool_failed")
        return "tool_version_mismatch" if status == "version_mismatch" else f"tool_{status}"
    prepare = result.get("prepare")
    if isinstance(prepare, dict) and not prepare.get("ok", False):
        if prepare.get("interrupted"):
            return "interrupted"
        if prepare.get("timed_out"):
            return "timed_out"
        return "prepare_failed"
    cases = result.get("cases")
    if isinstance(cases, list):
        for case in cases:
            if isinstance(case, dict) and not case.get("ok", False):
                status = str(case.get("status") or "failed")
                if status in {
                    "interrupted",
                    "timed_out",
                    "missing_success_marker",
                    "unexpected_exit",
                }:
                    return status
                return "case_failed"
    artifacts = result.get("artifacts")
    if isinstance(artifacts, list):
        if any(
            isinstance(item, dict)
            and item.get("required")
            and (not item.get("safe") or not item.get("exists"))
            for item in artifacts
        ):
            return "missing_required_artifact"
    return "passed"


@contextmanager
def _workspace_lock(runtime: HlsRuntime) -> Iterator[None]:
    lock_path = runtime.workspace.parent / f".{runtime.workspace.name}.xdb-hls.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise XdbError(
                f"HLS C-simulation workspace is already in use: {runtime.workspace}"
            ) from error
        lock.seek(0)
        lock.truncate()
        lock.write(f"{os.getpid()}\n")
        lock.flush()
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def format_hls_sim_summary(result: Mapping[str, Any]) -> str:
    manifest = cast(
        dict[str, Any], result.get("manifest") if isinstance(result.get("manifest"), dict) else {}
    )
    tool = cast(dict[str, Any], result.get("tool") if isinstance(result.get("tool"), dict) else {})
    cases = [item for item in list(result.get("cases") or []) if isinstance(item, dict)]
    lines = [
        "HLS C-simulation",
        f"status: {result.get('status', '?')}",
        f"project: {manifest.get('project', '?')}  top: {manifest.get('top', '?')}",
        f"tool: {tool.get('family', '?')} {tool.get('observed_version') or 'unavailable'} "
        f"(expected {tool.get('expected_version', '?')})",
        f"workspace: {result.get('workspace', '?')}",
        f"policy: {result.get('policy', '?')}",
    ]
    for case in cases:
        lines.append(
            f"  {case.get('name', '?')}: {case.get('status', '?')} "
            f"exit={case.get('exit_code')} duration={float(case.get('duration_seconds') or 0):.3f}s"
        )
    if not cases:
        prepare = result.get("prepare") if isinstance(result.get("prepare"), dict) else {}
        if prepare:
            lines.append(f"prepare: {prepare.get('status', '?')}")
    lines.append(f"result: {result.get('result_path', '?')}")
    return "\n".join(lines)


def load_last_hls_result(runtime: HlsRuntime) -> dict[str, Any] | None:
    if not runtime.last_result_path.is_file():
        return None
    try:
        data = json.loads(runtime.last_result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def run_hls_sim(
    package: str | None,
    *,
    workspace: str | None = None,
    case_name: str | None = None,
    all_cases: bool = False,
    continue_on_failure: bool = False,
    timeout_seconds: float | None = None,
    force_restage: bool = False,
) -> dict[str, Any]:
    runtime = resolve_hls_runtime(package, workspace=workspace, stage=False)
    selected_cases = select_hls_cases(
        runtime.manifest,
        case_name=case_name,
        all_cases=all_cases,
    )
    timeout = (
        runtime.manifest.default_timeout_seconds if timeout_seconds is None else timeout_seconds
    )
    if not 0 < timeout <= 86400:
        raise XdbError("HLS C-simulation timeout must be > 0 and <= 86400 seconds")

    identifier = _run_id()
    started = time.monotonic()
    with _workspace_lock(runtime):
        runtime = stage_hls_runtime(runtime, force=force_restage)
        run_dir = runtime.runs_dir / identifier
        run_dir.mkdir(parents=True, exist_ok=False)
        result_path = run_dir / "result.json"
        result: dict[str, Any] = {
            "schema": RESULT_SCHEMA,
            "xdb_version": __version__,
            "invocation": list(sys.argv),
            "ok": False,
            "status": "running",
            "run_id": identifier,
            "started_at": _now_iso(),
            "finished_at": None,
            "duration_seconds": None,
            "package_runtime": str(runtime.package_root),
            "package_fingerprint": runtime.package_fingerprint,
            "manifest_path": str(runtime.manifest.path),
            "workspace": str(runtime.workspace),
            "staged": runtime.staged,
            "workspace_reused": runtime.workspace_reused,
            "policy": "continue-on-failure" if continue_on_failure else "fail-fast",
            "selected_cases": [case.name for case in selected_cases],
            "timeout_seconds": timeout,
            "manifest": manifest_summary(runtime.manifest),
            "tool": None,
            "prepare": None,
            "cases": [],
            "artifacts": [],
            "result_path": str(result_path),
        }

        with _translate_termination_to_interrupt():
            _write_json(
                runtime.active_path,
                {
                    "pid": os.getpid(),
                    "run_id": identifier,
                    "started_at": result["started_at"],
                    "result_path": str(result_path),
                },
            )
            try:
                tool_result = _probe_tool(runtime, run_dir, timeout)
                result["tool"] = tool_result
                if tool_result["ok"]:
                    environment, reported = _effective_environment(runtime, None)
                    prepare_result = _execute_process(
                        _entrypoint_argv(
                            runtime,
                            runtime.manifest.prepare.path,
                            runtime.manifest.prepare.args,
                        ),
                        cwd=runtime.workspace,
                        environment=environment,
                        reported_environment=reported,
                        timeout_seconds=timeout,
                        expected_exit_code=0,
                        stdout_path=run_dir / "prepare.stdout.log",
                        stderr_path=run_dir / "prepare.stderr.log",
                        success_marker=None,
                    )
                    result["prepare"] = prepare_result
                    if prepare_result["ok"]:
                        for case in selected_cases:
                            expected_exit, marker = _case_expectations(runtime, case)
                            case_env, case_reported = _effective_environment(runtime, case.name)
                            process = _execute_process(
                                _entrypoint_argv(
                                    runtime,
                                    runtime.manifest.run.path,
                                    (*runtime.manifest.run.args, *case.args),
                                ),
                                cwd=runtime.workspace,
                                environment=case_env,
                                reported_environment=case_reported,
                                timeout_seconds=timeout,
                                expected_exit_code=expected_exit,
                                stdout_path=run_dir / f"case-{case.name}.stdout.log",
                                stderr_path=run_dir / f"case-{case.name}.stderr.log",
                                success_marker=marker,
                            )
                            case_result = {
                                "name": case.name,
                                "fixtures": list(case.fixtures),
                                **process,
                            }
                            result["cases"].append(case_result)
                            if not process["ok"] and not continue_on_failure:
                                break
                result["artifacts"] = _artifact_state(runtime)
                result["status"] = _overall_status(result)
                result["ok"] = result["status"] == "passed"
            except KeyboardInterrupt:
                result["status"] = "interrupted"
                result["ok"] = False
            finally:
                result["finished_at"] = _now_iso()
                result["duration_seconds"] = max(0.0, time.monotonic() - started)
                _write_json(result_path, result)
                _write_json(runtime.last_result_path, result)
                try:
                    runtime.active_path.unlink(missing_ok=True)
                except OSError:
                    pass
        return result
