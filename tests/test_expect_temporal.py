from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xdb.errors import XdbError
from xdb.sim.client import expect_condition_session, expect_stream_output_session
from xdb.sim.protocol import OP_EXPECT_CONDITION


class TemporalExpectationTests(unittest.TestCase):
    def test_expect_condition_sends_daemon_request(self) -> None:
        with patch("xdb.sim.client._send_request", return_value={"passed": True}) as send_request:
            expect_condition_session("unit", "{[get_value /done] eq 1}", ["10", "ns"])

        request = send_request.call_args.args[1]
        self.assertEqual(request["op"], OP_EXPECT_CONDITION)
        self.assertEqual(request["args"]["expr"], "{[get_value /done] eq 1}")
        self.assertEqual(request["args"]["within_tokens"], ["10", "ns"])

    def test_expect_stream_output_passes_when_axis_trace_has_handshake(self) -> None:
        with patch(
            "xdb.sim.client.axis_trace_session",
            return_value={"records": [{"interface": "/axis", "handshake": True, "time": "5 ns"}]},
        ) as axis_trace:
            result = expect_stream_output_session(
                "unit",
                "/axis",
                ["10", "ns"],
                step_tokens=["1", "ns"],
                decode_bytes=True,
            )

        self.assertTrue(result["passed"])
        self.assertEqual(result["record_count"], 1)
        self.assertEqual(axis_trace.call_args.args[1], ["/axis"])
        self.assertEqual(axis_trace.call_args.args[2], ["10", "ns"])
        self.assertEqual(axis_trace.call_args.kwargs["step_tokens"], ["1", "ns"])
        self.assertTrue(axis_trace.call_args.kwargs["only_handshakes"])
        self.assertTrue(axis_trace.call_args.kwargs["decode_bytes"])

    def test_expect_stream_output_fails_without_handshake(self) -> None:
        with patch("xdb.sim.client.axis_trace_session", return_value={"records": []}):
            with self.assertRaises(XdbError):
                expect_stream_output_session("unit", "/axis", ["10", "ns"], step_tokens=["1", "ns"])


if __name__ == "__main__":
    unittest.main()
