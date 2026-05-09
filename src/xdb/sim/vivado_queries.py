from __future__ import annotations

import re
import uuid
from typing import Any, Protocol

from xdb.errors import XdbError
from xdb.sim.tcl_api import build_proc_request


class _VivadoQueryHost(Protocol):
    top: str
    project: str
    simset: str
    mode: str
    runtime_root: str
    work_dir: str
    _snapshots: dict[str, dict[str, Any]]

    @property
    def _coyote(self) -> object | None: ...

    def request(self, body_tcl: str, timeout: int = 120) -> dict[str, Any]: ...

    def run(self, tokens: list[str]) -> dict[str, Any]: ...

    def snapshot_scope(
        self, scope: str, *, name: str | None = None
    ) -> dict[str, Any]: ...


class VivadoQueryMixin:
    @staticmethod
    def _infer_known_signal_paths(objects: list[dict[str, Any]], patterns: list[str]) -> list[str]:
        compiled = [re.compile(pattern, re.IGNORECASE) for pattern in patterns]
        out: list[str] = []
        seen: set[str] = set()
        for obj in objects:
            path = str(obj.get("path") or "")
            if not path or path in seen:
                continue
            base = path.rsplit("/", 1)[-1]
            if any(regex.search(base) for regex in compiled):
                seen.add(path)
                out.append(path)
        return out

    @staticmethod
    def _infer_dut_scope(top_scope: str, child_scopes: list[str]) -> str | None:
        if not child_scopes:
            return None
        preferred: list[tuple[int, str]] = []
        for scope in child_scopes:
            base = scope.rsplit("/", 1)[-1]
            score = None
            lowered = base.lower()
            if lowered == "dut":
                score = 0
            elif lowered == "inst_dut":
                score = 1
            elif "dut" in lowered:
                score = 2
            elif lowered.startswith("inst_"):
                score = 3
            if score is not None:
                preferred.append((score, scope))
        if preferred:
            preferred.sort(key=lambda item: (item[0], len(item[1]), item[1]))
            return preferred[0][1]
        return child_scopes[0] if child_scopes else (top_scope or None)

    def describe_session(self: _VivadoQueryHost) -> dict[str, Any]:
        result = self.request(build_proc_request("xdb_api_describe", self.top))
        objects = list(result.get("objects") or [])
        top_scope = str(result.get("top_scope") or "")
        child_scopes = [str(scope) for scope in list(result.get("child_scopes") or [])]
        clocks = VivadoQueryMixin._infer_known_signal_paths(
            objects,
            [r"(^|_)(clk|clock)(_|$)", r"(^|_)(aclk)(_|$)"],
        )
        resets = VivadoQueryMixin._infer_known_signal_paths(
            objects,
            [r"(^|_)(rst|reset|aresetn|resetn|srst|rstn)(_|$)"],
        )
        dut_scope = VivadoQueryMixin._infer_dut_scope(top_scope, child_scopes)
        common_scopes = [scope for scope in [top_scope, *child_scopes] if scope]
        return {
            "top": result.get("top", self.top),
            "top_scope": top_scope,
            "time": result.get("time", ""),
            "dut": dut_scope,
            "clocks": clocks,
            "resets": resets,
            "common_scopes": common_scopes,
            "root_scopes": result.get("root_scopes", []),
            "child_scopes": child_scopes,
            "child_scope_metadata": result.get("child_scope_metadata", []),
            "project": self.project,
            "simset": self.simset,
            "mode": self.mode,
            "runtime_root": self.runtime_root,
            "work_dir": self.work_dir,
            "coyote": self._coyote is not None,
        }

    def get_signal(self: _VivadoQueryHost, signal: str) -> dict[str, Any]:
        return self.request(build_proc_request("xdb_api_get_signal", signal))

    def get_many(self: _VivadoQueryHost, pattern: str) -> dict[str, Any]:
        return self.request(build_proc_request("xdb_api_get_many", pattern))

    def read_signals(self: _VivadoQueryHost, signals: list[str]) -> dict[str, Any]:
        return self.request(build_proc_request("xdb_api_read_signals", signals))

    def scopes(self: _VivadoQueryHost, scope: str | None) -> dict[str, Any]:
        pattern = "*" if not scope else f"{scope}/*"
        return self.request(build_proc_request("xdb_api_scopes", scope or "", pattern))

    def objects(self: _VivadoQueryHost, scope: str) -> dict[str, Any]:
        return self.request(build_proc_request("xdb_api_objects", scope))

    def snapshot_scope(self: _VivadoQueryHost, scope: str, *, name: str | None = None) -> dict[str, Any]:
        result = self.request(build_proc_request("xdb_api_snapshot_scope", scope))
        snapshot_id = name or f"snapshot-{uuid.uuid4().hex[:12]}"
        if snapshot_id in self._snapshots:
            raise XdbError(f"snapshot already exists: {snapshot_id}")
        stored = {
            "snapshot": snapshot_id,
            "scope": result.get("scope", scope),
            "time": result.get("time", ""),
            "objects": list(result.get("objects") or []),
        }
        stored["count"] = len(stored["objects"])
        self._snapshots[snapshot_id] = stored
        return dict(stored)

    @staticmethod
    def _diff_snapshot_payload(
        before: dict[str, Any], after: dict[str, Any]
    ) -> dict[str, Any]:
        before_map = {str(obj.get("path")): obj for obj in list(before.get("objects") or [])}
        after_map = {str(obj.get("path")): obj for obj in list(after.get("objects") or [])}
        added_paths = sorted(set(after_map) - set(before_map))
        removed_paths = sorted(set(before_map) - set(after_map))
        shared_paths = sorted(set(before_map) & set(after_map))

        changed = []
        unchanged_count = 0
        compare_fields = ["kind", "width", "value", "value_radix", "parent_scope"]
        for path in shared_paths:
            old_obj = before_map[path]
            new_obj = after_map[path]
            changed_fields = [field for field in compare_fields if old_obj.get(field) != new_obj.get(field)]
            if changed_fields:
                changed.append(
                    {
                        "path": path,
                        "fields": changed_fields,
                        "before": old_obj,
                        "after": new_obj,
                    }
                )
            else:
                unchanged_count += 1

        return {
            "before": str(before.get("snapshot") or ""),
            "after": str(after.get("snapshot") or ""),
            "scope_before": str(before.get("scope") or ""),
            "scope_after": str(after.get("scope") or ""),
            "time_before": str(before.get("time") or ""),
            "time_after": str(after.get("time") or ""),
            "added": [after_map[path] for path in added_paths],
            "removed": [before_map[path] for path in removed_paths],
            "changed": changed,
            "unchanged_count": unchanged_count,
            "before_count": len(before_map),
            "after_count": len(after_map),
            "changed_count": len(changed),
            "added_count": len(added_paths),
            "removed_count": len(removed_paths),
        }

    def diff_snapshot(self: _VivadoQueryHost, before: str, after: str) -> dict[str, Any]:
        before_snapshot = self._snapshots.get(before)
        if before_snapshot is None:
            raise XdbError(f"unknown snapshot: {before}")
        after_snapshot = self._snapshots.get(after)
        if after_snapshot is None:
            raise XdbError(f"unknown snapshot: {after}")
        return VivadoQueryMixin._diff_snapshot_payload(before_snapshot, after_snapshot)

    def watch_changes(self: _VivadoQueryHost, scope: str, *, duration_tokens: list[str]) -> dict[str, Any]:
        before_id = f"watch-before-{uuid.uuid4().hex[:10]}"
        after_id = f"watch-after-{uuid.uuid4().hex[:10]}"
        before = self.snapshot_scope(scope, name=before_id)
        run_result = self.run(duration_tokens)
        after = self.snapshot_scope(scope, name=after_id)
        diff = VivadoQueryMixin._diff_snapshot_payload(before, after)
        return {
            "scope": scope,
            "duration": " ".join(duration_tokens),
            "run": run_result,
            "before": before,
            "after": after,
            "diff": diff,
        }
