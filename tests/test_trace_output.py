from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xdb.cli import _emit_text, _format_with_trace_ndjson, _format_with_trace_summary


class TraceOutputTests(unittest.TestCase):
    def test_with_trace_ndjson_contains_window_action_transactions_and_axis(self) -> None:
        result = {
            "duration": "10 ns",
            "step": "1 ns",
            "time_before": "0 ns",
            "time_after": "10 ns",
            "action": {"op": "invoke", "result": {"ok": True}},
            "transactions": {"events": [{"type": "invoke", "opcode": "local-transfer"}]},
            "axis": {"records": [{"interface": "/axis", "time": "1 ns", "handshake": True}]},
            "correlation": {"timeline": [{"kind": "transaction", "label": "invoke:local-transfer"}]},
        }

        lines = [json.loads(line) for line in _format_with_trace_ndjson(result).splitlines()]

        self.assertEqual(
            [line["kind"] for line in lines],
            ["window", "action", "transaction", "axis", "correlation"],
        )
        self.assertEqual(lines[0]["time_before"], "0 ns")
        self.assertEqual(lines[2]["opcode"], "local-transfer")
        self.assertEqual(lines[3]["interface"], "/axis")
        self.assertEqual(lines[4]["entry"]["label"], "invoke:local-transfer")

    def test_with_trace_summary_is_compact_human_readable_text(self) -> None:
        result = {
            "duration": "10 ns",
            "step": "1 ns",
            "time_before": "0 ns",
            "time_after": "10 ns",
            "action": {"op": "invoke"},
            "transactions": {"events": [{"type": "invoke", "opcode": "local-transfer"}]},
            "axis": {"records": [{"interface": "/axis", "time": "1 ns", "handshake": True}]},
            "correlation": {
                "links": [
                    {
                        "transaction_label": "invoke:local-transfer",
                        "axis_label": "/axis beat 0",
                        "delta_wallclock_seconds": 0.1,
                    }
                ]
            },
        }

        summary = _format_with_trace_summary(result)

        self.assertIn("with-trace summary", summary)
        self.assertIn("action: invoke", summary)
        self.assertIn("transactions: 1 event(s)", summary)
        self.assertIn("axis: 1 record(s)", summary)
        self.assertIn("correlation links: 1", summary)
        self.assertIn("tx=invoke:local-transfer axis=/axis beat 0", summary)

    def test_emit_text_writes_output_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "trace.ndjson"
            _emit_text('{"kind":"window"}', str(out))
            self.assertEqual(out.read_text(), '{"kind":"window"}\n')


if __name__ == "__main__":
    unittest.main()
