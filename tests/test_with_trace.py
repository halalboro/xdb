from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xdb.errors import XdbError
from xdb.sim.client import with_trace_session
from xdb.sim.protocol import OP_INVOKE, OP_MEM_WRITE, OP_WITH_TRACE


class WithTraceTests(unittest.TestCase):
    def test_with_trace_wraps_xdb_sim_command_into_daemon_request(self) -> None:
        with patch(
            "xdb.sim.client._send_request",
            return_value={"ok": True, "transactions": {"event_count": 1}},
        ) as send_request:
            result = with_trace_session(
                None,
                ["xdb", "sim", "invoke", "local-transfer", "--src-addr", "0x1000", "--dst-addr", "0x2000", "--len", "4"],
                ["10", "ns"],
                step_tokens=["1", "ns"],
                transactions=True,
                axis_paths=["/tb_top/dut/axis_host_recv[0]"],
                decode_bytes=True,
            )

        self.assertTrue(result["ok"])
        request = send_request.call_args.args[1]
        self.assertEqual(request["op"], OP_WITH_TRACE)
        self.assertEqual(request["args"]["duration_tokens"], ["10", "ns"])
        self.assertEqual(request["args"]["step_tokens"], ["1", "ns"])
        self.assertTrue(request["args"]["transactions"])
        self.assertEqual(request["args"]["axis_paths"], ["/tb_top/dut/axis_host_recv[0]"])
        action_request = request["args"]["action_request"]
        self.assertEqual(action_request["op"], OP_INVOKE)
        self.assertEqual(action_request["args"]["opcode"], "local-transfer")
        self.assertEqual(action_request["args"]["src_addr"], 0x1000)
        self.assertEqual(action_request["args"]["dst_addr"], 0x2000)
        self.assertEqual(action_request["args"]["length"], 4)

    def test_with_trace_supports_mem_write_payload_parsing(self) -> None:
        with patch("xdb.sim.client._send_request", return_value={"ok": True}) as send_request:
            with_trace_session(
                None,
                ["xdb", "sim", "mem", "write", "host", "0x1000", "--hex", "deadbeef"],
                ["5", "ns"],
                step_tokens=["1", "ns"],
                transactions=True,
            )

        request = send_request.call_args.args[1]
        self.assertEqual(request["op"], OP_WITH_TRACE)
        self.assertEqual(request["args"]["action_request"]["op"], OP_MEM_WRITE)
        self.assertEqual(request["args"]["action_request"]["args"]["data_hex"], "deadbeef")

    def test_with_trace_rejects_non_xdb_sim_commands(self) -> None:
        with self.assertRaises(XdbError):
            with_trace_session(None, ["echo", "ok"], ["10", "ns"], step_tokens=["1", "ns"], transactions=True)

    def test_with_trace_requires_a_trace_mode(self) -> None:
        with self.assertRaises(XdbError):
            with_trace_session(None, ["xdb", "sim", "coyote-status"], ["10", "ns"], step_tokens=["1", "ns"])


if __name__ == "__main__":
    unittest.main()
