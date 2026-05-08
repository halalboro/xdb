from __future__ import annotations

import errno
import os
import queue
import re
import select
import struct
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..errors import XdbError

_INPUT_SET_CSR = 0
_INPUT_GET_CSR = 1
_INPUT_USER_MAP = 2
_INPUT_MEM_WRITE = 3
_INPUT_INVOKE = 4
_INPUT_SLEEP = 5
_INPUT_CHECK_COMPLETED = 6
_INPUT_CLEAR_COMPLETED = 7
_INPUT_USER_UNMAP = 8

_OUTPUT_GET_CSR = 0
_OUTPUT_HOST_WRITE = 1
_OUTPUT_IRQ = 2
_OUTPUT_CHECK_COMPLETED = 3
_OUTPUT_HOST_READ = 4

STREAM_NAME_TO_ID = {
    "card": 0,
    "host": 1,
    "rdma": 2,
    "tcp": 3,
}

STREAM_ID_TO_NAME = {value: key for key, value in STREAM_NAME_TO_ID.items()}

OPCODE_NAME_TO_ID = {
    "noop": 0,
    "local-read": 1,
    "local-write": 2,
    "local-transfer": 3,
    "local-offload": 4,
    "local-sync": 5,
    "remote-rdma-read": 6,
    "remote-rdma-write": 7,
    "remote-rdma-send": 8,
    "remote-tcp-send": 9,
}

OPCODE_ID_TO_NAME = {value: key for key, value in OPCODE_NAME_TO_ID.items()}

_LOCAL_PROTOCOL_OPCODES = {
    "local-read",
    "local-write",
    "local-transfer",
    "local-offload",
    "local-sync",
}

MAX_TRANSFER_SIZE = 128 * 1024 * 1024


@dataclass(frozen=True)
class MemoryRange:
    base: int
    size: int

    @property
    def end(self) -> int:
        return self.base + self.size


class CoyoteSimController:
    def __init__(self, build_dir: str):
        self.build_dir = str(Path(build_dir).resolve())
        self.sim_dir = Path(self.build_dir) / "sim"
        self.input_path = self.sim_dir / "input.bin"
        self.output_path = self.sim_dir / "output.bin"
        self._input_fd: int | None = None
        self._output_fd: int | None = None
        self._stop = threading.Event()
        self._reader_thread: threading.Thread | None = None
        self._write_lock = threading.Lock()
        self._segments: dict[int, bytearray] = {}
        self._csr_results: queue.Queue[int] = queue.Queue()
        self._completed_results: queue.Queue[int] = queue.Queue()
        self._irq_events: queue.Queue[dict[str, int]] = queue.Queue()
        self._host_write_count = 0
        self._host_read_count = 0
        self._last_protocol_error = ""

    def start(self) -> None:
        self.sim_dir.mkdir(parents=True, exist_ok=True)
        self._cleanup_fifo_path(self.input_path)
        self._cleanup_fifo_path(self.output_path)
        os.mkfifo(self.input_path)
        os.mkfifo(self.output_path)
        self._input_fd = os.open(self.input_path, os.O_RDWR | os.O_NONBLOCK)
        self._output_fd = os.open(self.output_path, os.O_RDWR | os.O_NONBLOCK)
        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=1.0)
            self._reader_thread = None
        for fd_attr in ("_input_fd", "_output_fd"):
            fd = getattr(self, fd_attr)
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
                setattr(self, fd_attr, None)
        self._cleanup_fifo_path(self.input_path)
        self._cleanup_fifo_path(self.output_path)

    @staticmethod
    def _cleanup_fifo_path(path: Path) -> None:
        try:
            if path.exists() or path.is_symlink():
                path.unlink()
        except FileNotFoundError:
            pass

    def status(self) -> dict[str, object]:
        return {
            "supported": True,
            "build_dir": self.build_dir,
            "sim_dir": str(self.sim_dir),
            "input_path": str(self.input_path),
            "output_path": str(self.output_path),
            "mapped_segments": [
                {
                    "addr": base,
                    "addr_hex": _hex(base),
                    "size": len(data),
                    "end": base + len(data),
                    "end_hex": _hex(base + len(data)),
                }
                for base, data in sorted(self._segments.items())
            ],
            "host_write_count": self._host_write_count,
            "host_read_count": self._host_read_count,
            "pending_irqs": self._irq_events.qsize(),
            "last_protocol_error": self._last_protocol_error,
            "supported_opcodes": sorted(_LOCAL_PROTOCOL_OPCODES),
            "notes": [
                "current xdb Coyote support is limited to the local host-memory protocol",
                "remote RDMA and TCP simulation commands are not supported by the underlying Coyote simulation target",
            ],
        }

    def map_host_memory(self, addr: int, size: int) -> dict[str, object]:
        _require_positive_size(size)
        self._ensure_non_overlapping(addr, size)
        self._segments[addr] = bytearray(size)
        self.write_input(self._encode_user_map(addr, size))
        return {
            "space": "host",
            "addr": addr,
            "addr_hex": _hex(addr),
            "size": size,
            "mapped": True,
        }

    def unmap_host_memory(self, addr: int) -> dict[str, object]:
        if addr not in self._segments:
            raise XdbError(f"host memory segment is not mapped at {_hex(addr)}")
        size = len(self._segments[addr])
        del self._segments[addr]
        self.write_input(self._encode_user_unmap(addr))
        return {
            "space": "host",
            "addr": addr,
            "addr_hex": _hex(addr),
            "size": size,
            "unmapped": True,
        }

    def ensure_host_memory(self, addr: int, size: int) -> bool:
        _require_positive_size(size)
        try:
            self._find_segment(addr, size)
            return False
        except XdbError:
            self.map_host_memory(addr, size)
            return True

    def write_host_memory(self, addr: int, data: bytes) -> dict[str, object]:
        if not data:
            raise XdbError("host memory write payload must not be empty")
        auto_mapped = self.ensure_host_memory(addr, len(data))
        base, segment, offset = self._find_segment(addr, len(data))
        segment[offset : offset + len(data)] = data
        self.write_input(self._encode_mem_write(addr, data))
        return {
            "space": "host",
            "addr": addr,
            "addr_hex": _hex(addr),
            "size": len(data),
            "mapped": auto_mapped,
        }

    def read_host_memory(self, addr: int, size: int) -> dict[str, object]:
        base, segment, offset = self._find_segment(addr, size)
        data = bytes(segment[offset : offset + size])
        return {
            "space": "host",
            "addr": addr,
            "addr_hex": _hex(addr),
            "size": size,
            "segment_base": base,
            "segment_base_hex": _hex(base),
            "data_hex": data.hex(),
        }

    def write_input(
        self,
        payload: bytes,
        *,
        pump: Callable[[], None] | None = None,
    ) -> None:
        fd = self._require_input_fd()
        with self._write_lock:
            view = memoryview(payload)
            total = 0
            while total < len(view):
                try:
                    written = os.write(fd, view[total:])
                except BlockingIOError:
                    written = 0
                except OSError as e:
                    if e.errno == errno.EAGAIN:
                        written = 0
                    else:
                        raise XdbError(f"failed to write to Coyote input pipe: {e}") from e
                if written > 0:
                    total += written
                    continue
                if pump is None:
                    select.select([], [fd], [], 0.05)
                    continue
                pump()

    def encode_csr_write(self, addr: int, value: int) -> bytes:
        return self._encode_set_csr(addr, value)

    def encode_csr_read(self, addr: int) -> bytes:
        return self._encode_get_csr(addr)

    def encode_check_completed(self, opcode: int) -> bytes:
        return self._encode_check_completed(opcode)

    def encode_clear_completed(self) -> bytes:
        return self._encode_clear_completed()

    def encode_invoke_single(
        self,
        *,
        opcode: int,
        addr: int,
        length: int,
        stream: int,
        dest: int,
        last: bool,
    ) -> bytes:
        return self._encode_invoke(opcode, stream, dest, addr, length, last)

    def encode_invoke_transfer(
        self,
        *,
        src_addr: int,
        src_len: int,
        src_stream: int,
        src_dest: int,
        dst_addr: int,
        dst_len: int,
        dst_stream: int,
        dst_dest: int,
        last: bool,
    ) -> bytes:
        return b"".join(
            [
                self._encode_mem_write(src_addr, self._read_host_bytes(src_addr, src_len)),
                self._encode_invoke(OPCODE_NAME_TO_ID["local-read"], src_stream, src_dest, src_addr, src_len, last),
                self._encode_invoke(OPCODE_NAME_TO_ID["local-write"], dst_stream, dst_dest, dst_addr, dst_len, last),
            ]
        )

    def encode_sync_source_write(self, addr: int, size: int) -> bytes:
        return self._encode_mem_write(addr, self._read_host_bytes(addr, size))

    def get_csr_result_nowait(self) -> int | None:
        return _queue_get_nowait(self._csr_results)

    def get_completed_result_nowait(self) -> int | None:
        return _queue_get_nowait(self._completed_results)

    def get_irq_nowait(self) -> dict[str, int] | None:
        return _queue_get_nowait(self._irq_events)

    def _require_input_fd(self) -> int:
        if self._input_fd is None:
            raise XdbError("Coyote input pipe is not open")
        return self._input_fd

    def _require_output_fd(self) -> int:
        if self._output_fd is None:
            raise XdbError("Coyote output pipe is not open")
        return self._output_fd

    def _ensure_non_overlapping(self, addr: int, size: int) -> None:
        new_range = MemoryRange(addr, size)
        for base, data in self._segments.items():
            existing = MemoryRange(base, len(data))
            if new_range.base < existing.end and existing.base < new_range.end:
                raise XdbError(
                    "host memory mapping overlaps an existing segment: "
                    f"new=[{_hex(new_range.base)}, {_hex(new_range.end)}) "
                    f"existing=[{_hex(existing.base)}, {_hex(existing.end)})"
                )

    def _find_segment(self, addr: int, size: int) -> tuple[int, bytearray, int]:
        _require_positive_size(size)
        for base, data in self._segments.items():
            end = base + len(data)
            if base <= addr and addr + size <= end:
                return base, data, addr - base
        raise XdbError(
            f"host memory range is not mapped: addr={_hex(addr)} size={size}"
        )

    def _read_host_bytes(self, addr: int, size: int) -> bytes:
        _base, segment, offset = self._find_segment(addr, size)
        return bytes(segment[offset : offset + size])

    def _reader_loop(self) -> None:
        fd = self._require_output_fd()
        buffer = bytearray()
        while not self._stop.is_set():
            try:
                ready, _, _ = select.select([fd], [], [], 0.1)
            except (OSError, ValueError):
                break
            if not ready:
                continue
            try:
                chunk = os.read(fd, 65536)
            except BlockingIOError:
                continue
            except OSError as e:
                self._set_protocol_error(f"failed to read Coyote output pipe: {e}")
                continue
            if not chunk:
                continue
            buffer.extend(chunk)
            self._parse_output_buffer(buffer)

    def _parse_output_buffer(self, buffer: bytearray) -> None:
        while buffer:
            opcode = buffer[0]
            if opcode == _OUTPUT_GET_CSR:
                if len(buffer) < 1 + 8:
                    return
                del buffer[0]
                value = struct.unpack_from("<Q", buffer, 0)[0]
                del buffer[:8]
                self._csr_results.put(value)
                continue
            if opcode == _OUTPUT_HOST_WRITE:
                if len(buffer) < 1 + 16:
                    return
                size = struct.unpack_from("<Q", buffer, 1 + 8)[0]
                total = 1 + 16 + size
                if len(buffer) < total:
                    return
                del buffer[0]
                vaddr, size = struct.unpack_from("<QQ", buffer, 0)
                del buffer[:16]
                data = bytes(buffer[:size])
                del buffer[:size]
                self._handle_host_write(vaddr, data)
                continue
            if opcode == _OUTPUT_IRQ:
                if len(buffer) < 1 + 5:
                    return
                del buffer[0]
                pid = buffer[0]
                value = struct.unpack_from("<I", buffer, 1)[0]
                del buffer[:5]
                self._irq_events.put({"pid": pid, "value": value})
                continue
            if opcode == _OUTPUT_CHECK_COMPLETED:
                if len(buffer) < 1 + 4:
                    return
                del buffer[0]
                value = struct.unpack_from("<I", buffer, 0)[0]
                del buffer[:4]
                self._completed_results.put(value)
                continue
            if opcode == _OUTPUT_HOST_READ:
                if len(buffer) < 1 + 16:
                    return
                del buffer[0]
                vaddr, size = struct.unpack_from("<QQ", buffer, 0)
                del buffer[:16]
                self._handle_host_read(vaddr, size)
                continue
            self._set_protocol_error(f"unknown Coyote output opcode: {opcode}")
            buffer.clear()
            return

    def _handle_host_write(self, vaddr: int, data: bytes) -> None:
        try:
            _base, segment, offset = self._find_segment(vaddr, len(data))
            segment[offset : offset + len(data)] = data
            self._host_write_count += 1
        except XdbError as e:
            self._set_protocol_error(str(e))

    def _handle_host_read(self, vaddr: int, size: int) -> None:
        try:
            payload = self._encode_mem_write(vaddr, self._read_host_bytes(vaddr, size))
        except XdbError as e:
            self._set_protocol_error(str(e))
            payload = self._encode_mem_write(vaddr, bytes(size))
        self._host_read_count += 1
        try:
            self.write_input(payload)
        except XdbError as e:
            self._set_protocol_error(str(e))

    def _set_protocol_error(self, message: str) -> None:
        self._last_protocol_error = message

    @staticmethod
    def _encode_set_csr(addr: int, value: int) -> bytes:
        return bytes([_INPUT_SET_CSR]) + struct.pack("<QQ", addr, value)

    @staticmethod
    def _encode_get_csr(addr: int) -> bytes:
        return bytes([_INPUT_GET_CSR]) + struct.pack("<QQB", addr, 0, 0)

    @staticmethod
    def _encode_user_map(vaddr: int, size: int) -> bytes:
        return bytes([_INPUT_USER_MAP]) + struct.pack("<QQ", vaddr, size)

    @staticmethod
    def _encode_mem_write(vaddr: int, data: bytes) -> bytes:
        return bytes([_INPUT_MEM_WRITE]) + struct.pack("<QQ", vaddr, len(data)) + data

    @staticmethod
    def _encode_invoke(
        opcode: int,
        stream: int,
        dest: int,
        vaddr: int,
        length: int,
        last: bool,
    ) -> bytes:
        return bytes([_INPUT_INVOKE]) + struct.pack(
            "<BBBQQB",
            opcode,
            stream,
            dest,
            vaddr,
            length,
            1 if last else 0,
        )

    @staticmethod
    def _encode_check_completed(opcode: int) -> bytes:
        return bytes([_INPUT_CHECK_COMPLETED]) + struct.pack("<BQB", opcode, 0, 0)

    @staticmethod
    def _encode_clear_completed() -> bytes:
        return bytes([_INPUT_CLEAR_COMPLETED])

    @staticmethod
    def _encode_user_unmap(vaddr: int) -> bytes:
        return bytes([_INPUT_USER_UNMAP]) + struct.pack("<Q", vaddr)


def ensure_supported_local_opcode(name: str) -> int:
    opcode = OPCODE_NAME_TO_ID.get(name)
    if opcode is None:
        raise XdbError(f"unsupported Coyote opcode: {name}")
    if name not in _LOCAL_PROTOCOL_OPCODES:
        raise XdbError(
            f"opcode {name!r} is not supported by the current Coyote simulation target"
        )
    return opcode


def parse_stream_name(name: str) -> int:
    stream = STREAM_NAME_TO_ID.get(name)
    if stream is None:
        raise XdbError(f"unsupported Coyote stream: {name}")
    return stream


def _queue_get_nowait(q: queue.Queue):
    try:
        return q.get_nowait()
    except queue.Empty:
        return None


def _hex(value: int) -> str:
    return f"0x{value:x}"


def _require_positive_size(size: int) -> None:
    if size <= 0:
        raise XdbError("size must be > 0")


def parse_hex_bytes(value: str) -> bytes:
    normalized = value.strip()
    if normalized.startswith(("0x", "0X")):
        normalized = normalized[2:]
    normalized = re.sub(r"[^0-9a-fA-F]", "", normalized)
    if not normalized:
        raise XdbError("hex payload is empty")
    if len(normalized) % 2 != 0:
        normalized = "0" + normalized
    try:
        return bytes.fromhex(normalized)
    except ValueError as e:
        raise XdbError(f"invalid hex payload: {value}") from e
