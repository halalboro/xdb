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
    OP_CLEAR_COMPLETED,
    OP_CLOSE,
    OP_COMPLETED,
    OP_COYOTE_STATUS,
    OP_CSR_READ,
    OP_CSR_WRITE,
    OP_FORCE,
    OP_GET,
    OP_GET_MANY,
    OP_INVOKE,
    OP_IRQ_WAIT,
    OP_MEM_MAP,
    OP_MEM_READ,
    OP_MEM_UNMAP,
    OP_MEM_WRITE,
    OP_OBJECTS,
    OP_READ_SIGNALS,
    OP_RELEASE,
    OP_RESTART,
    OP_RUN,
    OP_SCOPES,
    OP_SOURCE,
    OP_SNAPSHOT,
    OP_STATUS,
    OP_STEP,
    OP_TIME,
    OP_TCL,
    OP_TOP,
    OP_UNTIL,
    OP_UNTIL_SIGNAL,
    OP_WATCH_CHANGES,
    OP_WAVE_ADD,
    OP_DIFF_SNAPSHOT,
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
        package_runtime: str = "",
        runtime_root: str = "",
        work_dir: str = "",
        compile_script: str = "",
        elaborate_script: str = "",
        simulate_script: str = "",
    ):
        self.paths = paths
        self.project = project
        self.simset = simset
        self.mode = mode
        self.top = top
        self.package_runtime = package_runtime
        self.runtime_root = runtime_root
        self.work_dir = work_dir
        self.compile_script = compile_script
        self.elaborate_script = elaborate_script
        self.simulate_script = simulate_script
        self.driver = VivadoSimDriver(
            project=project,
            simset=simset,
            mode=mode,
            top=top,
            vivado_log_path=str(paths.vivado_log_path),
            runtime_root=runtime_root,
            work_dir=work_dir,
            compile_script=compile_script,
            elaborate_script=elaborate_script,
            simulate_script=simulate_script,
        )
        self._stop = False
        self._server: socket.socket | None = None
        self.meta = write_meta(
            self.paths,
            {
                "pid": os.getpid(),
                "cwd": str(Path.cwd()),
                "launch_kind": "runtime",
                "project": self.project,
                "simset": self.simset,
                "mode": self.mode,
                "top": self.top,
                "package_runtime": self.package_runtime,
                "runtime_root": self.runtime_root,
                "work_dir": self.work_dir,
                "compile_script": self.compile_script,
                "elaborate_script": self.elaborate_script,
                "simulate_script": self.simulate_script,
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
        if op == OP_FORCE:
            signal_name = str(args.get("signal") or "")
            values = [str(v) for v in list(args.get("values") or [])]
            if not signal_name:
                raise XdbError("missing signal")
            if not values:
                raise XdbError("missing force value")
            return self.driver.force(
                signal_name,
                values,
                radix=str(args.get("radix") or "") or None,
                repeat_every=str(args.get("repeat_every") or "") or None,
                cancel_after=str(args.get("cancel_after") or "") or None,
            )
        if op == OP_RELEASE:
            all_forces = bool(args.get("all"))
            signal_name = str(args.get("signal") or "")
            if not all_forces and not signal_name:
                raise XdbError("missing signal; pass a signal path or --all")
            return self.driver.release(signal_name or None, all_forces=all_forces)
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
        if op == OP_READ_SIGNALS:
            signals = [str(v) for v in list(args.get("signals") or []) if str(v)]
            if not signals:
                raise XdbError("missing signals")
            return self.driver.read_signals(signals)
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
        if op == OP_UNTIL:
            expr = str(args.get("expr") or "")
            step_tokens = [str(v) for v in list(args.get("step_tokens") or [])]
            timeout_seconds = args.get("timeout_seconds")
            max_iterations = args.get("max_iterations")
            if not expr:
                raise XdbError("missing Tcl expression")
            if not step_tokens:
                raise XdbError("missing step duration")
            if timeout_seconds is not None and float(timeout_seconds) <= 0:
                raise XdbError("timeout_seconds must be > 0")
            if max_iterations is not None and int(max_iterations) <= 0:
                raise XdbError("max_iterations must be > 0")
            return self.driver.wait_until(
                expr,
                step_tokens=step_tokens,
                timeout_seconds=None if timeout_seconds is None else float(timeout_seconds),
                max_iterations=None if max_iterations is None else int(max_iterations),
            )
        if op == OP_UNTIL_SIGNAL:
            signal_name = str(args.get("signal") or "")
            value = str(args.get("value") or "")
            step_tokens = [str(v) for v in list(args.get("step_tokens") or [])]
            timeout_seconds = args.get("timeout_seconds")
            max_iterations = args.get("max_iterations")
            if not signal_name:
                raise XdbError("missing signal")
            if value == "":
                raise XdbError("missing expected signal value")
            if not step_tokens:
                raise XdbError("missing step duration")
            if timeout_seconds is not None and float(timeout_seconds) <= 0:
                raise XdbError("timeout_seconds must be > 0")
            if max_iterations is not None and int(max_iterations) <= 0:
                raise XdbError("max_iterations must be > 0")
            return self.driver.wait_until_signal(
                signal_name,
                value,
                step_tokens=step_tokens,
                timeout_seconds=None if timeout_seconds is None else float(timeout_seconds),
                max_iterations=None if max_iterations is None else int(max_iterations),
            )
        if op == OP_BREAKPOINT_ADD:
            condition = str(args.get("condition") or "")
            if not condition:
                raise XdbError("missing breakpoint condition")
            return self.driver.add_breakpoint(condition)
        if op == OP_BREAKPOINT_CLEAR:
            return self.driver.clear_breakpoints()
        if op == OP_TCL:
            script = str(args.get("script") or "")
            if not script:
                raise XdbError("missing Tcl script")
            return self.driver.eval_tcl(script)
        if op == OP_SOURCE:
            path = str(args.get("path") or "")
            if not path:
                raise XdbError("missing Tcl script path")
            return self.driver.source_tcl(path)
        if op == OP_COYOTE_STATUS:
            return self.driver.coyote_status()
        if op == OP_CSR_READ:
            return self.driver.coyote_csr_read(
                int(args.get("addr") or 0),
                timeout_seconds=None
                if args.get("timeout_seconds") is None
                else float(args.get("timeout_seconds")),
            )
        if op == OP_CSR_WRITE:
            return self.driver.coyote_csr_write(
                int(args.get("addr") or 0),
                int(args.get("value") or 0),
            )
        if op == OP_MEM_MAP:
            return self.driver.coyote_mem_map(
                str(args.get("space") or "host"),
                int(args.get("addr") or 0),
                int(args.get("size") or 0),
            )
        if op == OP_MEM_UNMAP:
            return self.driver.coyote_mem_unmap(
                str(args.get("space") or "host"),
                int(args.get("addr") or 0),
            )
        if op == OP_MEM_WRITE:
            data_hex = str(args.get("data_hex") or "")
            if not data_hex:
                raise XdbError("missing memory write payload")
            return self.driver.coyote_mem_write(
                str(args.get("space") or "host"),
                int(args.get("addr") or 0),
                bytes.fromhex(data_hex),
            )
        if op == OP_MEM_READ:
            return self.driver.coyote_mem_read(
                str(args.get("space") or "host"),
                int(args.get("addr") or 0),
                int(args.get("size") or 0),
            )
        if op == OP_INVOKE:
            return self.driver.coyote_invoke(
                str(args.get("opcode") or ""),
                addr=None if args.get("addr") is None else int(args.get("addr")),
                length=None if args.get("length") is None else int(args.get("length")),
                stream_name=str(args.get("stream_name") or "host"),
                dest=int(args.get("dest") or 0),
                last=bool(args.get("last", True)),
                src_addr=None if args.get("src_addr") is None else int(args.get("src_addr")),
                src_length=None if args.get("src_length") is None else int(args.get("src_length")),
                src_stream_name=str(args.get("src_stream_name") or "host"),
                src_dest=int(args.get("src_dest") or 0),
                dst_addr=None if args.get("dst_addr") is None else int(args.get("dst_addr")),
                dst_length=None if args.get("dst_length") is None else int(args.get("dst_length")),
                dst_stream_name=str(args.get("dst_stream_name") or "host"),
                dst_dest=int(args.get("dst_dest") or 0),
            )
        if op == OP_COMPLETED:
            return self.driver.coyote_completed(
                str(args.get("opcode") or ""),
                target_count=None
                if args.get("target_count") is None
                else int(args.get("target_count")),
                timeout_seconds=None
                if args.get("timeout_seconds") is None
                else float(args.get("timeout_seconds")),
            )
        if op == OP_CLEAR_COMPLETED:
            return self.driver.coyote_clear_completed()
        if op == OP_IRQ_WAIT:
            return self.driver.coyote_irq_wait(
                timeout_seconds=None
                if args.get("timeout_seconds") is None
                else float(args.get("timeout_seconds")),
            )
        if op == OP_SNAPSHOT:
            scope = str(args.get("scope") or "")
            if not scope:
                raise XdbError("missing scope")
            name = str(args.get("name") or "")
            return self.driver.snapshot_scope(scope, name=name or None)
        if op == OP_DIFF_SNAPSHOT:
            before = str(args.get("before") or "")
            after = str(args.get("after") or "")
            if not before or not after:
                raise XdbError("diff-snapshot requires both snapshot identifiers")
            return self.driver.diff_snapshot(before, after)
        if op == OP_WATCH_CHANGES:
            scope = str(args.get("scope") or "")
            duration_tokens = [str(v) for v in list(args.get("duration_tokens") or [])]
            if not scope:
                raise XdbError("missing scope")
            if not duration_tokens:
                raise XdbError("missing duration")
            return self.driver.watch_changes(scope, duration_tokens=duration_tokens)
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
    package_runtime: str = "",
    runtime_root: str = "",
    work_dir: str = "",
    compile_script: str = "",
    elaborate_script: str = "",
    simulate_script: str = "",
) -> None:
    paths = SessionPaths(Path(anchor_dir), session_name)
    daemon = SimDaemon(
        paths=paths,
        project=project,
        simset=simset,
        mode=mode,
        top=top,
        package_runtime=package_runtime,
        runtime_root=runtime_root,
        work_dir=work_dir,
        compile_script=compile_script,
        elaborate_script=elaborate_script,
        simulate_script=simulate_script,
    )
    daemon.run()
