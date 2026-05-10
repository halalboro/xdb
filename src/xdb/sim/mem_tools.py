from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from xdb.errors import XdbError
from xdb.sim.client import coyote_mem_read_session


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def dump_memory_session(
    session_name: str | None,
    space: str,
    addr: int,
    size: int,
    out: str,
) -> dict[str, Any]:
    if size <= 0:
        raise XdbError("memory dump size must be > 0")
    out_path = Path(out).expanduser()
    if not out_path.is_absolute():
        out_path = Path.cwd() / out_path
    out_path = out_path.resolve()
    result = coyote_mem_read_session(session_name, space, addr, size)
    data_hex = str(result.get("data_hex") or "")
    try:
        data = bytes.fromhex(data_hex)
    except ValueError as e:
        raise XdbError("memory read returned invalid hex data") from e
    if len(data) != size:
        raise XdbError(f"memory read returned {len(data)} byte(s), expected {size}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(data)
    return {
        "space": space,
        "addr": addr,
        "addr_hex": hex(addr),
        "size": size,
        "out": str(out_path),
        "sha256": _sha256(data),
    }


def _changed_ranges(before: bytes, after: bytes) -> list[dict[str, Any]]:
    ranges: list[dict[str, Any]] = []
    max_len = max(len(before), len(after))
    index = 0
    while index < max_len:
        before_byte = before[index] if index < len(before) else None
        after_byte = after[index] if index < len(after) else None
        if before_byte == after_byte:
            index += 1
            continue
        start = index
        before_values: list[int | None] = []
        after_values: list[int | None] = []
        while index < max_len:
            before_byte = before[index] if index < len(before) else None
            after_byte = after[index] if index < len(after) else None
            if before_byte == after_byte:
                break
            before_values.append(before_byte)
            after_values.append(after_byte)
            index += 1
        before_hex = "".join("--" if value is None else f"{value:02x}" for value in before_values)
        after_hex = "".join("--" if value is None else f"{value:02x}" for value in after_values)
        ranges.append(
            {
                "offset": start,
                "offset_hex": hex(start),
                "size": index - start,
                "before_hex": before_hex,
                "after_hex": after_hex,
            }
        )
    return ranges


def diff_memory_files(before_path: str, after_path: str) -> dict[str, Any]:
    before_file = Path(before_path).expanduser()
    after_file = Path(after_path).expanduser()
    if not before_file.is_file():
        raise XdbError(f"memory diff input not found: {before_file}")
    if not after_file.is_file():
        raise XdbError(f"memory diff input not found: {after_file}")
    before = before_file.read_bytes()
    after = after_file.read_bytes()
    ranges = _changed_ranges(before, after)
    return {
        "same": not ranges,
        "before": str(before_file.resolve()),
        "after": str(after_file.resolve()),
        "before_size": len(before),
        "after_size": len(after),
        "before_sha256": _sha256(before),
        "after_sha256": _sha256(after),
        "changed_range_count": len(ranges),
        "changed_ranges": ranges,
    }
