from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xdb.errors import XdbError
from xdb.sim.client import axis_trace_session


class AxisTraceTests(unittest.TestCase):
    def test_axis_trace_decodes_handshakes_and_bytes(self) -> None:
        interface = "/tb_top/dut/axis_host_recv[0]"
        signal_paths = {
            name: f"{interface}/{name}" for name in ("tvalid", "tready", "tdata", "tkeep", "tlast")
        }
        samples = [
            {
                "tvalid": "1",
                "tready": "1",
                "tdata": "32'h04030201",
                "tkeep": "4'b1111",
                "tlast": "0",
            },
            {
                "tvalid": "0",
                "tready": "1",
                "tdata": "32'h00000000",
                "tkeep": "4'b0000",
                "tlast": "0",
            },
            {
                "tvalid": "1",
                "tready": "1",
                "tdata": "32'h0A0000FF",
                "tkeep": "4'b1001",
                "tlast": "1",
            },
        ]
        state = {"index": 0}

        def fake_get_objects(session_name: str | None, scope: str) -> dict:
            self.assertIsNone(session_name)
            self.assertEqual(scope, interface)
            return {
                "metadata": [
                    {"path": signal_paths["tvalid"], "width": 1},
                    {"path": signal_paths["tready"], "width": 1},
                    {"path": signal_paths["tdata"], "width": 32},
                    {"path": signal_paths["tkeep"], "width": 4},
                    {"path": signal_paths["tlast"], "width": 1},
                ]
            }

        def fake_time_session(session_name: str | None) -> dict:
            self.assertIsNone(session_name)
            return {"time": "0 ns"}

        def fake_read_signals(session_name: str | None, signals: list[str]) -> dict:
            self.assertIsNone(session_name)
            self.assertEqual(set(signals), set(signal_paths.values()))
            sample = samples[state["index"]]
            return {
                "signals": [
                    {
                        "path": signal_paths[name],
                        "value": value,
                        "width": 32 if name == "tdata" else 4 if name == "tkeep" else 1,
                    }
                    for name, value in sample.items()
                ]
            }

        def fake_run_session(session_name: str | None, tokens: list[str]) -> dict:
            self.assertIsNone(session_name)
            self.assertEqual(tokens, ["1", "ns"])
            state["index"] += 1
            return {"time_after": f"{state['index']} ns"}

        with (
            patch("xdb.sim.client.get_objects", side_effect=fake_get_objects),
            patch("xdb.sim.client.time_session", side_effect=fake_time_session),
            patch("xdb.sim.client.read_signals", side_effect=fake_read_signals),
            patch("xdb.sim.client.run_session", side_effect=fake_run_session),
        ):
            result = axis_trace_session(
                None,
                [interface],
                ["3", "ns"],
                step_tokens=["1", "ns"],
                decode_bytes=True,
            )

        self.assertEqual(result["time_before"], "0 ns")
        self.assertEqual(result["time_after"], "3 ns")
        self.assertEqual(result["iterations"], 3)
        self.assertEqual(len(result["records"]), 2)

        first = result["records"][0]
        self.assertEqual(first["time"], "0 ns")
        self.assertTrue(first["handshake"])
        self.assertEqual(first["beat_index"], 0)
        self.assertEqual(first["bytes"], ["01", "02", "03", "04"])
        self.assertEqual(first["valid_bytes"], ["01", "02", "03", "04"])
        self.assertEqual(first["tlast"], "0")
        self.assertIsInstance(first["wallclock_seconds"], float)

        second = result["records"][1]
        self.assertEqual(second["time"], "2 ns")
        self.assertTrue(second["handshake"])
        self.assertEqual(second["beat_index"], 1)
        self.assertEqual(second["bytes"], ["ff", "00", "00", "0a"])
        self.assertEqual(second["valid_bytes"], ["ff", "0a"])
        self.assertEqual(second["tlast"], "1")

    def test_axis_trace_decodes_prefixed_logic_literals_through_shared_sampler(self) -> None:
        interface = "/tb_top/dut/axis_prefixed"
        signal_paths = {
            name: f"{interface}/{name}" for name in ("tvalid", "tready", "tdata", "tkeep", "tlast")
        }

        def fake_get_objects(session_name: str | None, scope: str) -> dict:
            self.assertIsNone(session_name)
            self.assertEqual(scope, interface)
            return {
                "metadata": [
                    {"path": signal_paths["tvalid"], "width": 1},
                    {"path": signal_paths["tready"], "width": 1},
                    {"path": signal_paths["tdata"], "width": 32},
                    {"path": signal_paths["tkeep"], "width": 4},
                    {"path": signal_paths["tlast"], "width": 1},
                ]
            }

        def fake_read_signals(session_name: str | None, signals: list[str]) -> dict:
            self.assertIsNone(session_name)
            self.assertEqual(set(signals), set(signal_paths.values()))
            return {
                "signals": [
                    {"path": signal_paths["tvalid"], "value": "1", "width": 1},
                    {"path": signal_paths["tready"], "value": "1", "width": 1},
                    {"path": signal_paths["tdata"], "value": "0x04030201", "width": 32},
                    {"path": signal_paths["tkeep"], "value": "0b1011", "width": 4},
                    {"path": signal_paths["tlast"], "value": "1", "width": 1},
                ]
            }

        with (
            patch("xdb.sim.client.get_objects", side_effect=fake_get_objects),
            patch("xdb.sim.client.time_session", return_value={"time": "0 ns"}),
            patch("xdb.sim.client.read_signals", side_effect=fake_read_signals),
            patch("xdb.sim.client.run_session", return_value={"time_after": "1 ns"}),
        ):
            result = axis_trace_session(
                None,
                [interface],
                ["1", "ns"],
                step_tokens=["1", "ns"],
                decode_bytes=True,
            )

        self.assertEqual(len(result["records"]), 1)
        record = result["records"][0]
        self.assertEqual(record["bytes"], ["01", "02", "03", "04"])
        self.assertEqual(record["valid_bytes"], ["01", "02", "04"])
        self.assertIsInstance(record["wallclock_seconds"], float)

    def test_axis_trace_requires_tvalid_tready_tdata(self) -> None:
        interface = "/tb_top/dut/axis_host_send[0]"
        with patch(
            "xdb.sim.client.get_objects",
            return_value={"metadata": [{"path": f"{interface}/tvalid", "width": 1}]},
        ):
            with self.assertRaises(XdbError):
                axis_trace_session(None, [interface], ["10", "ns"], step_tokens=["1", "ns"])


if __name__ == "__main__":
    unittest.main()
