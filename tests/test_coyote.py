from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xdb.sim.coyote import CoyoteSimController


class CoyoteSimControllerTests(unittest.TestCase):
    def test_host_memory_status_reports_segments_and_counters(self) -> None:
        controller = CoyoteSimController("/tmp/xdb-coyote-test")
        controller._segments[0x2000] = bytearray(8)
        controller._segments[0x1000] = bytearray(4)
        controller._host_write_count = 3
        controller._host_read_count = 1
        controller._last_protocol_error = "boom"
        controller._irq_events.put({"pid": 7, "value": 9})

        status = controller.host_memory_status()

        self.assertEqual(
            status["mapped_segments"],
            [
                {
                    "addr": 0x1000,
                    "addr_hex": "0x1000",
                    "size": 4,
                    "end": 0x1004,
                    "end_hex": "0x1004",
                },
                {
                    "addr": 0x2000,
                    "addr_hex": "0x2000",
                    "size": 8,
                    "end": 0x2008,
                    "end_hex": "0x2008",
                },
            ],
        )
        self.assertEqual(status["host_write_count"], 3)
        self.assertEqual(status["host_read_count"], 1)
        self.assertEqual(status["pending_irqs"], 1)
        self.assertEqual(status["last_protocol_error"], "boom")

    def test_reset_host_memory_unmaps_all_segments_and_clears_accounting(self) -> None:
        controller = CoyoteSimController("/tmp/xdb-coyote-test")
        controller._segments[0x2000] = bytearray(8)
        controller._segments[0x1000] = bytearray(4)
        controller._host_write_count = 5
        controller._host_read_count = 2
        controller._last_protocol_error = "bad read"
        controller._irq_events.put({"pid": 1, "value": 2})
        controller.write_input = Mock()

        result = controller.reset_host_memory()

        controller.write_input.assert_called_once_with(
            controller._encode_user_unmap(0x1000) + controller._encode_user_unmap(0x2000)
        )
        self.assertEqual(result["space"], "host")
        self.assertTrue(result["reset"])
        self.assertEqual(result["unmapped_count"], 2)
        self.assertEqual(
            result["unmapped_segments"],
            [
                {
                    "addr": 0x1000,
                    "addr_hex": "0x1000",
                    "size": 4,
                    "end": 0x1004,
                    "end_hex": "0x1004",
                },
                {
                    "addr": 0x2000,
                    "addr_hex": "0x2000",
                    "size": 8,
                    "end": 0x2008,
                    "end_hex": "0x2008",
                },
            ],
        )
        self.assertEqual(result["mapped_segments"], [])
        self.assertEqual(result["host_write_count"], 0)
        self.assertEqual(result["host_read_count"], 0)
        self.assertEqual(result["pending_irqs"], 1)
        self.assertEqual(result["last_protocol_error"], "")
        self.assertEqual(controller._segments, {})


if __name__ == "__main__":
    unittest.main()
