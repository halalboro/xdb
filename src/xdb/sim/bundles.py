from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from xdb.errors import XdbError
from xdb.sim.session_store import load_meta, session_paths


def _now_label() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def resolve_bundle_dir(session_name: str | None, out: str | None) -> Path:
    paths = session_paths(session_name)
    if out:
        path = Path(out).expanduser()
        if path.is_absolute():
            return path.resolve()
        return (paths.xdb_root / "artifacts" / "bundles" / path).resolve()
    return (
        paths.xdb_root / "artifacts" / "bundles" / f"{_now_label()}-{paths.session_name}"
    ).resolve()


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=False) + "\n", encoding="utf-8")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text if text.endswith("\n") else f"{text}\n", encoding="utf-8")


def _copy_if_exists(src: Path, dst: Path) -> bool:
    if not src.is_file():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return True


def _relative_files(root: Path) -> list[str]:
    return sorted(str(path.relative_to(root)) for path in root.rglob("*") if path.is_file())


def create_sim_bundle(
    session_name: str | None,
    *,
    out: str | None = None,
    doctor: dict[str, Any] | None = None,
    provenance: dict[str, Any] | None = None,
    trace_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from xdb.sim.client import doctor_session, provenance_session

    paths = session_paths(session_name)
    bundle_dir = resolve_bundle_dir(session_name, out)
    if bundle_dir.exists() and any(bundle_dir.iterdir()):
        raise XdbError(f"bundle output directory already exists and is not empty: {bundle_dir}")
    bundle_dir.mkdir(parents=True, exist_ok=True)

    errors: list[str] = []
    metadata = load_meta(paths)
    if doctor is None:
        try:
            doctor = doctor_session(session_name)
        except Exception as e:
            errors.append(f"doctor failed: {e}")
            doctor = {"ok": False, "error": str(e)}
    if provenance is None:
        try:
            provenance = provenance_session(session_name)
        except Exception as e:
            errors.append(f"provenance failed: {e}")
            provenance = {"error": str(e)}

    _write_json(bundle_dir / "doctor.json", doctor)
    _write_json(bundle_dir / "provenance.json", provenance)
    _write_json(bundle_dir / "metadata.json", metadata)
    if trace_result is not None:
        _write_json(bundle_dir / "trace.json", trace_result)
        action_result = trace_result.get("action", {}) if isinstance(trace_result, dict) else {}
        action_payload = action_result.get("result", {}) if isinstance(action_result, dict) else {}
        if isinstance(action_payload, dict) and action_payload.get("kind") == "exec":
            _write_text(bundle_dir / "host" / "stdout.txt", str(action_payload.get("stdout") or ""))
            _write_text(bundle_dir / "host" / "stderr.txt", str(action_payload.get("stderr") or ""))

    copied_logs = {
        "daemon_log": _copy_if_exists(paths.daemon_log_path, bundle_dir / "logs" / "daemon.log"),
        "vivado_log": _copy_if_exists(paths.vivado_log_path, bundle_dir / "logs" / "vivado.log"),
    }
    manifest = {
        "format": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "session": paths.session_name,
        "session_id": paths.session_id,
        "anchor_dir": str(paths.anchor_dir),
        "xdb_root": str(paths.xdb_root),
        "bundle_dir": str(bundle_dir),
        "contains_trace": trace_result is not None,
        "copied_logs": copied_logs,
        "errors": errors,
    }
    _write_json(bundle_dir / "manifest.json", manifest)
    files = _relative_files(bundle_dir)
    manifest["files"] = files
    _write_json(bundle_dir / "manifest.json", manifest)
    return {
        "ok": not errors,
        "bundle_dir": str(bundle_dir),
        "files": files,
        "errors": errors,
    }
