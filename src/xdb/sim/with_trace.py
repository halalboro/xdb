from __future__ import annotations

import time
from typing import Any, Callable, cast

from ..errors import XdbError
from .axis_trace import AxisTraceSampler
from .protocol import (
    OP_CLEAR_COMPLETED,
    OP_COMPLETED,
    OP_COYOTE_STATUS,
    OP_CSR_READ,
    OP_CSR_WRITE,
    OP_INVOKE,
    OP_IRQ_WAIT,
    OP_MEM_LIST,
    OP_MEM_MAP,
    OP_MEM_READ,
    OP_MEM_RESET,
    OP_MEM_UNMAP,
    OP_MEM_WRITE,
    OP_RUN,
    OP_STEP,
    OP_UNTIL,
    OP_UNTIL_SIGNAL,
)
from .sim_time import parse_duration_tokens, parse_sim_time
from .tcl_helpers import _tcl_string
from .trace_correlation import correlate_trace


_SUPPORTED_WITH_TRACE_OPS = {
    OP_RUN,
    OP_STEP,
    OP_UNTIL,
    OP_UNTIL_SIGNAL,
    OP_INVOKE,
    OP_MEM_MAP,
    OP_MEM_UNMAP,
    OP_MEM_LIST,
    OP_MEM_RESET,
    OP_MEM_WRITE,
    OP_MEM_READ,
    OP_CSR_READ,
    OP_CSR_WRITE,
    OP_COMPLETED,
    OP_CLEAR_COMPLETED,
    OP_IRQ_WAIT,
    OP_COYOTE_STATUS,
}


def _arg_optional_int(args: dict[str, Any], name: str) -> int | None:
    value = args.get(name)
    return None if value is None else int(value)


def _arg_optional_float(args: dict[str, Any], name: str) -> float | None:
    value = args.get(name)
    return None if value is None else float(value)


class WithTraceRunner:
    def __init__(
        self,
        driver: Any,
        dispatch: Callable[[str, dict[str, Any]], dict[str, Any]],
    ):
        self.driver = driver
        self.dispatch = dispatch

    def run(self, args: dict[str, Any]) -> dict[str, Any]:
        action_request = cast(dict[str, Any], args.get("action_request") or {})
        action_op = str(action_request.get("op") or "")
        action_args = cast(dict[str, Any], action_request.get("args") or {})
        if action_op not in _SUPPORTED_WITH_TRACE_OPS:
            raise XdbError(
                "with-trace currently supports a limited set of 'xdb sim' commands; "
                f"unsupported op: {action_op}"
            )

        duration_tokens = [str(v) for v in list(args.get("duration_tokens") or []) if str(v)]
        step_tokens = [str(v) for v in list(args.get("step_tokens") or []) if str(v)]
        if not duration_tokens:
            raise XdbError("missing trace duration")
        if not step_tokens:
            raise XdbError("missing trace step")
        _duration_text, duration_value = parse_duration_tokens(duration_tokens)
        _step_text, step_value = parse_duration_tokens(step_tokens)
        if duration_value <= 0 or step_value <= 0:
            raise XdbError("trace duration and step must be > 0")

        transactions = bool(args.get("transactions"))
        axis_paths = [str(v) for v in list(args.get("axis_paths") or []) if str(v)]
        decode_bytes = bool(args.get("decode_bytes"))
        lane_order = str(args.get("lane_order") or "low-to-high")
        include_idle = bool(args.get("include_idle"))
        only_handshakes = bool(args.get("only_handshakes"))
        correlate_by = str(args.get("correlate_by") or "nearest")
        correlate_window_tokens = [
            str(v) for v in list(args.get("correlate_window_tokens") or []) if str(v)
        ]

        axis_sampler = AxisTraceSampler(
            self.driver,
            axis_paths,
            decode_bytes=decode_bytes,
            lane_order=lane_order,
            include_idle=include_idle,
            only_handshakes=only_handshakes,
        )

        time_before = str(self.driver.time().get("time") or "")
        current_trace_time = time_before
        if transactions:
            self.driver.trace_events_clear()
        axis_sampler.sample(time_before)
        previous_hook = self.driver._sim_advance_hook
        previous_context_provider: Callable[[], dict[str, object]] | None = None
        if transactions and self.driver._coyote is not None:
            previous_context_provider = self.driver._coyote.set_trace_context_provider(
                lambda: {"time": current_trace_time, "time_source": "last_sample"}
            )

        def handle_sim_advance(_before: str, after: str) -> None:
            nonlocal current_trace_time
            current_trace_time = after
            axis_sampler.sample(after)

        self.driver._sim_advance_hook = handle_sim_advance
        try:
            action_result = self._with_trace_action(action_op, action_args, step_tokens)
            action_time_after = str(self.driver.time().get("time") or "")
            current_time_value = parse_sim_time(action_time_after)
            end_time_value = current_time_value + duration_value
            iterations = 0
            while current_time_value < end_time_value:
                run_result = self.driver.run(step_tokens)
                current_time_text = str(run_result.get("time_after") or "")
                current_time_value = parse_sim_time(current_time_text)
                iterations += 1
            time_after = str(self.driver.time().get("time") or "")
        finally:
            self.driver._sim_advance_hook = previous_hook
            if transactions and self.driver._coyote is not None:
                self.driver._coyote.set_trace_context_provider(previous_context_provider)

        result: dict[str, Any] = {
            "action": {
                "op": action_op,
                "args": action_args,
                "result": action_result,
            },
            "duration": " ".join(duration_tokens),
            "step": " ".join(step_tokens),
            "time_before": time_before,
            "time_after": time_after,
            "observation_iterations": iterations,
        }
        transaction_result: dict[str, Any] | None = None
        axis_result: dict[str, Any] | None = None
        if transactions:
            transaction_result = self.driver.trace_events_get()
            result["transactions"] = transaction_result
        if axis_paths:
            axis_result = {
                "interfaces": axis_paths,
                "duration": " ".join(duration_tokens),
                "step": " ".join(step_tokens),
                "time_before": time_before,
                "time_after": time_after,
                "decode_bytes": decode_bytes,
                "lane_order": lane_order,
                "include_idle": include_idle,
                "only_handshakes": only_handshakes,
                "records": axis_sampler.records,
            }
            result["axis"] = axis_result
        if transaction_result is not None and axis_result is not None:
            result["correlation"] = correlate_trace(
                transaction_result,
                axis_result,
                correlate_by=correlate_by,
                window_tokens=correlate_window_tokens or None,
            )
        return result

    def _run_duration_sampled(
        self,
        duration_tokens: list[str],
        sample_step_tokens: list[str],
        *,
        kind: str,
    ) -> dict[str, Any]:
        duration_text, duration_value = parse_duration_tokens(duration_tokens)
        if duration_value <= 0:
            raise XdbError(f"{kind} duration must be > 0")
        time_before = str(self.driver.time().get("time") or "")
        current_time_value = parse_sim_time(time_before)
        end_time_value = current_time_value + duration_value
        iterations = 0
        last_result: dict[str, Any] | None = None
        while current_time_value < end_time_value:
            previous_time_value = current_time_value
            run_result = self.driver.run(sample_step_tokens)
            last_result = run_result
            current_time_text = str(run_result.get("time_after") or "")
            current_time_value = parse_sim_time(current_time_text)
            iterations += 1
            if current_time_value <= previous_time_value:
                raise XdbError(f"{kind} did not advance simulation")
        time_after = str(self.driver.time().get("time") or "")
        return {
            "time_before": time_before,
            "time_after": time_after,
            "duration": duration_text,
            "sample_step": " ".join(sample_step_tokens),
            "sample_iterations": iterations,
            "last_step": last_result or {},
        }

    def _eval_tcl_condition(self, expr: str) -> bool:
        script = f"set __xdb_expr [xdb_normalize_expr {_tcl_string(expr)}]; expr $__xdb_expr"
        result = self.driver.eval_tcl(script)
        value = str(result.get("result") or "").strip().lower()
        return value not in {"", "0", "false", "no"}

    def _with_trace_wait_until(
        self,
        expr: str,
        *,
        step_tokens: list[str],
        timeout_seconds: float | None,
        max_iterations: int | None,
    ) -> dict[str, Any]:
        if not expr:
            raise XdbError("missing Tcl expression")
        if not step_tokens:
            raise XdbError("missing step duration")
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise XdbError("timeout_seconds must be > 0")
        if max_iterations is not None and max_iterations <= 0:
            raise XdbError("max_iterations must be > 0")
        time_before = str(self.driver.time().get("time") or "")
        deadline = None if timeout_seconds is None else time.monotonic() + timeout_seconds
        iterations = 0
        while not self._eval_tcl_condition(expr):
            if max_iterations is not None and iterations >= max_iterations:
                raise XdbError(f"condition not met before reaching max iterations ({max_iterations})")
            if deadline is not None and time.monotonic() >= deadline:
                raise XdbError(f"timed out after {timeout_seconds} second(s) while waiting for condition")
            before = str(self.driver.time().get("time") or "")
            self.driver.run(step_tokens)
            iterations += 1
            after = str(self.driver.time().get("time") or "")
            if after == before and not self._eval_tcl_condition(expr):
                raise XdbError("condition not met and simulation did not advance while waiting")
        time_after = str(self.driver.time().get("time") or "")
        return {
            "expr": expr,
            "step": " ".join(step_tokens),
            "iterations": iterations,
            "time_before": time_before,
            "time_after": time_after,
            "timeout_seconds": timeout_seconds,
            "max_iterations": max_iterations,
        }

    def _with_trace_wait_until_signal(
        self,
        signal_name: str,
        expected_value: str,
        *,
        step_tokens: list[str],
        timeout_seconds: float | None,
        max_iterations: int | None,
    ) -> dict[str, Any]:
        if not signal_name:
            raise XdbError("missing signal")
        if expected_value == "":
            raise XdbError("missing expected signal value")
        if not step_tokens:
            raise XdbError("missing step duration")
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise XdbError("timeout_seconds must be > 0")
        if max_iterations is not None and max_iterations <= 0:
            raise XdbError("max_iterations must be > 0")
        time_before = str(self.driver.time().get("time") or "")
        deadline = None if timeout_seconds is None else time.monotonic() + timeout_seconds
        iterations = 0
        value = str(self.driver.get_signal(signal_name).get("value") or "")
        while value != expected_value:
            if max_iterations is not None and iterations >= max_iterations:
                raise XdbError(
                    f"signal did not reach expected value before reaching max iterations ({max_iterations})"
                )
            if deadline is not None and time.monotonic() >= deadline:
                raise XdbError(f"timed out after {timeout_seconds} second(s) while waiting for signal")
            before = str(self.driver.time().get("time") or "")
            self.driver.run(step_tokens)
            iterations += 1
            after = str(self.driver.time().get("time") or "")
            value = str(self.driver.get_signal(signal_name).get("value") or "")
            if after == before and value != expected_value:
                raise XdbError("signal did not reach expected value and simulation did not advance while waiting")
        time_after = str(self.driver.time().get("time") or "")
        return {
            "signal": signal_name,
            "value": value,
            "expected": expected_value,
            "step": " ".join(step_tokens),
            "iterations": iterations,
            "time_before": time_before,
            "time_after": time_after,
            "timeout_seconds": timeout_seconds,
            "max_iterations": max_iterations,
        }

    def _with_trace_action(
        self,
        action_op: str,
        action_args: dict[str, Any],
        sample_step_tokens: list[str],
    ) -> dict[str, Any]:
        if action_op == OP_RUN:
            run_tokens = [str(v) for v in list(action_args.get("tokens") or []) if str(v)]
            if not run_tokens:
                raise XdbError("with-trace wrapped 'xdb sim run' requires an explicit duration")
            return self._run_duration_sampled(run_tokens, sample_step_tokens, kind="run")
        if action_op == OP_STEP:
            time_tokens = [str(v) for v in list(action_args.get("time_tokens") or []) if str(v)]
            if time_tokens:
                result = self._run_duration_sampled(time_tokens, sample_step_tokens, kind="step")
                result["step_mode"] = "time"
                return result
            return self.dispatch(action_op, action_args)
        if action_op == OP_UNTIL:
            return self._with_trace_wait_until(
                str(action_args.get("expr") or ""),
                step_tokens=[str(v) for v in list(action_args.get("step_tokens") or []) if str(v)],
                timeout_seconds=_arg_optional_float(action_args, "timeout_seconds"),
                max_iterations=_arg_optional_int(action_args, "max_iterations"),
            )
        if action_op == OP_UNTIL_SIGNAL:
            return self._with_trace_wait_until_signal(
                str(action_args.get("signal") or ""),
                str(action_args.get("value") or ""),
                step_tokens=[str(v) for v in list(action_args.get("step_tokens") or []) if str(v)],
                timeout_seconds=_arg_optional_float(action_args, "timeout_seconds"),
                max_iterations=_arg_optional_int(action_args, "max_iterations"),
            )
        return self.dispatch(action_op, action_args)
