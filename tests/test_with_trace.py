from __future__ import annotations

import sys
import unittest
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xdb.errors import XdbError
from xdb.sim.client import with_trace_session
from xdb.sim.sim_time import parse_sim_time
from xdb.sim.protocol import (
    OP_INVOKE,
    OP_MEM_WRITE,
    OP_RUN,
    OP_STEP,
    OP_UNTIL,
    OP_UNTIL_SIGNAL,
    OP_WITH_TRACE,
    make_request,
)
from xdb.sim.with_trace import WithTraceRunner


class _FakeWithTraceDriver:
    def __init__(self) -> None:
        self.time_seconds = Decimal("0")
        self.run_tokens: list[list[str]] = []
        self._sim_advance_hook = None
        self._coyote = None

    def time(self) -> dict[str, str]:
        ns_value = self.time_seconds / Decimal("1e-9")
        text = format(ns_value.normalize(), "f").rstrip("0").rstrip(".") or "0"
        return {"time": f"{text} ns"}

    def run(self, tokens: list[str]) -> dict[str, str]:
        before = self.time()["time"]
        self.run_tokens.append(list(tokens))
        self.time_seconds += parse_sim_time(" ".join(tokens))
        after = self.time()["time"]
        if self._sim_advance_hook is not None:
            self._sim_advance_hook(before, after)
        return {"time_before": before, "time_after": after, "duration": " ".join(tokens)}

    def trace_events_clear(self) -> dict[str, bool]:
        return {"cleared": True}

    def trace_events_get(self) -> dict[str, Any]:
        return {"event_count": 0, "events": []}

    @contextmanager
    def sim_advance_hook(self, hook: Callable[[str, str], None]) -> Iterator[None]:
        previous = self._sim_advance_hook
        self._sim_advance_hook = hook
        try:
            yield
        finally:
            self._sim_advance_hook = previous

    @contextmanager
    def coyote_trace_context(
        self,
        _provider: Callable[[], dict[str, object]],
        *,
        enabled: bool = True,
    ) -> Iterator[bool]:
        yield False and enabled


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
                correlate_by="opcode",
                correlate_window_tokens=["5", "ns"],
            )

        self.assertTrue(result["ok"])
        request = send_request.call_args.args[1]
        self.assertEqual(request["op"], OP_WITH_TRACE)
        self.assertEqual(request["args"]["duration_tokens"], ["10", "ns"])
        self.assertEqual(request["args"]["step_tokens"], ["1", "ns"])
        self.assertTrue(request["args"]["transactions"])
        self.assertEqual(request["args"]["axis_paths"], ["/tb_top/dut/axis_host_recv[0]"])
        self.assertEqual(request["args"]["correlate_by"], "opcode")
        self.assertEqual(request["args"]["correlate_window_tokens"], ["5", "ns"])
        action_request = request["args"]["action_request"]
        self.assertEqual(action_request["op"], OP_INVOKE)
        self.assertEqual(action_request["args"]["opcode"], "local-transfer")
        self.assertEqual(action_request["args"]["src_addr"], 0x1000)
        self.assertEqual(action_request["args"]["dst_addr"], 0x2000)
        self.assertEqual(action_request["args"]["length"], 4)

    def test_with_trace_supports_run_step_and_until_wrapped_commands(self) -> None:
        cases = [
            (["xdb", "sim", "run", "50", "ns"], OP_RUN, {"tokens": ["50", "ns"]}),
            (["xdb", "sim", "step", "25", "ns"], OP_STEP, {"time_tokens": ["25", "ns"]}),
            (["xdb", "sim", "step", "3"], OP_STEP, {"count": 3}),
            (
                ["xdb", "sim", "until", "--step", "5", "ns", "{[get_value /done] eq \"1\"}"],
                OP_UNTIL,
                {"step_tokens": ["5", "ns"], "expr": "{[get_value /done] eq \"1\"}"},
            ),
            (
                ["xdb", "sim", "until-signal", "--step", "5", "ns", "/done", "1"],
                OP_UNTIL_SIGNAL,
                {"step_tokens": ["5", "ns"], "signal": "/done", "value": "1"},
            ),
        ]
        for command, expected_op, expected_args in cases:
            with self.subTest(command=command):
                with patch("xdb.sim.client._send_request", return_value={"ok": True}) as send_request:
                    with_trace_session(
                        None,
                        command,
                        ["10", "ns"],
                        step_tokens=["1", "ns"],
                        transactions=True,
                    )
                action_request = send_request.call_args.args[1]["args"]["action_request"]
                self.assertEqual(action_request["op"], expected_op)
                for key, value in expected_args.items():
                    self.assertEqual(action_request["args"][key], value)

    def test_with_trace_exec_mode_sends_external_command_request(self) -> None:
        with patch("xdb.sim.client._send_request", return_value={"ok": True}) as send_request:
            with_trace_session(
                None,
                ["--", "host-test", "--input", "0102"],
                ["10", "ns"],
                step_tokens=["1", "ns"],
                transactions=True,
                exec_mode=True,
                exec_cwd="/repo",
                exec_env_overrides=["EXTRA=1"],
                exec_timeout_seconds=2.0,
                exec_expect_exit_code=3,
                exec_clean_env=True,
            )

        request = send_request.call_args.args[1]
        self.assertEqual(request["op"], OP_WITH_TRACE)
        self.assertEqual(request["args"]["exec_command"], ["host-test", "--input", "0102"])
        self.assertEqual(request["args"]["exec_cwd"], "/repo")
        self.assertEqual(request["args"]["exec_env_overrides"], ["EXTRA=1"])
        self.assertEqual(request["args"]["exec_timeout_seconds"], 2.0)
        self.assertEqual(request["args"]["exec_expect_exit_code"], 3)
        self.assertTrue(request["args"]["exec_clean_env"])
        self.assertEqual(request["args"]["exec_base_env"], {})
        self.assertNotIn("action_request", request["args"])

    def test_with_trace_exec_until_exit_sends_external_command_request_without_duration(self) -> None:
        with patch("xdb.sim.client._send_request", return_value={"ok": True}) as send_request:
            with_trace_session(
                None,
                ["--", "host-test", "--input", "0102"],
                [],
                step_tokens=["1", "ns"],
                transactions=True,
                exec_mode=True,
                exec_until_exit=True,
            )

        request = send_request.call_args.args[1]
        self.assertEqual(request["op"], OP_WITH_TRACE)
        self.assertEqual(request["args"]["duration_tokens"], [])
        self.assertTrue(request["args"]["exec_until_exit"])
        self.assertEqual(request["args"]["exec_command"], ["host-test", "--input", "0102"])

    def test_with_trace_rejects_missing_duration_without_exec_until_exit(self) -> None:
        with self.assertRaises(XdbError):
            with_trace_session(
                None,
                ["--", "host-test"],
                [],
                step_tokens=["1", "ns"],
                transactions=True,
                exec_mode=True,
            )

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

    def test_with_trace_sampled_run_caps_last_step_to_remaining_duration(self) -> None:
        driver = _FakeWithTraceDriver()
        runner = WithTraceRunner(driver, lambda _op, _args: {})

        result = runner.run(
            {
                "action_request": make_request(OP_RUN, tokens=["5", "ns"]),
                "duration_tokens": ["1", "ns"],
                "step_tokens": ["10", "ns"],
                "transactions": True,
                "axis_paths": [],
            }
        )

        self.assertEqual(driver.run_tokens, [["5", "ns"], ["1", "ns"]])
        self.assertEqual(result["time_after"], "6 ns")
        self.assertEqual(result["action"]["result"]["sample_iterations"], 1)
        self.assertEqual(result["observation_iterations"], 1)

    def test_with_trace_exec_until_exit_runs_without_observation_duration(self) -> None:
        driver = _FakeWithTraceDriver()
        runner = WithTraceRunner(
            driver,
            lambda _op, _args: {},
            meta={
                "session_name": "unit",
                "anchor_dir": str(Path.cwd()),
                "runtime_root": "/tmp/xdb-runtime",
                "work_dir": "/tmp/xdb-runtime/work",
                "socket_path": "/tmp/xdb.sock",
            },
        )

        result = runner.run(
            {
                "exec_command": [
                    sys.executable,
                    "-c",
                    "import os,time; print(os.environ['COYOTE_SIM_DIR']); time.sleep(0.02)",
                ],
                "duration_tokens": [],
                "step_tokens": ["1", "ns"],
                "transactions": True,
                "axis_paths": [],
                "exec_clean_env": True,
                "exec_until_exit": True,
            }
        )

        self.assertEqual(result["trace_until"], "exec_exit")
        self.assertIsNone(result["duration"])
        self.assertEqual(result["observation_iterations"], 0)
        self.assertTrue(result["action"]["result"]["ok"])
        self.assertEqual(result["action"]["result"]["stdout"].strip(), "/tmp/xdb-runtime")
        self.assertGreaterEqual(len(driver.run_tokens), 1)

    def test_with_trace_exec_action_advances_sim_and_injects_env(self) -> None:
        driver = _FakeWithTraceDriver()
        runner = WithTraceRunner(
            driver,
            lambda _op, _args: {},
            meta={
                "session_name": "unit",
                "anchor_dir": str(Path.cwd()),
                "runtime_root": "/tmp/xdb-runtime",
                "work_dir": "/tmp/xdb-runtime/work",
                "socket_path": "/tmp/xdb.sock",
            },
        )

        result = runner._with_trace_exec_action(
            [
                sys.executable,
                "-c",
                "import os,time; print(os.environ['COYOTE_SIM_DIR']); time.sleep(0.02)",
            ],
            ["1", "ns"],
            cwd=None,
            env_overrides=[],
            timeout_seconds=None,
            expect_exit_code=0,
            clean_env=True,
            base_env={},
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["stdout"].strip(), "/tmp/xdb-runtime")
        self.assertEqual(result["env"]["COYOTE_SIM_DIR"], "/tmp/xdb-runtime")
        self.assertGreaterEqual(len(driver.run_tokens), 1)

    def test_with_trace_wrapped_argparse_errors_raise_xdb_error(self) -> None:
        with self.assertRaises(XdbError):
            with_trace_session(
                None,
                ["xdb", "sim", "invoke"],
                ["10", "ns"],
                step_tokens=["1", "ns"],
                transactions=True,
            )

    def test_with_trace_rejects_non_xdb_sim_commands(self) -> None:
        with self.assertRaises(XdbError):
            with_trace_session(None, ["echo", "ok"], ["10", "ns"], step_tokens=["1", "ns"], transactions=True)

    def test_with_trace_requires_a_trace_mode(self) -> None:
        with self.assertRaises(XdbError):
            with_trace_session(None, ["xdb", "sim", "coyote-status"], ["10", "ns"], step_tokens=["1", "ns"])


if __name__ == "__main__":
    unittest.main()
