from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xdb.hardware_workflow import capture_around_command


class HardwareWorkflowTests(unittest.TestCase):
    def test_capture_around_command_orders_lifecycle_and_retains_host_evidence(self) -> None:
        backend = MagicMock()
        calls: list[str] = []
        backend.arm_ila.side_effect = lambda *args, **kwargs: calls.append("arm") or {"armed": True}
        backend.wait_ila.side_effect = lambda *args, **kwargs: calls.append("wait") or {
            "done": True
        }
        backend.upload_ila.side_effect = lambda *args, **kwargs: calls.append("upload") or {
            "output": args[2]
        }

        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "capture.vcd"
            result = capture_around_command(
                backend,
                part_hint="xcv80",
                ila_name="ila0",
                output_path=str(output),
                command=[
                    sys.executable,
                    "-c",
                    "import sys; print('out'); print('err', file=sys.stderr)",
                ],
                samples=256,
                windows=2,
                trigger_position=32,
                triggers=[],
                ltx=None,
                capture_timeout=10,
                export_format="VCD",
                host_timeout=5,
                host_cwd=td,
                host_env=["TEST_VALUE=1"],
            )
            stdout = Path(result["host"]["stdout"]).read_text(encoding="utf-8")
            stderr = Path(result["host"]["stderr"]).read_text(encoding="utf-8")

        self.assertEqual(calls, ["arm", "wait", "upload"])
        self.assertEqual(result["schema"], "xdb.ila-with-capture/v1")
        self.assertEqual(result["host"]["exit_code"], 0)
        self.assertFalse(result["host"]["timed_out"])
        self.assertEqual(stdout, "out\n")
        self.assertEqual(stderr, "err\n")
        backend.arm_ila.assert_called_once()
        backend.upload_ila.assert_called_once_with(
            "xcv80", "ila0", str(output.resolve()), timeout=10, ltx=None, export_format="VCD"
        )


if __name__ == "__main__":
    unittest.main()
