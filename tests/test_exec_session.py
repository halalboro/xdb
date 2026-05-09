from __future__ import annotations

import subprocess
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
            work_dir = runtime_root / "helios-coyote.sim" / "sim_1" / "behav" / "xsim"
            meta = {
                "anchor_dir": str(repo),
                "runtime_root": str(runtime_root),
                "work_dir": str(work_dir),
                "socket_path": str(Path(tmpdir) / "control.sock"),
                "package_runtime": "/nix/store/demo/project/sim",
                "project": str(runtime_root / "helios-coyote.xpr"),
                "simset": "sim_1",
                "top": "tb_user",
                "mode": "behavioral",
                "state": "ready",
            }
            completed = subprocess.CompletedProcess(
                ["helios-host", "--input-hex", "0102"],
                0,
                stdout="ok\n",
                stderr="",
            )
            old_cwd = Path.cwd()
            try:
                import os

                os.chdir(repo)
                with (
                    patch("xdb.sim.client.require_live_meta", return_value=meta),
                    patch("xdb.sim.client.subprocess.run", return_value=completed) as run_cmd,
                    patch("xdb.sim.client.time.time", side_effect=[10.0, 10.25]),
                    patch("xdb.sim.client._now_iso", side_effect=["start", "finish"]),
                ):
                    result = exec_session(
                        "versal",
                        ["--", "helios-host", "--input-hex", "0102"],
                        env_overrides=["EXTRA=1"],
                    )
            finally:
                os.chdir(old_cwd)

        run_kwargs = run_cmd.call_args.kwargs
        self.assertEqual(run_cmd.call_args.args[0], ["helios-host", "--input-hex", "0102"])
        self.assertEqual(run_kwargs["cwd"], str(repo))
        self.assertEqual(run_kwargs["env"]["XDB_SIM_SESSION"], "versal")
        self.assertEqual(run_kwargs["env"]["XDB_SIM_RUNTIME_ROOT"], str(runtime_root))
        self.assertEqual(run_kwargs["env"]["XDB_SIM_WORK_DIR"], str(work_dir))
        self.assertEqual(run_kwargs["env"]["COYOTE_SIM_DIR"], str(runtime_root / "sim"))
        self.assertEqual(run_kwargs["env"]["EXTRA"], "1")
        self.assertTrue(result["ok"])
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["stdout"], "ok\n")
        self.assertEqual(result["duration_seconds"], 0.25)
        self.assertEqual(result["env"]["EXTRA"], "1")
        self.assertEqual(result["session"]["name"], "versal")

    def test_exec_session_timeout_returns_structured_failure(self) -> None:
        with (
            patch(
                "xdb.sim.client.require_live_meta",
                return_value={"anchor_dir": "/repo", "runtime_root": "/runtime", "state": "ready"},
            ),
            patch(
                "xdb.sim.client.subprocess.run",
                side_effect=subprocess.TimeoutExpired(["host"], 1.0, output="partial", stderr="late"),
            ),
            patch("xdb.sim.client.time.time", side_effect=[1.0, 2.5]),
            patch("xdb.sim.client._now_iso", side_effect=["start", "finish"]),
        ):
            result = exec_session(None, ["host"], timeout_seconds=1.0)

        self.assertFalse(result["ok"])
        self.assertTrue(result["timed_out"])
        self.assertIsNone(result["exit_code"])
        self.assertEqual(result["stdout"], "partial")
        self.assertEqual(result["stderr"], "late")

    def test_exec_session_rejects_bad_env_override(self) -> None:
        with patch("xdb.sim.client.require_live_meta", return_value={"anchor_dir": "/repo"}):
            with self.assertRaises(XdbError):
                exec_session(None, ["host"], env_overrides=["NO_EQUALS"])


if __name__ == "__main__":
    unittest.main()
