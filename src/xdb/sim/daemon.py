from __future__ import annotations

import json
import os
import signal
import socket
import traceback
from pathlib import Path
from typing import Any

from ..errors import XdbError
from .protocol import (
    OP_BREAKPOINT_ADD,
    OP_BREAKPOINT_CLEAR,
    OP_CLOSE,
    OP_GET,
    OP_GET_MANY,
    OP_OBJECTS,
    OP_RUN,
    OP_SCOPES,
    OP_STATUS,
    OP_STEP,
    OP_RESTART,
    OP_TIME,
    OP_TOP,
    OP_WAVE_ADD,
)
from .session_store import SessionPaths, ensure_session_dir, write_meta
from .vivado_driver import VivadoSimDriver


class SimDaemon:
    def __init__(
        self,
        paths: SessionPaths,
        project: str,
        simset: str,
        mode: str,
        top: str,
    ):
        self.paths = paths
        self.project = project
        self.simset = simset
        self.mode = mode
        self.top = top
        self.driver = VivadoSimDriver(
            project=project,
            simset=simset,
            mode=mode,
            top=top,
            vivado_log_path=str(paths.vivado_log_path),
        )
        self._stop = False
        self._server: socket.socket | None = None
        self.meta = write_meta(
            self.paths,
            {
                "pid": os.getpid(),
                "cwd": str(Path.cwd()),
                "project": self.project,
                "simset": self.simset,
                "mode": self.mode,
                "top": self.top,
                "state": "starting",
            },
        )

    def run(self) -> None:
        self._install_signal_handlers()
        try:
            ensure_session_dir(self.paths)
            self.driver.start()
            self.meta = write_meta(
                self.paths,
                {
                    **self.meta,
                    "top": self.driver.top,
                    "state": "ready",
                    "last_error": "",
                },
            )
            self._serve()
        except Exception as e:
            self.meta = write_meta(
                self.paths,
                {
                    **self.meta,
                    "state": "error",
                    "last_error": str(e),
                },
            )
            raise
        finally:
            try:
                self.driver.shutdown()
            finally:
                self._cleanup_socket()
                if self.meta.get("state") != "error":
                    self.meta = write_meta(self.paths, {**self.meta, "state": "closed"})

    def _install_signal_handlers(self) -> None:
        def _handle_signal(signum: int, _frame) -> None:
            self._stop = True
            raise SystemExit(128 + signum)

        signal.signal(signal.SIGTERM, _handle_signal)
        signal.signal(signal.SIGINT, _handle_signal)

    def _serve(self) -> None:
        self._cleanup_socket()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server = server
        server.bind(str(self.paths.socket_path))
        server.listen(8)
        server.settimeout(0.5)

        while not self._stop:
            try:
                conn, _ = server.accept()
            except TimeoutError:
                continue
            except OSError:
                if self._stop:
                    break
                raise
            with conn:
                response = self._handle_connection(conn)
                conn.sendall(json.dumps(response).encode("utf-8"))
                if bool((response.get("result") or {}).get("_shutdown")):
                    self._stop = True
                    break

    def _handle_connection(self, conn: socket.socket) -> dict[str, Any]:
        try:
            payload = self._recv_all(conn)
            req = json.loads(payload.decode("utf-8"))
            result = self._dispatch(req.get("op", ""), req.get("args") or {})
            return {"ok": True, "result": result}
        except Exception as e:
            return {
                "ok": False,
                "error": str(e),
                "traceback": traceback.format_exc(),
            }

    @staticmethod
    def _recv_all(conn: socket.socket) -> bytes:
        chunks: list[bytes] = []
        while True:
            data = conn.recv(65536)
            if not data:
                break
            chunks.append(data)
        return b"".join(chunks)

    def _dispatch(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        if op == OP_STATUS:
            time_info = self.driver.time()
            return {
                "session": self.meta,
                "time": time_info.get("time", ""),
            }
        if op == OP_TIME:
            return self.driver.time()
        if op == OP_RUN:
            return self.driver.run(list(args.get("tokens") or []))
        if op == OP_RESTART:
            return self.driver.restart()
        if op == OP_GET:
            signal_name = str(args.get("signal") or "")
            if not signal_name:
                raise XdbError("missing signal")
            return self.driver.get_signal(signal_name)
        if op == OP_GET_MANY:
            pattern = str(args.get("pattern") or "")
            if not pattern:
                raise XdbError("missing pattern")
            return self.driver.get_many(pattern)
        if op == OP_SCOPES:
            scope = args.get("scope")
            return self.driver.scopes(None if scope in (None, "") else str(scope))
        if op == OP_OBJECTS:
            scope = str(args.get("scope") or "")
            if not scope:
                raise XdbError("missing scope")
            return self.driver.objects(scope)
        if op == OP_TOP:
            top = str(args.get("top") or "")
            if not top:
                raise XdbError("missing top module")
            result = self.driver.set_top(top)
            self.top = top
            self.meta = write_meta(self.paths, {**self.meta, "top": self.driver.top, "state": "ready"})
            return result
        if op == OP_WAVE_ADD:
            pattern = str(args.get("pattern") or "")
            if not pattern:
                raise XdbError("missing pattern")
            return self.driver.add_wave(pattern)
        if op == OP_STEP:
            time_tokens = list(args.get("time_tokens") or [])
            if time_tokens:
                return self.driver.step(time_tokens=time_tokens)
            count = int(args.get("count") or 1)
            if count <= 0:
                raise XdbError("step count must be > 0")
            return self.driver.step(count=count)
        if op == OP_BREAKPOINT_ADD:
            condition = str(args.get("condition") or "")
            if not condition:
                raise XdbError("missing breakpoint condition")
            return self.driver.add_breakpoint(condition)
        if op == OP_BREAKPOINT_CLEAR:
            return self.driver.clear_breakpoints()
        if op == OP_CLOSE:
            return {"closed": True, "session_id": self.paths.session_id, "_shutdown": True}
        raise XdbError(f"unsupported simulation operation: {op}")

    def _cleanup_socket(self) -> None:
        try:
            if self._server is not None:
                self._server.close()
        finally:
            self._server = None
        try:
            self.paths.socket_path.unlink(missing_ok=True)
        except Exception:
            pass


def run_daemon(
    *,
    anchor_dir: str,
    session_name: str | None,
    project: str,
    simset: str,
    mode: str,
    top: str,
) -> None:
    paths = SessionPaths(Path(anchor_dir), session_name)
    daemon = SimDaemon(paths=paths, project=project, simset=simset, mode=mode, top=top)
    daemon.run()
