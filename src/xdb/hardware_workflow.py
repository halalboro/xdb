from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

from xdb.backend.base import DebugBackend, ProbeTrigger
from xdb.errors import XdbError


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalize_command(command: list[str]) -> list[str]:
    normalized = list(command)
    if normalized and normalized[0] == "--":
        normalized.pop(0)
    if not normalized:
        raise XdbError("missing command after --exec")
    return normalized


def _parse_env(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in values:
        if "=" not in item:
            raise XdbError(f"environment override must be KEY=VALUE: {item!r}")
        key, value = item.split("=", 1)
        if not key:
            raise XdbError(f"environment override has empty key: {item!r}")
        result[key] = value
    return result


def _run_host(
    command: list[str],
    *,
    cwd: str | None,
    env_values: list[str],
    timeout: float,
    stdout_path: Path,
    stderr_path: Path,
) -> dict[str, object]:
    if timeout <= 0:
        raise XdbError("host command timeout must be > 0")
    environment = dict(os.environ)
    environment.update(_parse_env(env_values))
    started = time.monotonic()
    process = subprocess.Popen(
        _normalize_command(command),
        cwd=str(Path(cwd).expanduser().resolve()) if cwd else None,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
        start_new_session=True,
    )
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        os.killpg(process.pid, signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
    stdout_path.write_bytes(stdout or b"")
    stderr_path.write_bytes(stderr or b"")
    return {
        "command": _normalize_command(command),
        "cwd": str(Path(cwd).expanduser().resolve()) if cwd else os.getcwd(),
        "exit_code": process.returncode,
        "timed_out": timed_out,
        "elapsed_seconds": time.monotonic() - started,
        "stdout": str(stdout_path),
        "stdout_sha256": _sha256(stdout_path),
        "stderr": str(stderr_path),
        "stderr_sha256": _sha256(stderr_path),
    }


def capture_around_command(
    backend: DebugBackend,
    *,
    part_hint: str,
    ila_name: str,
    output_path: str,
    command: list[str],
    samples: int,
    windows: int,
    trigger_position: int | None,
    triggers: list[ProbeTrigger],
    ltx: str | None,
    capture_timeout: int,
    export_format: str,
    host_timeout: float,
    host_cwd: str | None,
    host_env: list[str],
) -> dict[str, Any]:
    output = Path(output_path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    stdout_path = Path(str(output) + ".host.stdout")
    stderr_path = Path(str(output) + ".host.stderr")
    armed = backend.arm_ila(
        part_hint,
        ila_name,
        samples,
        timeout=capture_timeout,
        ltx=ltx,
        windows=windows,
        trigger_position=trigger_position,
        triggers=triggers,
    )
    host = _run_host(
        command,
        cwd=host_cwd,
        env_values=host_env,
        timeout=host_timeout,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
    )
    waited = backend.wait_ila(part_hint, ila_name, timeout=capture_timeout, ltx=ltx)
    capture = backend.upload_ila(
        part_hint,
        ila_name,
        str(output),
        timeout=capture_timeout,
        ltx=ltx,
        export_format=export_format,
    )
    result: dict[str, Any] = {
        "schema": "xdb.ila-with-capture/v1",
        "arm": armed,
        "host": host,
        "wait": waited,
        "capture": capture,
    }
    manifest_path = Path(str(output) + ".workflow.json")
    result["manifest"] = str(manifest_path)
    manifest_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result
