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


class SessionPaths:
    def __init__(self, anchor_dir: Path, session_name: str | None):
        self.anchor_dir = anchor_dir.resolve()
        self.session_name = session_name or "default"
        self.session_id = _session_id(self.anchor_dir, session_name)
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


def resolve_project_arg(project: str | None, paths: SessionPaths) -> str:
    if project:
        p = Path(project).expanduser()
        if not p.is_absolute():
            p = Path.cwd() / p
        if not p.is_file():
            raise XdbError(f"simulation project not found: {p}")
        return str(p.resolve())

    meta = load_meta(paths)
    if meta and meta.get("project"):
        return str(meta["project"])

    candidates = sorted(Path.cwd().glob("*.xpr"))
    if len(candidates) == 1:
        return str(candidates[0].resolve())
    if len(candidates) > 1:
        raise XdbError("multiple .xpr files found in current directory; pass --project")
    raise XdbError("missing simulation project: pass --project <path.xpr>")
