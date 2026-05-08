from __future__ import annotations

from pathlib import Path

from ..errors import XdbError
from .tcl_api import build_proc_request


class VivadoDebugMixin:
    def set_top(self, top: str, timeout: int = 300) -> dict:
        if top != self.top:
            raise XdbError("changing top module is not supported for runtime-backed simulation sessions")
        data = self.launch(timeout=timeout, top=top)
        data["relaunched"] = False
        return data

    def add_wave(self, pattern: str) -> dict:
        return self.request(build_proc_request("xdb_api_add_wave", pattern))

    def vcd_start(self, file_path: str, scope: str | None = None) -> dict:
        if self._vcd_state is not None:
            raise XdbError(
                f"a VCD dump is already active: {self._vcd_state.get('file', '<unknown>')}"
            )
        resolved = Path(file_path).expanduser().resolve()
        resolved.parent.mkdir(parents=True, exist_ok=True)
        result = self.request(build_proc_request("xdb_api_vcd_start", str(resolved), scope or ""))
        self._vcd_state = {
            "active": True,
            "file": str(resolved),
            "scope": scope,
            "started_at": str(result.get("time") or ""),
        }
        return dict(self._vcd_state)

    def vcd_stop(self) -> dict:
        if self._vcd_state is None:
            return {
                "active": False,
                "stopped": False,
                "file": None,
                "scope": None,
            }
        state = dict(self._vcd_state)
        result = self.request(build_proc_request("xdb_api_vcd_stop"))
        self._vcd_state = None
        return {
            "active": False,
            "stopped": True,
            "file": state.get("file"),
            "scope": state.get("scope"),
            "started_at": state.get("started_at"),
            "stopped_at": result.get("time"),
        }

    def vcd_status(self) -> dict:
        time_info = self.time()
        if self._vcd_state is None:
            return {
                "active": False,
                "file": None,
                "scope": None,
                "time": time_info.get("time"),
            }
        return {
            "active": True,
            "file": self._vcd_state.get("file"),
            "scope": self._vcd_state.get("scope"),
            "started_at": self._vcd_state.get("started_at"),
            "time": time_info.get("time"),
        }

    def assert_signal(self, signal: str, value: str) -> dict:
        return self.request(build_proc_request("xdb_api_assert_signal", signal, value))

    def assert_tcl(self, expr: str) -> dict:
        return self.request(build_proc_request("xdb_api_assert_tcl", expr))

    def expect_signal(self, signal: str, value: str, *, within_tokens: list[str]) -> dict:
        return self.request(build_proc_request("xdb_api_expect_signal", signal, value, within_tokens))

    def expect_change(self, signal: str, *, within_tokens: list[str]) -> dict:
        return self.request(build_proc_request("xdb_api_expect_change", signal, within_tokens))

    def add_breakpoint(self, condition: str) -> dict:
        return self.request(build_proc_request("xdb_api_breakpoint_add", condition))

    def clear_breakpoints(self) -> dict:
        return self.request(build_proc_request("xdb_api_breakpoint_clear"))

    def eval_tcl(self, script: str) -> dict:
        return self.request(build_proc_request("xdb_api_eval_tcl", script))

    def source_tcl(self, path: str) -> dict:
        return self.request(build_proc_request("xdb_api_source_tcl", path))

    def force(
        self,
        signal: str,
        values: list[str],
        *,
        radix: str | None = None,
        repeat_every: str | None = None,
        cancel_after: str | None = None,
    ) -> dict:
        return self.request(
            build_proc_request(
                "xdb_api_force",
                signal,
                values,
                radix or "",
                repeat_every or "",
                cancel_after or "",
            )
        )

    def release(self, signal: str | None = None, *, all_forces: bool = False) -> dict:
        if all_forces:
            return self.request(build_proc_request("xdb_api_release_all"))
        return self.request(build_proc_request("xdb_api_release_signal", signal or ""))
