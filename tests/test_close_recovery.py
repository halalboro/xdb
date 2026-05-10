from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xdb.sim.client import close_session
from xdb.sim.protocol import OP_CLOSE
from xdb.sim.session_store import session_paths, write_meta


class CloseRecoveryTests(unittest.TestCase):
    def test_close_passes_daemon_response_timeout(self) -> None:
        with patch("xdb.sim.client._send_request", return_value={"closed": True}) as send_request:
            result = close_session("unit", timeout_seconds=2.5)

        self.assertTrue(result["closed"])
        self.assertFalse(result["force"])
        self.assertEqual(send_request.call_args.args[0], "unit")
        self.assertEqual(send_request.call_args.args[1]["op"], OP_CLOSE)
        self.assertEqual(send_request.call_args.kwargs["timeout_seconds"], 2.5)

    def test_force_close_removes_stale_cached_session_without_daemon_rpc(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            (repo / ".git").mkdir()
            old_cwd = Path.cwd()
            try:
                os.chdir(repo)
                with patch.dict(os.environ, {"XDB_ROOT": str(tmp_path / "xdb-root"), "XDB_CACHE_ROOT": str(tmp_path / "cache-root")}, clear=False):
                    paths = session_paths("unit")
                    write_meta(
                        paths,
                        {
                            "pid": 999999999,
                            "socket_path": str(paths.socket_path),
                            "state": "ready",
                        },
                    )
                    paths.socket_path.write_text("stale socket placeholder", encoding="utf-8")

                    with patch("xdb.sim.client._send_request") as send_request:
                        result = close_session("unit", force=True)

                    send_request.assert_not_called()
                    self.assertTrue(result["closed"])
                    self.assertTrue(result["force"])
                    self.assertFalse(result["was_alive"])
                    self.assertTrue(result["session_removed"])
                    self.assertFalse(paths.session_dir.exists())
            finally:
                os.chdir(old_cwd)


if __name__ == "__main__":
    unittest.main()
