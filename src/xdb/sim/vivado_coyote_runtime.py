from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, Callable, Protocol, TypeVar

from ..errors import XdbError
from .coyote import (
    CoyoteSimController,
    MAX_TRANSFER_SIZE,
    ensure_supported_local_opcode,
    parse_stream_name,
)


_T = TypeVar("_T")


class _VivadoCoyoteHost(Protocol):
    runtime_root: str
    _coyote: CoyoteSimController | None

    def run(self, tokens: list[str]) -> dict[str, Any]: ...

    def _require_coyote(self) -> CoyoteSimController: ...

    def _coyote_pump_step(self) -> None: ...

    def _coyote_wait_for_item(
        self,
        getter: Callable[[], _T | None],
        *,
        timeout_seconds: float | None,
        description: str,
    ) -> _T: ...


class VivadoCoyoteMixin:
    def _prepare_coyote_runtime(self: _VivadoCoyoteHost) -> None:
        if not self.runtime_root:
            return
        runtime_root = Path(self.runtime_root)
        lynx_pkg = runtime_root / "lynx_pkg.sv"
        if not lynx_pkg.is_file():
            return
        try:
            text = lynx_pkg.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            raise XdbError(f"failed to read Coyote runtime file: {lynx_pkg}") from e
        replaced = text.replace(
            'localparam string BUILD_DIR = "/build/source/.nix-hw-v80";',
            f'localparam string BUILD_DIR = "{runtime_root}";',
        )
        replaced = replaced.replace(
            'localparam string BUILD_DIR = "/build/source/.nix-hw-u280";',
            f'localparam string BUILD_DIR = "{runtime_root}";',
        )
        if replaced == text:
            replaced = re.sub(
                r'localparam\s+string\s+BUILD_DIR\s*=\s*"[^"]*"\s*;',
                f'localparam string BUILD_DIR = "{runtime_root}";',
                text,
                count=1,
            )
        if replaced != text:
            try:
                lynx_pkg.write_text(replaced, encoding="utf-8")
            except OSError as e:
                raise XdbError(f"failed to rewrite Coyote runtime file: {lynx_pkg}") from e
        if self._coyote is not None:
            self._coyote.close()
        self._coyote = CoyoteSimController(str(runtime_root))
        self._coyote.start()

    def _require_coyote(self: _VivadoCoyoteHost) -> CoyoteSimController:
        if self._coyote is None:
            raise XdbError(
                "Coyote simulation protocol is not available for this simulation runtime"
            )
        return self._coyote

    def _coyote_pump_step(self: _VivadoCoyoteHost) -> None:
        self.run(["10", "ns"])

    def _coyote_wait_for_item(
        self: _VivadoCoyoteHost,
        getter: Callable[[], _T | None],
        *,
        timeout_seconds: float | None,
        description: str,
    ) -> _T:
        deadline = None if timeout_seconds is None else time.monotonic() + timeout_seconds
        while True:
            item = getter()
            if item is not None:
                return item
            if deadline is not None and time.monotonic() >= deadline:
                raise XdbError(f"timed out waiting for {description}")
            self._coyote_pump_step()

    def coyote_status(self: _VivadoCoyoteHost) -> dict[str, Any]:
        return self._require_coyote().status()

    def coyote_csr_read(
        self: _VivadoCoyoteHost, addr: int, *, timeout_seconds: float | None = None
    ) -> dict[str, Any]:
        controller = self._require_coyote()
        controller.write_input(
            controller.encode_csr_read(addr),
            pump=self._coyote_pump_step,
        )
        value = self._coyote_wait_for_item(
            controller.get_csr_result_nowait,
            timeout_seconds=timeout_seconds,
            description=f"CSR read response at 0x{addr:x}",
        )
        return {
            "addr": addr,
            "addr_hex": f"0x{addr:x}",
            "value": value,
            "value_hex": f"0x{value:x}",
        }

    def coyote_csr_write(
        self: _VivadoCoyoteHost, addr: int, value: int
    ) -> dict[str, Any]:
        controller = self._require_coyote()
        controller.write_input(
            controller.encode_csr_write(addr, value),
            pump=self._coyote_pump_step,
        )
        self._coyote_pump_step()
        return {
            "addr": addr,
            "addr_hex": f"0x{addr:x}",
            "value": value,
            "value_hex": f"0x{value:x}",
            "written": True,
        }

    def coyote_mem_map(
        self: _VivadoCoyoteHost, space: str, addr: int, size: int
    ) -> dict[str, Any]:
        if space != "host":
            raise XdbError("only host memory is currently supported")
        controller = self._require_coyote()
        result = controller.map_host_memory(addr, size)
        self._coyote_pump_step()
        return result

    def coyote_mem_unmap(
        self: _VivadoCoyoteHost, space: str, addr: int
    ) -> dict[str, Any]:
        if space != "host":
            raise XdbError("only host memory is currently supported")
        controller = self._require_coyote()
        result = controller.unmap_host_memory(addr)
        self._coyote_pump_step()
        return result

    def coyote_mem_list(self: _VivadoCoyoteHost, space: str) -> dict[str, Any]:
        if space != "host":
            raise XdbError("only host memory is currently supported")
        return {
            "space": "host",
            **self._require_coyote().host_memory_status(),
        }

    def coyote_mem_reset(self: _VivadoCoyoteHost, space: str) -> dict[str, Any]:
        if space != "host":
            raise XdbError("only host memory is currently supported")
        controller = self._require_coyote()
        result = controller.reset_host_memory()
        self._coyote_pump_step()
        return result

    def coyote_mem_write(
        self: _VivadoCoyoteHost, space: str, addr: int, data: bytes
    ) -> dict[str, Any]:
        if space != "host":
            raise XdbError("only host memory is currently supported")
        controller = self._require_coyote()
        result = controller.write_host_memory(addr, data)
        self._coyote_pump_step()
        return result

    def coyote_mem_read(
        self: _VivadoCoyoteHost, space: str, addr: int, size: int
    ) -> dict[str, Any]:
        if space != "host":
            raise XdbError("only host memory is currently supported")
        return self._require_coyote().read_host_memory(addr, size)

    def coyote_invoke(
        self: _VivadoCoyoteHost,
        opcode_name: str,
        *,
        addr: int | None = None,
        length: int | None = None,
        stream_name: str = "host",
        dest: int = 0,
        last: bool = True,
        src_addr: int | None = None,
        src_length: int | None = None,
        src_stream_name: str = "host",
        src_dest: int = 0,
        dst_addr: int | None = None,
        dst_length: int | None = None,
        dst_stream_name: str = "host",
        dst_dest: int = 0,
    ) -> dict[str, Any]:
        controller = self._require_coyote()
        opcode = ensure_supported_local_opcode(opcode_name)
        if opcode_name == "local-transfer":
            if src_stream_name != "host" or dst_stream_name != "host":
                raise XdbError(
                    "current xdb Coyote support only implements host-stream local transfers"
                )
            if src_addr is None or dst_addr is None:
                raise XdbError("local-transfer requires --src-addr and --dst-addr")
            effective_src_len = src_length if src_length is not None else length
            effective_dst_len = dst_length if dst_length is not None else length
            if effective_src_len is None or effective_dst_len is None:
                raise XdbError("local-transfer requires --len or both --src-len and --dst-len")
            if effective_src_len <= 0 or effective_dst_len <= 0:
                raise XdbError("transfer lengths must be > 0")
            if effective_src_len > MAX_TRANSFER_SIZE or effective_dst_len > MAX_TRANSFER_SIZE:
                raise XdbError("Coyote transfers over 128MB are not supported")
            controller.ensure_host_memory(dst_addr, effective_dst_len)
            payload = controller.encode_invoke_transfer(
                src_addr=src_addr,
                src_len=effective_src_len,
                src_stream=parse_stream_name(src_stream_name),
                src_dest=src_dest,
                dst_addr=dst_addr,
                dst_len=effective_dst_len,
                dst_stream=parse_stream_name(dst_stream_name),
                dst_dest=dst_dest,
                last=last,
            )
            controller.write_input(payload, pump=self._coyote_pump_step)
            self._coyote_pump_step()
            return {
                "opcode": opcode_name,
                "src_addr": src_addr,
                "src_addr_hex": f"0x{src_addr:x}",
                "src_len": effective_src_len,
                "dst_addr": dst_addr,
                "dst_addr_hex": f"0x{dst_addr:x}",
                "dst_len": effective_dst_len,
                "issued": True,
            }

        if addr is None or length is None:
            raise XdbError(f"{opcode_name} requires --addr and --len")
        if length <= 0:
            raise XdbError("length must be > 0")
        if length > MAX_TRANSFER_SIZE:
            raise XdbError("Coyote transfers over 128MB are not supported")
        if stream_name != "host":
            raise XdbError(
                "current xdb Coyote support only implements host-stream local operations"
            )
        stream = parse_stream_name(stream_name)
        if opcode_name in {"local-read", "local-offload"}:
            source_write = controller.encode_sync_source_write(addr, length)
            payload = source_write + controller.encode_invoke_single(
                opcode=opcode,
                addr=addr,
                length=length,
                stream=stream,
                dest=dest,
                last=last,
            )
        else:
            if opcode_name in {"local-write", "local-sync"}:
                controller.ensure_host_memory(addr, length)
            payload = controller.encode_invoke_single(
                opcode=opcode,
                addr=addr,
                length=length,
                stream=stream,
                dest=dest,
                last=last,
            )
        controller.write_input(payload, pump=self._coyote_pump_step)
        self._coyote_pump_step()
        return {
            "opcode": opcode_name,
            "addr": addr,
            "addr_hex": f"0x{addr:x}",
            "length": length,
            "stream": stream_name,
            "dest": dest,
            "last": bool(last),
            "issued": True,
        }

    def coyote_completed(
        self: _VivadoCoyoteHost,
        opcode_name: str,
        *,
        target_count: int | None = None,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        controller = self._require_coyote()
        opcode = ensure_supported_local_opcode(opcode_name)

        def check_once(wait_timeout: float | None) -> int:
            controller.write_input(
                controller.encode_check_completed(opcode),
                pump=self._coyote_pump_step,
            )
            return self._coyote_wait_for_item(
                controller.get_completed_result_nowait,
                timeout_seconds=wait_timeout,
                description=f"completion count for {opcode_name}",
            )

        if target_count is None:
            count = check_once(timeout_seconds)
            return {"opcode": opcode_name, "count": count}
        if target_count <= 0:
            raise XdbError("target completion count must be > 0")
        deadline = None if timeout_seconds is None else time.monotonic() + timeout_seconds
        count = 0
        while count < target_count:
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            count = check_once(remaining)
            if count >= target_count:
                break
            if deadline is not None and time.monotonic() >= deadline:
                raise XdbError(
                    f"timed out waiting for completion count {target_count} for {opcode_name}"
                )
            self._coyote_pump_step()
        return {
            "opcode": opcode_name,
            "count": count,
            "target_count": target_count,
            "satisfied": count >= target_count,
        }

    def coyote_clear_completed(self: _VivadoCoyoteHost) -> dict[str, Any]:
        controller = self._require_coyote()
        controller.write_input(
            controller.encode_clear_completed(),
            pump=self._coyote_pump_step,
        )
        self._coyote_pump_step()
        return {"cleared": True}

    def coyote_irq_wait(
        self: _VivadoCoyoteHost, *, timeout_seconds: float | None = None
    ) -> dict[str, Any]:
        event = self._coyote_wait_for_item(
            self._require_coyote().get_irq_nowait,
            timeout_seconds=timeout_seconds,
            description="Coyote IRQ",
        )
        return {
            "pid": int(event["pid"]),
            "value": int(event["value"]),
            "value_hex": f"0x{int(event['value']):x}",
        }
