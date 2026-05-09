from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xdb.errors import XdbError
from xdb.sim.client import exec_session


class ExecSessionTests(unittest.TestCase):
    def test_exec_session_injects_live_sim_environment_and_captures_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir) / "repo"
            repo.mkdir()
            (repo / ".git").mkdir()
            runtime_root = Path(tmpdir) / "runtime"
            work_dir = runtime_root / "project.sim" / "sim_1" / "behav" / "xsim"
            meta = {
                "anchor_dir": str(repo),
                "runtime_root": str(runtime_root),
                "work_dir": str(work_dir),
                "socket_path": str(Path(tmpdir) / "control.sock"),
                "package_runtime": "/nix/store/demo/project/sim",
                "project": str(runtime_root / "project.xpr"),
                "simset": "sim_1",
                "top": "tb_user",
                "mode": "behavioral",
                "state": "ready",
            }
            old_cwd = Path.cwd()
            try:
                os.chdir(repo)
                with (
                    patch("xdb.sim.client.require_live_meta", return_value=meta),
                    patch("xdb.sim.client.time.time", side_effect=[10.0, 10.25]),
                    patch("xdb.sim.client._now_iso", side_effect=["start", "finish"]),
                ):
                    result = exec_session(
                        "versal",
                        [
                            sys.executable,
                            "-c",
                            (
                                "import json, os; "
                                "print(json.dumps({k: os.environ[k] for k in "
                                "['XDB_SIM_SESSION', 'XDB_SIM_RUNTIME_ROOT', "
                                "'XDB_SIM_WORK_DIR', 'COYOTE_SIM_DIR', 'EXTRA']}))"
                            ),
                        ],
                        env_overrides=["EXTRA=1"],
                    )
            finally:
                os.chdir(old_cwd)

        env = json.loads(result["stdout"])
        self.assertEqual(env["XDB_SIM_SESSION"], "versal")
        self.assertEqual(env["XDB_SIM_RUNTIME_ROOT"], str(runtime_root))
        self.assertEqual(env["XDB_SIM_WORK_DIR"], str(work_dir))
        self.assertEqual(env["COYOTE_SIM_DIR"], str(runtime_root))
        self.assertEqual(env["EXTRA"], "1")
        self.assertTrue(result["ok"])
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["duration_seconds"], 0.25)
        self.assertEqual(result["env"]["EXTRA"], "1")
        self.assertEqual(result["session"]["name"], "versal")
        self.assertFalse(result["streamed"])

    def test_exec_session_timeout_returns_structured_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch(
                    "xdb.sim.client.require_live_meta",
                    return_value={"anchor_dir": tmpdir, "runtime_root": "/runtime", "state": "ready"},
                ),
                patch("xdb.sim.client.time.time", side_effect=[1.0, 2.5]),
                patch("xdb.sim.client._now_iso", side_effect=["start", "finish"]),
            ):
                result = exec_session(
                    None,
                    [sys.executable, "-c", "import time; print('partial', flush=True); time.sleep(5)"],
                    timeout_seconds=0.1,
                )

        self.assertFalse(result["ok"])
        self.assertTrue(result["timed_out"])
        self.assertIsNone(result["exit_code"])
        self.assertEqual(result["stdout"], "partial\n")

    def test_exec_session_rejects_bad_env_override(self) -> None:
        with patch("xdb.sim.client.require_live_meta", return_value={"anchor_dir": "/repo"}):
            with self.assertRaises(XdbError):
                exec_session(None, ["host"], env_overrides=["NO_EQUALS"])


if __name__ == "__main__":
    unittest.main()
