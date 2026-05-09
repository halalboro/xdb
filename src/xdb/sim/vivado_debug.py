from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from xdb.errors import XdbError
from xdb.sim.tcl_api import build_proc_request


class _VivadoDebugHost(Protocol):
    top: str
    _vcd_state: dict[str, Any] | None

    def launch(self, timeout: int = 300, top: str | None = None) -> dict[str, Any]: ...

    def request(self, body_tcl: str, timeout: int = 120) -> dict[str, Any]: ...

    def time(self) -> dict[str, Any]: ...


class VivadoDebugMixin:
    def set_top(self: _VivadoDebugHost, top: str, timeout: int = 300) -> dict[str, Any]:
        if top != self.top:
            raise XdbError("changing top module is not supported for runtime-backed simulation sessions")
        data = self.launch(timeout=timeout, top=top)
        data["relaunched"] = False
        return data

    def add_wave(self: _VivadoDebugHost, pattern: str) -> dict[str, Any]:
        return self.request(build_proc_request("xdb_api_add_wave", pattern))

    def vcd_start(self: _VivadoDebugHost, file_path: str, scope: str | None = None) -> dict[str, Any]:
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

    def vcd_stop(self: _VivadoDebugHost) -> dict[str, Any]:
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

    def vcd_status(self: _VivadoDebugHost) -> dict[str, Any]:
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

    def assert_signal(self: _VivadoDebugHost, signal: str, value: str) -> dict[str, Any]:
        return self.request(build_proc_request("xdb_api_assert_signal", signal, value))

    def assert_tcl(self: _VivadoDebugHost, expr: str) -> dict[str, Any]:
        return self.request(build_proc_request("xdb_api_assert_tcl", expr))

    def expect_signal(
        self: _VivadoDebugHost, signal: str, value: str, *, within_tokens: list[str]
    ) -> dict[str, Any]:
        return self.request(build_proc_request("xdb_api_expect_signal", signal, value, within_tokens))

    def expect_change(
        self: _VivadoDebugHost, signal: str, *, within_tokens: list[str]
    ) -> dict[str, Any]:
        return self.request(build_proc_request("xdb_api_expect_change", signal, within_tokens))

    def add_breakpoint(self: _VivadoDebugHost, condition: str) -> dict[str, Any]:
        return self.request(build_proc_request("xdb_api_breakpoint_add", condition))

    def clear_breakpoints(self: _VivadoDebugHost) -> dict[str, Any]:
        return self.request(build_proc_request("xdb_api_breakpoint_clear"))

    def eval_tcl(self: _VivadoDebugHost, script: str) -> dict[str, Any]:
        return self.request(build_proc_request("xdb_api_eval_tcl", script))

    def source_tcl(self: _VivadoDebugHost, path: str) -> dict[str, Any]:
        return self.request(build_proc_request("xdb_api_source_tcl", path))

    def force(
        self: _VivadoDebugHost,
        signal: str,
        values: list[str],
        *,
        radix: str | None = None,
        repeat_every: str | None = None,
        cancel_after: str | None = None,
    ) -> dict[str, Any]:
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

    def release(
        self: _VivadoDebugHost, signal: str | None = None, *, all_forces: bool = False
    ) -> dict[str, Any]:
        if all_forces:
            return self.request(build_proc_request("xdb_api_release_all"))
        return self.request(build_proc_request("xdb_api_release_signal", signal or ""))
