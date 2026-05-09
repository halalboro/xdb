from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xdb.sim.client import run_session
from xdb.sim.protocol import OP_RUN


class SimRequestTimeoutTests(unittest.TestCase):
    def test_run_session_passes_daemon_response_timeout(self) -> None:
        with patch("xdb.sim.client._send_request", return_value={"ok": True}) as send_request:
            run_session("unit", ["500", "ns"], timeout_seconds=12.5)

        self.assertEqual(send_request.call_args.args[0], "unit")
        request = send_request.call_args.args[1]
        self.assertEqual(request["op"], OP_RUN)
        self.assertEqual(request["args"]["tokens"], ["500", "ns"])
        self.assertEqual(request["args"]["timeout_seconds"], 12.5)
        self.assertEqual(send_request.call_args.kwargs["timeout_seconds"], 17.5)


if __name__ == "__main__":
    unittest.main()
