from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xdb.backend.base import Capability
from xdb.hw_session import (
    HardwareSessionBackend,
    HardwareSessionDaemon,
    HardwareSessionPaths,
    _write_meta,
    hardware_session_status,
    launch_hardware_session,
    persistent_backend_from_env,
)


class HardwareSessionTests(unittest.TestCase):
    def session_environment(self, root: str):
        return patch.dict(
            os.environ,
            {"XDB_ROOT": f"{root}/xdb", "XDB_CACHE_ROOT": f"{root}/cache"},
            clear=True,
        )

    def test_launch_waits_for_daemon_and_returns_reuse_environment(self) -> None:
        with tempfile.TemporaryDirectory() as td, self.session_environment(td):
            process = MagicMock()
            process.poll.return_value = None

            with (
                patch("xdb.hw_session.subprocess.Popen", return_value=process) as popen,
                patch("xdb.hw_session._live", side_effect=[False, True]),
                patch("xdb.hw_session.time.sleep"),
            ):
                # First _live checks for an existing session; second observes daemon startup.
                result = launch_hardware_session("v80")

        self.assertEqual(result["environment"], "XDB_HW_SESSION=v80")
        argv = popen.call_args.args[0]
        self.assertEqual(argv[-3:], ["daemon", "--name", "v80"])

    def test_proxy_reads_capabilities_and_sends_backend_method(self) -> None:
        with tempfile.TemporaryDirectory() as td, self.session_environment(td):
            paths = HardwareSessionPaths("v80")
            _write_meta(
                paths,
                {
                    "capabilities": [Capability.ILA_CONTROL.value],
                    "pid": 123,
                },
            )
            with patch(
                "xdb.hw_session._request", return_value={"status": {"is_armed": True}}
            ) as request:
                backend = HardwareSessionBackend("v80")
                result = backend.ila_status("xcv80", "ila0", timeout=9)

        self.assertEqual(backend.capabilities(), {Capability.ILA_CONTROL})
        self.assertTrue(result["status"]["is_armed"])
        self.assertEqual(request.call_args.args[1]["method"], "ila_status")
        self.assertEqual(request.call_args.args[1]["args"], ("xcv80", "ila0"))
        self.assertEqual(request.call_args.args[1]["kwargs"], {"timeout": 9})

    def test_daemon_dispatches_allowlisted_method_and_close(self) -> None:
        daemon = object.__new__(HardwareSessionDaemon)
        daemon.backend = MagicMock()
        daemon.backend.ila_status.return_value = {"status": {"is_armed": False}}
        daemon.stop = False

        client, server = socket.socketpair()
        try:
            client.sendall(
                json.dumps(
                    {
                        "method": "ila_status",
                        "args": ["xcv80", "ila0"],
                        "kwargs": {"timeout": 3},
                    }
                ).encode()
            )
            client.shutdown(socket.SHUT_WR)
            daemon._handle(server)
            response = json.loads(client.recv(65536).decode())
        finally:
            client.close()
            server.close()

        self.assertTrue(response["ok"])
        daemon.backend.ila_status.assert_called_once_with("xcv80", "ila0", timeout=3)

    def test_environment_selects_only_a_live_persistent_session(self) -> None:
        with tempfile.TemporaryDirectory() as td, self.session_environment(td):
            paths = HardwareSessionPaths("v80")
            _write_meta(paths, {"pid": 123, "capabilities": []})
            paths.socket_path.parent.mkdir(parents=True, exist_ok=True)
            paths.socket_path.touch()
            with (
                patch.dict(os.environ, {"XDB_HW_SESSION": "v80"}),
                patch("xdb.hw_session._pid_alive", return_value=True),
            ):
                backend = persistent_backend_from_env()
                status = hardware_session_status("v80")

        self.assertIsInstance(backend, HardwareSessionBackend)
        self.assertTrue(status["live"])


if __name__ == "__main__":
    unittest.main()
