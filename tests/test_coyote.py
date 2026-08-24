from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xdb.errors import XdbError
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

    def test_resident_service_csr_protocol_is_separate_and_byte_exact(self) -> None:
        controller = CoyoteSimController("/tmp/xdb-coyote-test")

        self.assertEqual(
            controller.encode_service_csr_write(0x138, 0xFEDCBA9876543210),
            bytes.fromhex("0c38010000000000001032547698badcfe"),
        )
        self.assertEqual(
            controller.encode_service_csr_read(0x138),
            bytes.fromhex("0d3801000000000000000000000000000000"),
        )
        self.assertNotEqual(
            controller.encode_service_csr_read(0x138),
            controller.encode_csr_read(0x138),
        )

        buffer = bytearray(bytes.fromhex("051032547698badcfe"))
        controller._parse_output_buffer(buffer)

        self.assertEqual(controller.get_service_csr_result_nowait(), 0xFEDCBA9876543210)
        self.assertIsNone(controller.get_csr_result_nowait())
        self.assertEqual(buffer, bytearray())

    def test_resident_service_csr_rejects_invalid_addresses_and_values(self) -> None:
        controller = CoyoteSimController("/tmp/xdb-coyote-test")

        for addr in (-8, 1, 0x1000):
            with self.subTest(addr=addr), self.assertRaises(XdbError):
                controller.encode_service_csr_read(addr)
        with self.assertRaises(XdbError):
            controller.encode_service_csr_write(0, -1)
        with self.assertRaises(XdbError):
            controller.encode_service_csr_write(0, 1 << 64)

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
