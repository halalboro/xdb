from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, cast

from xdb.backend.base import Capability, DebugBackend
from xdb.backend.chipscopy_backend import ChipScoPyBackend
from xdb.errors import XdbError

_ALLOWED_METHODS = {
    "list_targets",
    "program",
    "list_ilas",
    "list_instruments",
    "arm_ila",
    "compile_ila_trigger",
    "ila_status",
    "wait_ila",
    "upload_ila",
    "capture",
    "list_vios",
    "read_vio",
    "write_vio",
    "arm_ila_group",
    "ila_group_status",
    "wait_ila_group",
    "upload_ila_group",
}


def _slug(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in value.strip())
    return cleaned.strip("-._") or "default"


def _anchor() -> Path:
    current = Path.cwd().resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return current


def _xdb_root() -> Path:
    configured = os.environ.get("XDB_ROOT")
    return Path(configured).expanduser().resolve() if configured else _anchor() / ".xdb"


def _cache_root() -> Path:
    configured = os.environ.get("XDB_CACHE_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "xdb"


class HardwareSessionPaths:
    def __init__(self, name: str) -> None:
        self.name = _slug(name)
        digest = hashlib.sha256(str(_anchor()).encode()).hexdigest()[:12]
        self.session_id = f"hw-{self.name}-{digest}"
        self.session_dir = _xdb_root() / "hardware-sessions" / self.session_id
        self.meta_path = self.session_dir / "meta.json"
        self.log_path = self.session_dir / "daemon.log"
        self.socket_path = _cache_root() / "sockets" / f"{self.session_id}.sock"

    def ensure(self) -> None:
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)


def _read_meta(paths: HardwareSessionPaths) -> dict[str, Any] | None:
    try:
        value = json.loads(paths.meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _write_meta(paths: HardwareSessionPaths, value: dict[str, Any]) -> None:
    paths.ensure()
    paths.meta_path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _live(paths: HardwareSessionPaths) -> bool:
    meta = _read_meta(paths) or {}
    return _pid_alive(int(meta.get("pid", 0))) and paths.socket_path.exists()


def launch_hardware_session(name: str) -> dict[str, Any]:
    paths = HardwareSessionPaths(name)
    if _live(paths):
        raise XdbError(f"hardware session {paths.name!r} is already running")
    paths.ensure()
    paths.socket_path.unlink(missing_ok=True)
    with paths.log_path.open("ab", buffering=0) as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "xdb.hw_session", "daemon", "--name", paths.name],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
            close_fds=True,
        )
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        if _live(paths):
            result = _read_meta(paths) or {}
            result["environment"] = f"XDB_HW_SESSION={paths.name}"
            return result
        if process.poll() is not None:
            break
        time.sleep(0.05)
    raise XdbError(f"hardware session {paths.name!r} failed to start; see {paths.log_path}")


def hardware_session_status(name: str) -> dict[str, Any]:
    paths = HardwareSessionPaths(name)
    meta = _read_meta(paths) or {"name": paths.name, "state": "absent"}
    return {**meta, "live": _live(paths)}


def close_hardware_session(name: str, *, force: bool = False) -> dict[str, Any]:
    paths = HardwareSessionPaths(name)
    meta = _read_meta(paths) or {}
    if _live(paths) and not force:
        _request(paths, {"method": "close", "args": [], "kwargs": {}})
    elif _pid_alive(int(meta.get("pid", 0))):
        os.killpg(int(meta["pid"]), signal.SIGKILL if force else signal.SIGTERM)
    paths.socket_path.unlink(missing_ok=True)
    return {"name": paths.name, "closed": True, "forced": force}


def _request(paths: HardwareSessionPaths, request: dict[str, Any]) -> dict[str, Any]:
    if not _live(paths):
        raise XdbError(f"hardware session {paths.name!r} is not running")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.connect(str(paths.socket_path))
        sock.sendall(json.dumps(request).encode())
        sock.shutdown(socket.SHUT_WR)
        chunks = []
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    response = json.loads(b"".join(chunks).decode())
    if not response.get("ok"):
        raise XdbError(str(response.get("error", "hardware session request failed")))
    return cast(dict[str, Any], response.get("result") or {})


class HardwareSessionBackend:
    name = "chipscopy"

    def __init__(self, name: str) -> None:
        self.paths = HardwareSessionPaths(name)
        meta = _read_meta(self.paths) or {}
        self._capabilities = {
            Capability(value)
            for value in meta.get("capabilities", [])
            if value in Capability._value2member_map_
        }

    def capabilities(self) -> set[Capability]:
        return set(self._capabilities)

    def __getattr__(self, name: str):
        if name not in _ALLOWED_METHODS:
            raise AttributeError(name)

        def call(*args: Any, **kwargs: Any) -> dict[str, Any]:
            return _request(self.paths, {"method": name, "args": args, "kwargs": kwargs})

        return call


def persistent_backend_from_env() -> DebugBackend | None:
    name = (os.environ.get("XDB_HW_SESSION") or "").strip()
    if not name:
        return None
    paths = HardwareSessionPaths(name)
    if not _live(paths):
        raise XdbError(f"configured hardware session {paths.name!r} is not running")
    return cast(DebugBackend, HardwareSessionBackend(name))


class HardwareSessionDaemon:
    def __init__(self, name: str) -> None:
        self.paths = HardwareSessionPaths(name)
        bootstrap = ChipScoPyBackend()
        session = bootstrap._create_session(require_cs=True)  # noqa: SLF001
        self.backend = ChipScoPyBackend(persistent_session=session)
        self.stop = False

    def run(self) -> None:
        self.paths.ensure()
        self.paths.socket_path.unlink(missing_ok=True)
        meta = {
            "name": self.paths.name,
            "session_id": self.paths.session_id,
            "pid": os.getpid(),
            "state": "ready",
            "socket_path": str(self.paths.socket_path),
            "backend": self.backend.name,
            "capabilities": sorted(capability.value for capability in self.backend.capabilities()),
            "hw_server_url": os.environ.get("HW_SERVER_URL", "TCP:localhost:3121"),
            "cs_server_url": os.environ.get("CS_SERVER_URL", "TCP:localhost:3042"),
        }
        _write_meta(self.paths, meta)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(str(self.paths.socket_path))
            server.listen(8)
            while not self.stop:
                connection, _ = server.accept()
                with connection:
                    self._handle(connection)
        finally:
            server.close()
            self.paths.socket_path.unlink(missing_ok=True)
            self.backend.close()
            _write_meta(self.paths, {**meta, "state": "closed"})

    def _handle(self, connection: socket.socket) -> None:
        try:
            payload = b""
            while True:
                chunk = connection.recv(65536)
                if not chunk:
                    break
                payload += chunk
            request = json.loads(payload.decode())
            method = str(request.get("method", ""))
            if method == "close":
                self.stop = True
                result = {"closed": True}
            elif method in _ALLOWED_METHODS:
                result = getattr(self.backend, method)(
                    *list(request.get("args") or []), **dict(request.get("kwargs") or {})
                )
            else:
                raise XdbError(f"unsupported hardware session method: {method}")
            response = {"ok": True, "result": result}
        except Exception as error:
            response = {"ok": False, "error": str(error)}
        connection.sendall(json.dumps(response).encode())


def _main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["daemon"])
    parser.add_argument("--name", required=True)
    args = parser.parse_args()
    HardwareSessionDaemon(args.name).run()


if __name__ == "__main__":
    _main()
