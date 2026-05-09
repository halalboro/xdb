from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xdb.errors import XdbError
from xdb.sim.client import add_breakpoint, list_breakpoints, remove_breakpoint
from xdb.sim.protocol import OP_BREAKPOINT_ADD, OP_BREAKPOINT_LIST, OP_BREAKPOINT_REMOVE


class BreakpointClientTests(unittest.TestCase):
    def test_add_breakpoint_sends_poll_step_tokens(self) -> None:
        with patch("xdb.sim.client._send_request", return_value={"breakpoint_id": 1}) as send_request:
            add_breakpoint("unit", "{[get_value /done] eq 1}", poll_step_tokens=["1", "ns"])

        request = send_request.call_args.args[1]
        self.assertEqual(request["op"], OP_BREAKPOINT_ADD)
        self.assertEqual(request["args"]["condition"], "{[get_value /done] eq 1}")
        self.assertEqual(request["args"]["poll_step_tokens"], ["1", "ns"])

    def test_list_breakpoints_sends_request(self) -> None:
        with patch("xdb.sim.client._send_request", return_value={"count": 0}) as send_request:
            list_breakpoints("unit")

        request = send_request.call_args.args[1]
        self.assertEqual(request["op"], OP_BREAKPOINT_LIST)

    def test_remove_breakpoint_sends_id(self) -> None:
        with patch("xdb.sim.client._send_request", return_value={"removed": True}) as send_request:
            remove_breakpoint("unit", 7)

        request = send_request.call_args.args[1]
        self.assertEqual(request["op"], OP_BREAKPOINT_REMOVE)
        self.assertEqual(request["args"]["breakpoint_id"], 7)

    def test_remove_breakpoint_rejects_nonpositive_id(self) -> None:
        with self.assertRaises(XdbError):
            remove_breakpoint("unit", 0)


if __name__ == "__main__":
    unittest.main()
