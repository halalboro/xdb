from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xdb.errors import XdbError
from xdb.sim.client import with_trace_session


class WithTraceTests(unittest.TestCase):
    def test_with_trace_wraps_xdb_command_and_collects_axis_and_transactions(self) -> None:
        completed = subprocess.CompletedProcess(
            ["xdb", "sim", "invoke"],
            0,
            stdout="issued\n",
            stderr="",
        )
        with (
            patch("xdb.sim.client.session_paths", return_value=object()),
            patch("xdb.sim.client.require_live_meta", return_value={"anchor_dir": "/repo"}),
            patch("xdb.sim.client.trace_events_clear_session") as clear_trace,
            patch(
                "xdb.sim.client.trace_events_get_session",
                return_value={"event_count": 1, "events": [{"type": "invoke"}]},
            ),
            patch(
                "xdb.sim.client._axis_trace_collect",
                return_value={"records": [{"time": "1 ns", "handshake": True}]},
            ) as axis_collect,
            patch("xdb.sim.client.subprocess.run", return_value=completed) as run_cmd,
            patch("xdb.sim.client.time.time", side_effect=[10.0, 10.25]),
        ):
            result = with_trace_session(
                None,
                ["xdb", "sim", "invoke", "local-transfer"],
                ["10", "ns"],
                step_tokens=["1", "ns"],
                transactions=True,
                axis_paths=["/tb_top/dut/axis_host_recv[0]"],
                decode_bytes=True,
            )

        clear_trace.assert_called_once_with(None)
        axis_collect.assert_called_once()
        run_cmd.assert_called_once_with(
            [sys.executable, "-m", "xdb.cli", "sim", "invoke", "local-transfer"],
            cwd="/repo",
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result["command"]["exit_code"], 0)
        self.assertEqual(result["command"]["stdout"], "issued\n")
        self.assertAlmostEqual(result["command"]["duration_seconds"], 0.25)
        self.assertEqual(result["transactions"]["event_count"], 1)
        self.assertIn("axis", result)
        self.assertEqual(result["axis"]["records"][0]["time"], "1 ns")

    def test_with_trace_transactions_only_runs_observation_window(self) -> None:
        completed = subprocess.CompletedProcess(["echo", "ok"], 0, stdout="ok\n", stderr="")
        with (
            patch("xdb.sim.client.session_paths", return_value=object()),
            patch("xdb.sim.client.require_live_meta", return_value={"anchor_dir": "/repo"}),
            patch("xdb.sim.client.trace_events_clear_session"),
            patch(
                "xdb.sim.client.trace_events_get_session",
                return_value={"event_count": 0, "events": []},
            ),
            patch(
                "xdb.sim.client.run_session",
                return_value={"time_before": "5 ns", "time_after": "15 ns"},
            ) as run_session,
            patch("xdb.sim.client.subprocess.run", return_value=completed),
            patch("xdb.sim.client.time.time", side_effect=[20.0, 20.1]),
        ):
            result = with_trace_session(
                None,
                ["echo", "ok"],
                ["10", "ns"],
                step_tokens=["1", "ns"],
                transactions=True,
            )

        run_session.assert_called_once_with(None, ["10", "ns"])
        self.assertNotIn("axis", result)
        self.assertEqual(result["observation"]["time_before"], "5 ns")
        self.assertEqual(result["observation"]["time_after"], "15 ns")

    def test_with_trace_requires_a_trace_mode(self) -> None:
        with self.assertRaises(XdbError):
            with_trace_session(None, ["echo", "ok"], ["10", "ns"], step_tokens=["1", "ns"])


if __name__ == "__main__":
    unittest.main()
