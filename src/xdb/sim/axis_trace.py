from __future__ import annotations

import math
import re
import time
from typing import Any, Protocol, cast

from xdb.errors import XdbError
from xdb.sim.sim_time import parse_duration_tokens, parse_sim_time

_AXIS_REQUIRED_SIGNALS = ("tvalid", "tready", "tdata")
_AXIS_OPTIONAL_SIGNALS = ("tkeep", "tlast", "tid")


class AxisTraceDriver(Protocol):
    def objects(self, scope: str) -> dict[str, Any]: ...

    def read_signals(self, signals: list[str]) -> dict[str, Any]: ...


class AxisTraceCollectDriver(AxisTraceDriver, Protocol):
    def time(self) -> dict[str, Any]: ...

    def run(self, tokens: list[str]) -> dict[str, Any]: ...


def _parse_logic_int(value: str) -> tuple[int | None, int | None]:
    normalized = value.strip().replace("_", "")
    if not normalized:
        return None, None
    sized = re.match(r"^([0-9]+)'([bBoOdDhH])([0-9a-fA-FxXzZ]+)$", normalized)
    if sized:
        width = int(sized.group(1))
        radix = sized.group(2).lower()
        digits = sized.group(3)
        if re.search(r"[xXzZ]", digits):
            return None, width
        base = {"b": 2, "o": 8, "d": 10, "h": 16}[radix]
        return int(digits, base), width
    prefixed = re.match(r"^0([boxd])([0-9a-fA-F]+)$", normalized, re.IGNORECASE)
    if prefixed:
        radix = prefixed.group(1).lower()
        digits = prefixed.group(2)
        base = {"b": 2, "o": 8, "d": 10, "x": 16}.get(radix)
        if base is None:
            return None, None
        return int(digits, base), None
    if re.search(r"[xXzZ]", normalized):
        return None, None
    if re.fullmatch(r"[01]+", normalized):
        return int(normalized, 2), len(normalized)
    if re.fullmatch(r"[0-9]+", normalized):
        return int(normalized, 10), None
    if re.fullmatch(r"[0-9a-fA-F]+", normalized):
        return int(normalized, 16), None
    return None, None


class AxisTraceSampler:
    def __init__(
        self,
        driver: AxisTraceDriver,
        interface_paths: list[str],
        *,
        decode_bytes: bool = False,
        lane_order: str = "low-to-high",
        include_idle: bool = False,
        only_handshakes: bool = False,
    ):
        self.driver = driver
        self.interface_paths = interface_paths
        self.decode_bytes = decode_bytes
        self.lane_order = lane_order
        self.include_idle = include_idle
        if lane_order not in {"low-to-high", "high-to-low"}:
            raise XdbError("lane order must be 'low-to-high' or 'high-to-low'")
        self.only_handshakes = only_handshakes
        self.interface_signals = {
            path: self._axis_child_signal_map(path) for path in interface_paths
        }
        self.signal_paths = [
            str(meta.get("path") or "")
            for signal_map in self.interface_signals.values()
            for meta in signal_map.values()
            if str(meta.get("path") or "")
        ]
        self.records: list[dict[str, Any]] = []
        self._beat_counts = {path: 0 for path in interface_paths}

    def sample(self, time_text: str) -> None:
        if not self.interface_paths:
            return
        sampled = self.driver.read_signals(self.signal_paths)
        sampled_signals = [
            cast(dict[str, Any], item)
            for item in list(sampled.get("signals") or [])
            if isinstance(item, dict)
        ]
        for interface_path, signal_map in self.interface_signals.items():
            signal_values = self._axis_signal_value_map(signal_map, sampled_signals)
            tvalid = str((signal_values.get("tvalid") or {}).get("value") or "")
            tready = str((signal_values.get("tready") or {}).get("value") or "")
            handshake = tvalid == "1" and tready == "1"
            if self.only_handshakes and not handshake:
                continue
            if not self.include_idle and not handshake:
                continue
            beat_index = None
            if handshake:
                beat_index = self._beat_counts[interface_path]
                self._beat_counts[interface_path] += 1
            self.records.append(
                self._axis_record(
                    interface_path=interface_path,
                    time_text=time_text,
                    signal_values=signal_values,
                    beat_index=beat_index,
                )
            )

    def _axis_child_signal_map(self, interface_path: str) -> dict[str, dict[str, Any]]:
        result = self.driver.objects(interface_path)
        metadata = [
            cast(dict[str, Any], item)
            for item in list(result.get("metadata") or [])
            if isinstance(item, dict)
        ]
        signal_map: dict[str, dict[str, Any]] = {}
        for item in metadata:
            path = str(item.get("path") or "")
            base = path.rsplit("/", 1)[-1].lower()
            if base in {*_AXIS_REQUIRED_SIGNALS, *_AXIS_OPTIONAL_SIGNALS}:
                signal_map[base] = item
        missing = [name for name in _AXIS_REQUIRED_SIGNALS if name not in signal_map]
        if missing:
            raise XdbError(
                f"AXIS interface {interface_path!r} is missing required signals: {', '.join(missing)}"
            )
        return signal_map

    @staticmethod
    def _axis_signal_value_map(
        signal_metadata: dict[str, dict[str, Any]],
        sampled_signals: list[dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        by_path = {
            str(item.get("path") or ""): item
            for item in sampled_signals
            if isinstance(item, dict) and item.get("path")
        }
        return {
            name: cast(dict[str, Any], by_path.get(str(meta.get("path") or ""), meta))
            for name, meta in signal_metadata.items()
        }

    @staticmethod
    def _axis_decode_bytes(
        signal_values: dict[str, dict[str, Any]], lane_order: str
    ) -> tuple[list[str] | None, list[str] | None, int | None]:
        tdata = signal_values.get("tdata") or {}
        tkeep = signal_values.get("tkeep") or {}
        data_value, parsed_data_width = _parse_logic_int(str(tdata.get("value") or ""))
        keep_value, parsed_keep_width = _parse_logic_int(str(tkeep.get("value") or ""))
        meta_data_width = tdata.get("width")
        data_width = int(meta_data_width) if isinstance(meta_data_width, int) else parsed_data_width
        lane_count = None
        if isinstance(data_width, int) and data_width > 0:
            lane_count = max(1, math.ceil(data_width / 8))
        elif isinstance(parsed_keep_width, int) and parsed_keep_width > 0:
            lane_count = parsed_keep_width
        if lane_count is None or lane_count <= 0 or data_value is None:
            return None, None, data_width
        bytes_low_to_high = [f"{(data_value >> (8 * i)) & 0xFF:02x}" for i in range(lane_count)]
        keep_bits_low_to_high = [
            True if keep_value is None else bool((keep_value >> i) & 1) for i in range(lane_count)
        ]
        if lane_order == "high-to-low":
            ordered_bytes = list(reversed(bytes_low_to_high))
            ordered_keep = list(reversed(keep_bits_low_to_high))
        else:
            ordered_bytes = bytes_low_to_high
            ordered_keep = keep_bits_low_to_high
        valid_bytes = [byte for byte, keep in zip(ordered_bytes, ordered_keep) if keep]
        return ordered_bytes, valid_bytes, data_width

    def _axis_record(
        self,
        *,
        interface_path: str,
        time_text: str,
        signal_values: dict[str, dict[str, Any]],
        beat_index: int | None,
    ) -> dict[str, Any]:
        tvalid = str((signal_values.get("tvalid") or {}).get("value") or "")
        tready = str((signal_values.get("tready") or {}).get("value") or "")
        record: dict[str, Any] = {
            "interface": interface_path,
            "time": time_text,
            "wallclock_seconds": time.monotonic(),
            "handshake": tvalid == "1" and tready == "1",
            "tvalid": tvalid,
            "tready": tready,
            "tdata": str((signal_values.get("tdata") or {}).get("value") or ""),
            "tkeep": str((signal_values.get("tkeep") or {}).get("value") or ""),
            "tlast": str((signal_values.get("tlast") or {}).get("value") or ""),
            "tid": str((signal_values.get("tid") or {}).get("value") or ""),
        }
        if beat_index is not None:
            record["beat_index"] = beat_index
        if self.decode_bytes:
            decoded_bytes, valid_bytes, width_bits = self._axis_decode_bytes(
                signal_values,
                self.lane_order,
            )
            record["lane_order"] = self.lane_order
            record["data_width_bits"] = width_bits
            record["bytes"] = decoded_bytes
            record["valid_bytes"] = valid_bytes
        return record


def collect_axis_trace(
    driver: AxisTraceCollectDriver,
    interface_paths: list[str],
    duration_tokens: list[str],
    *,
    step_tokens: list[str],
    decode_bytes: bool = False,
    lane_order: str = "low-to-high",
    include_idle: bool = False,
    only_handshakes: bool = False,
) -> dict[str, Any]:
    if not interface_paths:
        raise XdbError("missing AXIS interface path")

    duration_text, duration_value = parse_duration_tokens(duration_tokens)
    step_text, step_value = parse_duration_tokens(step_tokens)
    if duration_value <= 0:
        raise XdbError("AXIS trace duration must be > 0")
    if step_value <= 0:
        raise XdbError("AXIS trace step must be > 0")

    sampler = AxisTraceSampler(
        driver,
        interface_paths,
        decode_bytes=decode_bytes,
        lane_order=lane_order,
        include_idle=include_idle,
        only_handshakes=only_handshakes,
    )
    start_time_text = str(driver.time().get("time") or "")
    current_time_text = start_time_text
    current_time_value = parse_sim_time(current_time_text)
    end_time_value = current_time_value + duration_value

    iterations = 0
    while current_time_value < end_time_value:
        sampler.sample(current_time_text)
        run_result = driver.run(step_tokens)
        next_time_text = str(run_result.get("time_after") or "")
        next_time_value = parse_sim_time(next_time_text)
        iterations += 1
        if next_time_value <= current_time_value:
            raise XdbError("simulation did not advance while tracing AXIS activity")
        current_time_text = next_time_text
        current_time_value = next_time_value

    return {
        "interfaces": interface_paths,
        "duration": duration_text,
        "step": step_text,
        "time_before": start_time_text,
        "time_after": current_time_text,
        "iterations": iterations,
        "decode_bytes": bool(decode_bytes),
        "lane_order": lane_order,
        "include_idle": bool(include_idle),
        "only_handshakes": bool(only_handshakes),
        "records": sampler.records,
    }
