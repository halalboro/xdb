from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
from datetime import datetime, timezone
from pathlib import Path

from ..errors import XdbError
from .types import SessionMeta

_RUNTIME_STAGED_STAMP = ".xdb-runtime-staged.json"
_RUNTIME_META = "xdb-runtime.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cache_root() -> Path:
    base = os.environ.get("XDB_SIM_CACHE_DIR")
    if base:
        return Path(base).expanduser()
    return Path.home() / ".cache" / "xdb" / "sim"


def _repo_root_for(path: Path) -> Path:
    current = path.resolve()
    for candidate in [current, *current.parents]:
        if (candidate / ".git").exists():
            return candidate
    return current


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip())
    slug = slug.strip("-._")
    return slug or "default"


def _session_id(anchor_dir: Path, session_name: str | None) -> str:
    label = _slug(session_name or "default")
    digest = hashlib.sha256(str(anchor_dir.resolve()).encode("utf-8")).hexdigest()[:12]
    return f"{label}-{digest}"


def _env_value(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _resolve_path(value: str, base: Path | None = None) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = (base or Path.cwd()) / path
    return path.resolve()


class SessionPaths:
    def __init__(self, anchor_dir: Path, session_name: str | None):
        effective_session_name = resolve_session_name_arg(session_name)
        self.anchor_dir = anchor_dir.resolve()
        self.session_name = effective_session_name or "default"
        self.session_id = _session_id(self.anchor_dir, effective_session_name)
        self.session_dir = _cache_root() / self.session_id
        self.meta_path = self.session_dir / "meta.json"
        self.socket_path = self.session_dir / "control.sock"
        self.daemon_log_path = self.session_dir / "daemon.log"
        self.vivado_log_path = self.session_dir / "vivado.log"

    def to_meta(self) -> SessionMeta:
        return {
            "session_id": self.session_id,
            "session_name": self.session_name,
            "session_dir": str(self.session_dir),
            "socket_path": str(self.socket_path),
            "daemon_log": str(self.daemon_log_path),
            "vivado_log": str(self.vivado_log_path),
            "anchor_dir": str(self.anchor_dir),
        }


def session_paths(session_name: str | None, cwd: Path | None = None) -> SessionPaths:
    actual_cwd = (cwd or Path.cwd()).resolve()
    anchor = _repo_root_for(actual_cwd)
    return SessionPaths(anchor, session_name)


def ensure_session_dir(paths: SessionPaths) -> None:
    paths.session_dir.mkdir(parents=True, exist_ok=True)


def write_meta(paths: SessionPaths, meta: SessionMeta) -> SessionMeta:
    ensure_session_dir(paths)
    payload = dict(paths.to_meta())
    payload.update(meta)
    payload["updated_at"] = _now_iso()
    if "created_at" not in payload:
        payload["created_at"] = payload["updated_at"]
    with paths.meta_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
    return payload


def load_meta(paths: SessionPaths) -> SessionMeta | None:
    if not paths.meta_path.exists():
        return None
    with paths.meta_path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return data


def remove_session(paths: SessionPaths) -> None:
    if paths.session_dir.exists():
        shutil.rmtree(paths.session_dir, ignore_errors=True)


def pid_is_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def is_live_session(meta: SessionMeta | None) -> bool:
    if not meta:
        return False
    pid = int(meta.get("pid", 0) or 0)
    socket_path = str(meta.get("socket_path", ""))
    return pid_is_alive(pid) and bool(socket_path) and Path(socket_path).exists()


def cleanup_stale_session(paths: SessionPaths) -> None:
    meta = load_meta(paths)
    if meta and not is_live_session(meta):
        remove_session(paths)


def require_live_meta(paths: SessionPaths) -> SessionMeta:
    cleanup_stale_session(paths)
    meta = load_meta(paths)
    if not is_live_session(meta):
        raise XdbError(
            f"no live simulation session for {paths.session_name!r}; run 'xdb sim launch' first"
        )
    assert meta is not None
    return meta


def terminate_session(meta: SessionMeta, force: bool = False) -> None:
    pid = int(meta.get("pid", 0) or 0)
    if not pid_is_alive(pid):
        return
    sig = signal.SIGKILL if force else signal.SIGTERM
    try:
        os.kill(pid, sig)
    except OSError:
        return


def resolve_session_name_arg(session_name: str | None) -> str | None:
    if session_name is not None and session_name.strip() != "":
        return session_name.strip()
    return _env_value("XDB_SIM_SESSION")


def resolve_simset_arg(simset: str | None) -> str:
    if simset is not None and simset.strip() != "":
        return simset.strip()
    return _env_value("XDB_SIM_SIMSET") or "sim_1"


def resolve_mode_arg(mode: str | None) -> str:
    resolved = (
        mode.strip()
        if mode is not None and mode.strip() != ""
        else _env_value("XDB_SIM_MODE")
    )
    if not resolved:
        resolved = "behavioral"
    if resolved not in {"behavioral", "post-synth", "post-impl"}:
        raise XdbError(
            "invalid simulation mode: expected behavioral, post-synth, or post-impl "
            f"(got {resolved!r})"
        )
    return resolved


def resolve_top_arg(top: str | None, meta: SessionMeta | None) -> str:
    if top is not None:
        return top
    env_top = _env_value("XDB_SIM_TOP")
    if env_top is not None:
        return env_top
    return str((meta or {}).get("top") or "")


def _source_tree_fingerprint(source_root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(source_root.rglob("*")):
        rel = path.relative_to(source_root)
        digest.update(str(rel).encode("utf-8"))
        if path.is_symlink():
            digest.update(b"symlink\0")
            digest.update(os.readlink(path).encode("utf-8"))
            continue
        stat = path.stat()
        if path.is_dir():
            digest.update(b"dir\0")
        else:
            digest.update(b"file\0")
            digest.update(str(stat.st_size).encode("utf-8"))
            digest.update(b"\0")
        digest.update(str(stat.st_mtime_ns).encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def _make_workspace_tree_user_writable(workspace: Path) -> None:
    if not workspace.exists():
        return

    for path in [workspace, *workspace.rglob("*")]:
        if path.is_symlink():
            continue
        try:
            mode = path.stat().st_mode
            if path.is_dir():
                os.chmod(path, mode | 0o700)
            else:
                os.chmod(path, mode | 0o600)
        except OSError:
            continue


def _runtime_stage_stamp_path(workspace: Path) -> Path:
    return workspace / _RUNTIME_STAGED_STAMP


def _current_runtime_stage_stamp(workspace: Path) -> dict[str, object] | None:
    stamp_path = _runtime_stage_stamp_path(workspace)
    if not stamp_path.exists():
        return None
    try:
        with stamp_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return data


def _load_runtime_meta(root: Path) -> dict[str, str]:
    meta_path = root / _RUNTIME_META
    if not meta_path.is_file():
        raise XdbError(f"missing packaged simulation runtime metadata: {meta_path}")
    try:
        with meta_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        raise XdbError(f"failed to read packaged simulation runtime metadata: {meta_path}") from e
    if not isinstance(data, dict):
        raise XdbError(f"invalid packaged simulation runtime metadata: {meta_path}")
    required = ["work_dir", "compile_script", "elaborate_script", "simulate_script"]
    missing = [key for key in required if not isinstance(data.get(key), str) or not str(data[key]).strip()]
    if missing:
        raise XdbError(
            f"packaged simulation runtime metadata is missing required field(s) {missing}: {meta_path}"
        )
    return {str(k): str(v) for k, v in data.items() if isinstance(v, (str, int, float))}


def _resolve_packaged_runtime_layout(package_value: str, workspace_value: str) -> tuple[Path, Path]:
    package_path = _resolve_path(package_value)
    workspace = _resolve_path(workspace_value)

    if package_path.is_file():
        if package_path.name != _RUNTIME_META:
            raise XdbError(
                "XDB_SIM_PACKAGE_RUNTIME must point to a runtime directory or xdb-runtime.json"
            )
        source_root = package_path.parent.resolve()
    elif package_path.is_dir():
        source_root = package_path.resolve()
    else:
        raise XdbError(
            f"packaged simulation runtime not found: {package_path} "
            "(build the simulation package first)"
        )

    _load_runtime_meta(source_root)
    return source_root, workspace.resolve()


def _runtime_stage_matches(workspace: Path, *, source_root: Path) -> bool:
    stamp = _current_runtime_stage_stamp(workspace)
    if not stamp:
        return False
    return (
        stamp.get("source_root") == str(source_root)
        and stamp.get("source_fingerprint") == _source_tree_fingerprint(source_root)
        and (workspace / _RUNTIME_META).is_file()
    )


def _write_runtime_stage_stamp(workspace: Path, *, source_root: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    payload = {
        "source_root": str(source_root),
        "source_fingerprint": _source_tree_fingerprint(source_root),
        "updated_at": _now_iso(),
    }
    with _runtime_stage_stamp_path(workspace).open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)


def _stage_runtime_tree(source_root: Path, workspace: Path) -> bool:
    if _runtime_stage_matches(workspace, source_root=source_root):
        return False

    if workspace.exists():
        _make_workspace_tree_user_writable(workspace)
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source_root, workspace, dirs_exist_ok=True, copy_function=shutil.copyfile)
    _make_workspace_tree_user_writable(workspace)
    _write_runtime_stage_stamp(workspace, source_root=source_root)
    return True


def resolve_launch_spec(*, stage: bool) -> dict[str, str | bool]:
    runtime_value = _env_value("XDB_SIM_PACKAGE_RUNTIME")
    if runtime_value is None:
        legacy_values = {
            name: _env_value(name)
            for name in ("XDB_SIM_PACKAGE_PROJECT", "XDB_SIM_PROJECT")
        }
        if any(legacy_values.values()):
            raise XdbError(
                "project-backed simulation launch is no longer supported; "
                "export XDB_SIM_PACKAGE_RUNTIME instead"
            )
        raise XdbError(
            "missing packaged simulation runtime: set XDB_SIM_PACKAGE_RUNTIME"
        )

    workspace_value = _env_value("XDB_SIM_WORKSPACE")
    if workspace_value is None:
        raise XdbError(
            "XDB_SIM_PACKAGE_RUNTIME is set but XDB_SIM_WORKSPACE is missing"
        )

    source_root, workspace = _resolve_packaged_runtime_layout(runtime_value, workspace_value)
    needs_stage = not _runtime_stage_matches(workspace, source_root=source_root)
    did_stage = False
    runtime_root = workspace
    if stage:
        did_stage = _stage_runtime_tree(source_root, workspace)
    elif needs_stage:
        runtime_root = source_root

    meta_root = runtime_root if runtime_root.exists() else source_root
    runtime_meta = _load_runtime_meta(meta_root)
    work_dir = (runtime_root / runtime_meta["work_dir"]).resolve()
    compile_script = (runtime_root / runtime_meta["compile_script"]).resolve()
    elaborate_script = (runtime_root / runtime_meta["elaborate_script"]).resolve()
    simulate_script = (runtime_root / runtime_meta["simulate_script"]).resolve()

    return {
        "launch_kind": "runtime",
        "package_runtime": str(source_root),
        "runtime_root": str(runtime_root),
        "workspace": str(workspace),
        "project": str(runtime_meta.get("project", "")),
        "work_dir": str(work_dir),
        "compile_script": str(compile_script),
        "elaborate_script": str(elaborate_script),
        "simulate_script": str(simulate_script),
        "staged": did_stage,
        "workspace_reused": not needs_stage,
        "needs_stage": needs_stage,
    }
