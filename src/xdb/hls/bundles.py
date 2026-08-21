from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from xdb import __version__
from xdb.errors import XdbError
from xdb.hls.diagnostics import hls_doctor, hls_provenance
from xdb.hls.runner import load_last_hls_result
from xdb.hls.runtime import HlsRuntime, resolve_hls_runtime


_DEFAULT_BUNDLE_MAX_BYTES = 16 * 1024 * 1024
_MAX_BUNDLE_MAX_BYTES = 256 * 1024 * 1024
_DEFAULT_LOG_MAX_BYTES = 4 * 1024 * 1024


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=False) + "\n", encoding="utf-8")


def _resolve_bundle_path(runtime: HlsRuntime, out: str) -> Path:
    path = Path(out).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (runtime.control_dir / "bundles" / path).resolve()


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _copy_bounded(
    source: Path,
    destination: Path,
    *,
    limit: int,
    remaining: int,
) -> tuple[dict[str, Any], int]:
    size = source.stat().st_size
    allowed = max(0, min(limit, remaining))
    if allowed == 0:
        return {
            "source": str(source),
            "destination": None,
            "size_bytes": size,
            "copied_bytes": 0,
            "truncated": size > 0,
            "omitted_reason": "bundle byte limit reached",
        }, 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    if size <= allowed:
        shutil.copy2(source, destination)
        return {
            "source": str(source),
            "destination": str(destination),
            "size_bytes": size,
            "copied_bytes": size,
            "truncated": False,
        }, size
    tail_destination = destination.with_name(f"{destination.name}.tail")
    with source.open("rb") as source_stream:
        source_stream.seek(-allowed, os.SEEK_END)
        data = source_stream.read(allowed)
    tail_destination.write_bytes(data)
    return {
        "source": str(source),
        "destination": str(tail_destination),
        "size_bytes": size,
        "copied_bytes": len(data),
        "truncated": True,
        "omitted_bytes": size - len(data),
    }, len(data)


def _result_log_paths(result: dict[str, Any]) -> list[Path]:
    paths: list[Path] = []
    for section in (result.get("tool"), result.get("prepare")):
        if isinstance(section, dict):
            paths.extend(
                Path(str(section[key]))
                for key in ("stdout_path", "stderr_path")
                if section.get(key)
            )
    for case in list(result.get("cases") or []):
        if not isinstance(case, dict):
            continue
        paths.extend(
            Path(str(case[key])) for key in ("stdout_path", "stderr_path") if case.get(key)
        )
    return sorted(set(paths), key=lambda path: path.name)


def create_hls_bundle(
    package: str | None,
    *,
    workspace: str | None,
    out: str,
    max_bytes: int = _DEFAULT_BUNDLE_MAX_BYTES,
) -> dict[str, Any]:
    if not 1 <= max_bytes <= _MAX_BUNDLE_MAX_BYTES:
        raise XdbError(f"HLS bundle --max-bytes must be between 1 and {_MAX_BUNDLE_MAX_BYTES}")
    runtime = resolve_hls_runtime(package, workspace=workspace, stage=False)
    result = load_last_hls_result(runtime)
    if result is None:
        raise XdbError(
            f"no HLS C-simulation result is available under workspace {runtime.workspace}"
        )
    bundle_dir = _resolve_bundle_path(runtime, out)
    if _is_within(bundle_dir, runtime.package_root):
        raise XdbError(
            f"bundle output may not be written inside the immutable package: {bundle_dir}"
        )
    if bundle_dir.exists() and not bundle_dir.is_dir():
        raise XdbError(f"bundle output path is not a directory: {bundle_dir}")
    if bundle_dir.exists() and any(bundle_dir.iterdir()):
        raise XdbError(f"bundle output directory already exists and is not empty: {bundle_dir}")
    bundle_dir.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{bundle_dir.name}.", dir=bundle_dir.parent))
    copied: list[dict[str, Any]] = []
    omitted: list[dict[str, Any]] = []
    used = 0
    try:
        manifest_destination = temporary / "runtime" / runtime.manifest.path.name
        manifest_destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(runtime.manifest.path, manifest_destination)

        provenance = hls_provenance(
            str(runtime.manifest.path),
            workspace=str(runtime.workspace),
        )
        doctor = hls_doctor(
            str(runtime.manifest.path),
            workspace=str(runtime.workspace),
            probe_tool=False,
        )
        _write_json(temporary / "provenance.json", provenance)
        _write_json(temporary / "doctor.json", doctor)
        _write_json(temporary / "result.json", result)

        for source in _result_log_paths(result):
            if not _is_within(source, runtime.runs_dir):
                omitted.append({"source": str(source), "reason": "outside xdb run directory"})
                continue
            if not source.is_file():
                omitted.append({"source": str(source), "reason": "missing"})
                continue
            record, count = _copy_bounded(
                source,
                temporary / "logs" / source.name,
                limit=_DEFAULT_LOG_MAX_BYTES,
                remaining=max_bytes - used,
            )
            used += count
            if record.get("destination"):
                record["destination"] = (
                    Path(str(record["destination"])).relative_to(temporary).as_posix()
                )
            copied.append(record)

        for artifact in runtime.manifest.artifacts:
            source = (runtime.workspace / artifact.path).resolve()
            if not _is_within(source, runtime.workspace):
                omitted.append(
                    {
                        "source": str(source),
                        "path": artifact.path,
                        "reason": "runtime artifact escapes the staged workspace",
                        "required": artifact.required,
                    }
                )
                continue
            if not source.is_file():
                omitted.append(
                    {
                        "source": str(source),
                        "path": artifact.path,
                        "reason": "missing",
                        "required": artifact.required,
                    }
                )
                continue
            record, count = _copy_bounded(
                source,
                temporary / "artifacts" / artifact.path,
                limit=artifact.max_bytes,
                remaining=max_bytes - used,
            )
            used += count
            if record.get("destination"):
                record["destination"] = (
                    Path(str(record["destination"])).relative_to(temporary).as_posix()
                )
            copied.append(record)

        files = sorted(
            path.relative_to(temporary).as_posix()
            for path in temporary.rglob("*")
            if path.is_file()
        )
        manifest = {
            "schema": "xdb-hls-csim-bundle-v1",
            "created_at": result.get("finished_at"),
            "xdb_version": result.get("xdb_version") or __version__,
            "invocation": result.get("invocation") or [],
            "package_runtime": str(runtime.package_root),
            "package_fingerprint": runtime.package_fingerprint,
            "workspace": str(runtime.workspace),
            "run_id": result.get("run_id"),
            "result_status": result.get("status"),
            "max_bytes": max_bytes,
            "bounded_payload_bytes": used,
            "copied": copied,
            "omitted": omitted,
            "files": files,
        }
        _write_json(temporary / "manifest.json", manifest)
        files = sorted(
            path.relative_to(temporary).as_posix()
            for path in temporary.rglob("*")
            if path.is_file()
        )
        manifest["files"] = files
        _write_json(temporary / "manifest.json", manifest)
        if bundle_dir.exists():
            bundle_dir.rmdir()
        os.replace(temporary, bundle_dir)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return {
        "ok": True,
        "bundle_dir": str(bundle_dir),
        "run_id": result.get("run_id"),
        "files": files,
        "bounded_payload_bytes": used,
        "omitted": omitted,
    }
