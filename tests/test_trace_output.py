from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xdb.cli import (
    _emit_text,
    _format_doctor_summary,
    _format_provenance_summary,
    _format_with_trace_ndjson,
    _format_with_trace_summary,
)
from xdb.sim.trace_correlation import correlate_trace


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

    def test_doctor_summary_reports_issues_and_suggestions(self) -> None:
        summary = _format_doctor_summary(
            {
                "ok": False,
                "session": "unit",
                "session_id": "unit-123",
                "anchor_dir": "/repo",
                "checks": [
                    {"name": "daemon_responsive", "ok": False, "severity": "error", "detail": "timeout"},
                    {"name": "vivado_log_exists", "ok": False, "severity": "warning"},
                ],
                "suggestions": ["run: xdb sim close --force"],
                "paths": {"session_dir": "/repo/.xdb/sessions/unit", "socket": "/cache/s.sock"},
            }
        )

        self.assertIn("doctor summary", summary)
        self.assertIn("1 error(s), 1 warning(s)", summary)
        self.assertIn("daemon_responsive", summary)
        self.assertIn("run: xdb sim close --force", summary)

    def test_provenance_summary_reports_runtime_state(self) -> None:
        summary = _format_provenance_summary(
            {
                "session": "unit",
                "session_id": "unit-123",
                "anchor_dir": "/repo",
                "requested": {"simset": "sim_1", "mode": "behavioral", "top": "tb"},
                "live_session": {"present": True, "state": "ready", "pid": 42},
                "runtime": {
                    "available": True,
                    "package_runtime": "/nix/store/pkg/project/sim",
                    "workspace": "/repo/.build/sim",
                    "workspace_exists": True,
                    "needs_stage": False,
                    "stage_source_matches_package": True,
                    "stage_fingerprint_matches_package": True,
                },
                "comparisons": {"live_session_matches_request": True},
            }
        )

        self.assertIn("provenance summary", summary)
        self.assertIn("live: yes state=ready pid=42", summary)
        self.assertIn("needs stage: no", summary)
        self.assertIn("live session matches request: yes", summary)

    def test_correlation_window_and_mode_filter_links_expected_records(self) -> None:
        result = correlate_trace(
            {
                "events": [
                    {
                        "type": "invoke",
                        "opcode": "local-transfer",
                        "time": "0 ns",
                        "wallclock_seconds": 1.0,
                    },
                    {
                        "type": "host_read",
                        "addr_hex": "0x1000",
                        "time": "20 ns",
                        "wallclock_seconds": 2.0,
                    },
                ]
            },
            {
                "records": [
                    {"interface": "/axis", "beat_index": 0, "time": "1 ns", "wallclock_seconds": 1.1},
                    {"interface": "/axis", "beat_index": 1, "time": "30 ns", "wallclock_seconds": 2.1},
                ]
            },
            correlate_by="opcode",
            window_tokens=["5", "ns"],
        )

        self.assertEqual(result["correlate_by"], "opcode")
        self.assertEqual(result["window"], "5 ns")
        self.assertEqual(result["link_count"], 1)
        self.assertEqual(result["skipped_by_mode"], 1)
        self.assertEqual(result["links"][0]["axis_label"], "/axis beat 0")

    def test_emit_text_writes_output_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "trace.ndjson"
            _emit_text('{"kind":"window"}', str(out))
            self.assertEqual(out.read_text(), '{"kind":"window"}\n')


if __name__ == "__main__":
    unittest.main()
