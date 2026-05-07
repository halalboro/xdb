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

_MATERIALIZED_STAMP = ".xdb-materialized.json"


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


def _find_single_xpr(root: Path, *, recursive: bool, context: str) -> Path:
    candidates = sorted(root.rglob("*.xpr") if recursive else root.glob("*.xpr"))
    if len(candidates) == 1:
        return candidates[0].resolve()
    if len(candidates) > 1:
        raise XdbError(f"multiple .xpr files found in {context}; pass --project")
    raise XdbError(f"missing simulation project in {context}: pass --project <path.xpr>")


def _materialization_stamp_path(workspace: Path) -> Path:
    return workspace / _MATERIALIZED_STAMP


def _current_materialization_stamp(workspace: Path) -> dict[str, object] | None:
    stamp_path = _materialization_stamp_path(workspace)
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


def _target_relative_to_workspace(target_project: Path, workspace: Path) -> Path:
    try:
        return target_project.resolve().relative_to(workspace.resolve())
    except ValueError as e:
        raise XdbError(
            "XDB_SIM_PROJECT must point inside XDB_SIM_WORKSPACE when using "
            "XDB_SIM_PACKAGE_PROJECT"
        ) from e


def _resolve_packaged_project_layout(
    package_value: str,
    workspace_value: str,
    target_value: str | None,
) -> tuple[Path, Path, Path, Path]:
    package_path = _resolve_path(package_value)
    workspace = _resolve_path(workspace_value)
    target_project = _resolve_path(target_value) if target_value else None

    if package_path.is_file():
        source_project = package_path.resolve()
        if target_project is None:
            source_root = source_project.parent
            target_rel = Path(source_project.name)
            target_project = workspace / target_rel
        else:
            target_rel = _target_relative_to_workspace(target_project, workspace)
            if len(target_rel.parts) > len(source_project.parts):
                raise XdbError(
                    "cannot map packaged simulation project into workspace: "
                    f"target path is too deep for source project {source_project}"
                )
            source_root = source_project.parents[len(target_rel.parts) - 1]
            if (source_root / target_rel).resolve() != source_project:
                raise XdbError(
                    "cannot map packaged simulation project into workspace: "
                    f"source={source_project} target={target_project}"
                )
        return (
            source_root.resolve(),
            source_project,
            workspace.resolve(),
            target_project.resolve(),
        )

    if package_path.is_dir():
        source_root = package_path.resolve()
        if target_project is None:
            source_project = _find_single_xpr(
                source_root,
                recursive=True,
                context=str(source_root),
            )
            target_rel = source_project.relative_to(source_root)
            target_project = workspace / target_rel
        else:
            target_rel = _target_relative_to_workspace(target_project, workspace)
            source_project = (source_root / target_rel).resolve()
            if not source_project.is_file():
                raise XdbError(
                    "packaged simulation project not found at expected path: "
                    f"{source_project}"
                )
        return (
            source_root,
            source_project,
            workspace.resolve(),
            target_project.resolve(),
        )

    if package_path.exists():
        raise XdbError(f"unsupported packaged simulation project path: {package_path}")
    raise XdbError(
        f"packaged simulation project not found: {package_path} "
        "(build the simulation package first)"
    )


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


def _materialization_matches(
    workspace: Path,
    *,
    source_root: Path,
    source_project: Path,
    target_project: Path,
) -> bool:
    stamp = _current_materialization_stamp(workspace)
    if not stamp:
        return False
    return (
        stamp.get("source_root") == str(source_root)
        and stamp.get("source_project") == str(source_project)
        and stamp.get("target_project") == str(target_project)
        and stamp.get("source_fingerprint") == _source_tree_fingerprint(source_root)
        and target_project.is_file()
    )


def _write_materialization_stamp(
    workspace: Path,
    *,
    source_root: Path,
    source_project: Path,
    target_project: Path,
) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    stamp_path = _materialization_stamp_path(workspace)
    payload = {
        "source_root": str(source_root),
        "source_project": str(source_project),
        "target_project": str(target_project),
        "source_fingerprint": _source_tree_fingerprint(source_root),
        "updated_at": _now_iso(),
    }
    with stamp_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)


def _materialize_project_tree(
    source_root: Path,
    source_project: Path,
    workspace: Path,
    target_project: Path,
) -> bool:
    if _materialization_matches(
        workspace,
        source_root=source_root,
        source_project=source_project,
        target_project=target_project,
    ):
        return False

    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source_root, workspace, dirs_exist_ok=True, copy_function=shutil.copy2)

    if not target_project.is_file():
        raise XdbError(
            "materialized simulation project is missing after copy: "
            f"{target_project}"
        )

    _write_materialization_stamp(
        workspace,
        source_root=source_root,
        source_project=source_project,
        target_project=target_project,
    )
    return True


def resolve_launch_project(
    project: str | None,
    paths: SessionPaths,
    *,
    materialize: bool,
) -> dict[str, str | bool]:
    if project:
        p = _resolve_path(project)
        if not p.is_file():
            raise XdbError(f"simulation project not found: {p}")
        return {
            "project": str(p),
            "materialized": False,
            "workspace_reused": False,
            "needs_materialization": False,
        }

    package_value = _env_value("XDB_SIM_PACKAGE_PROJECT")
    if package_value is not None:
        workspace_value = _env_value("XDB_SIM_WORKSPACE")
        if workspace_value is None:
            raise XdbError(
                "XDB_SIM_PACKAGE_PROJECT is set but XDB_SIM_WORKSPACE is missing"
            )
        target_value = _env_value("XDB_SIM_PROJECT")
        (
            source_root,
            source_project,
            workspace,
            target_project,
        ) = _resolve_packaged_project_layout(
            package_value,
            workspace_value,
            target_value,
        )
        needs_materialization = not _materialization_matches(
            workspace,
            source_root=source_root,
            source_project=source_project,
            target_project=target_project,
        )
        did_materialize = False
        if materialize:
            did_materialize = _materialize_project_tree(
                source_root,
                source_project,
                workspace,
                target_project,
            )
        return {
            "project": str(target_project),
            "package_project": str(source_project),
            "workspace": str(workspace),
            "materialized": did_materialize,
            "workspace_reused": not needs_materialization,
            "needs_materialization": needs_materialization,
        }

    env_project = _env_value("XDB_SIM_PROJECT")
    if env_project is not None:
        p = _resolve_path(env_project)
        if not p.is_file():
            raise XdbError(f"simulation project not found: {p}")
        return {
            "project": str(p),
            "materialized": False,
            "workspace_reused": False,
            "needs_materialization": False,
        }

    meta = load_meta(paths)
    if meta and meta.get("project"):
        return {
            "project": str(meta["project"]),
            "materialized": False,
            "workspace_reused": False,
            "needs_materialization": False,
        }

    candidate = _find_single_xpr(Path.cwd(), recursive=False, context="current directory")
    return {
        "project": str(candidate),
        "materialized": False,
        "workspace_reused": False,
        "needs_materialization": False,
    }
