from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xdb.sim.coyote import CoyoteSimController
from xdb.sim.vivado_coyote_runtime import VivadoCoyoteMixin


class _FakeTraceHost(VivadoCoyoteMixin):
    def __init__(self, controller: CoyoteSimController):
        self.runtime_root = ""
        self._coyote = controller
        self._time_index = 0
        self._times = ["0 ns", "5 ns"]

    def run(self, tokens: list[str]) -> dict[str, str]:
        self._time_index = min(self._time_index + 1, len(self._times) - 1)
        self._coyote.record_trace_event(
            "invoke",
            opcode="local-transfer",
            src_addr=0x1000,
            dst_addr=0x2000,
        )
        self._coyote.record_trace_event(
            "host_read",
            addr=0x1000,
            addr_hex="0x1000",
            size=4,
            host_read_count=1,
        )
        return {"time_after": self._times[self._time_index], "duration": " ".join(tokens)}

    def time(self) -> dict[str, str]:
        return {"time": self._times[self._time_index]}


class TransactionTraceTests(unittest.TestCase):
    def test_controller_records_memory_and_output_events(self) -> None:
        controller = CoyoteSimController("/tmp/xdb-coyote-trace-test")
        controller.write_input = Mock()

        controller.map_host_memory(0x1000, 4)
        controller.write_host_memory(0x1000, bytes.fromhex("deadbeef"))
        controller._handle_host_read(0x1000, 4)
        controller._handle_host_write(0x1000, bytes.fromhex("01020304"))

        events = controller.get_trace_events()
        self.assertEqual(
            [event["type"] for event in events],
            [
                "mem_map",
                "mem_write",
                "host_read",
                "host_write",
            ],
        )
        self.assertEqual(events[0]["addr_hex"], "0x1000")
        self.assertEqual(events[1]["data_hex"], "deadbeef")
        self.assertEqual(events[2]["size"], 4)
        self.assertEqual(events[3]["data_hex"], "01020304")

    def test_resident_service_csr_runtime_access_uses_separate_protocol(self) -> None:
        controller = CoyoteSimController("/tmp/xdb-coyote-trace-test")
        host = _FakeTraceHost(controller)

        def respond_to_read(payload: bytes, *, pump: object) -> None:
            self.assertEqual(payload[0], 13)
            self.assertIsNotNone(pump)
            controller._service_csr_results.put(0x51534832)

        controller.write_input = Mock(side_effect=respond_to_read)
        read = host.coyote_service_csr_read(0x0, timeout_seconds=1.0)

        self.assertEqual(read["space"], "resident-service")
        self.assertEqual(read["value"], 0x51534832)
        self.assertEqual(read["addr_hex"], "0x0")
        self.assertEqual(controller.get_trace_events()[0]["type"], "service_csr_read_request")

        controller.write_input = Mock()
        write = host.coyote_service_csr_write(0x100, 0x1234)

        self.assertTrue(write["written"])
        self.assertEqual(write["space"], "resident-service")
        self.assertEqual(controller.write_input.call_args.args[0][0], 12)

    def test_trace_transactions_returns_window_and_opcode_filter(self) -> None:
        controller = CoyoteSimController("/tmp/xdb-coyote-trace-test")
        host = _FakeTraceHost(controller)

        result = host.trace_transactions(["5", "ns"], opcode_filter="local-transfer")

        self.assertEqual(result["time_before"], "0 ns")
        self.assertEqual(result["time_after"], "5 ns")
        self.assertEqual(result["duration"], "5 ns")
        self.assertEqual(result["opcode_filter"], "local-transfer")
        self.assertEqual(result["event_count"], 1)
        self.assertEqual(result["events"][0]["type"], "invoke")
        self.assertEqual(result["events"][0]["opcode"], "local-transfer")

    def test_trace_context_provider_adds_sim_time_to_events(self) -> None:
        controller = CoyoteSimController("/tmp/xdb-coyote-trace-test")
        controller.set_trace_context_provider(lambda: {"time": "5 ns", "time_source": "test"})

        event = controller.record_trace_event("invoke", opcode="local-transfer")

        self.assertEqual(event["time"], "5 ns")
        self.assertEqual(event["time_source"], "test")
        self.assertEqual(event["type"], "invoke")


if __name__ == "__main__":
    unittest.main()
