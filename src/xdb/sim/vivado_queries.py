from __future__ import annotations

import re
import uuid

from ..errors import XdbError
from .tcl_helpers import _tcl_list, _tcl_string


class VivadoQueryMixin:
    @staticmethod
    def _infer_known_signal_paths(objects: list[dict], patterns: list[str]) -> list[str]:
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

    def describe_session(self) -> dict:
        body = fr'''
set __xdb_top_name {_tcl_string(self.top)}
set __xdb_time [current_time]
set __xdb_root_scopes [get_scopes *]
set __xdb_top_scope ""
foreach __xdb_scope $__xdb_root_scopes {{
  if {{[xdb_basename $__xdb_scope] eq $__xdb_top_name}} {{
    set __xdb_top_scope $__xdb_scope
    break
  }}
}}
if {{$__xdb_top_scope eq "" && [llength $__xdb_root_scopes] == 1}} {{
  set __xdb_top_scope [lindex $__xdb_root_scopes 0]
}}
if {{$__xdb_top_scope eq ""}} {{
  set __xdb_top_scope $__xdb_top_name
}}
set __xdb_child_scopes {{}}
catch {{set __xdb_child_scopes [get_scopes [format "%s/*" $__xdb_top_scope]]}}
set __xdb_objects [xdb_collect_snapshot_value_objects $__xdb_top_scope]
xdb_reply_ok_fields $__xdb_request_id "\"top\":[xdb_json_string $__xdb_top_name],\"top_scope\":[xdb_json_string $__xdb_top_scope],\"time\":[xdb_json_string $__xdb_time],\"root_scopes\":[xdb_json_array_strings $__xdb_root_scopes],\"child_scopes\":[xdb_json_array_strings $__xdb_child_scopes],\"child_scope_metadata\":[xdb_json_object_metadata_array $__xdb_child_scopes \"module\" 0],\"objects\":[xdb_json_object_metadata_array $__xdb_objects \"signal\" 1]"
'''
        result = self.request(body)
        objects = list(result.get("objects") or [])
        top_scope = str(result.get("top_scope") or "")
        child_scopes = [str(scope) for scope in list(result.get("child_scopes") or [])]
        clocks = self._infer_known_signal_paths(
            objects,
            [r"(^|_)(clk|clock)(_|$)", r"(^|_)(aclk)(_|$)"],
        )
        resets = self._infer_known_signal_paths(
            objects,
            [r"(^|_)(rst|reset|aresetn|resetn|srst|rstn)(_|$)"],
        )
        dut_scope = self._infer_dut_scope(top_scope, child_scopes)
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

    def get_signal(self, signal: str) -> dict:
        body = fr'''
set __xdb_signal {_tcl_string(signal)}
set __xdb_value [get_value $__xdb_signal]
set __xdb_object_json [xdb_json_object_metadata $__xdb_signal "signal" 1]
xdb_reply_ok_fields $__xdb_request_id "\"signal\":[xdb_json_string $__xdb_signal],\"value\":[xdb_json_string $__xdb_value],\"object\":$__xdb_object_json"
'''
        return self.request(body)

    def get_many(self, pattern: str) -> dict:
        body = fr'''
set __xdb_pattern {_tcl_string(pattern)}
set __xdb_items [get_objects $__xdb_pattern]
xdb_reply_ok_fields $__xdb_request_id "\"pattern\":[xdb_json_string $__xdb_pattern],\"signals\":[xdb_json_signal_values $__xdb_items],\"objects\":[xdb_json_object_metadata_array $__xdb_items \"signal\" 1]"
'''
        return self.request(body)

    def read_signals(self, signals: list[str]) -> dict:
        body = fr'''
set __xdb_signals {_tcl_list(signals)}
xdb_reply_ok_fields $__xdb_request_id "\"signals\":[xdb_json_object_metadata_array $__xdb_signals \"signal\" 1]"
'''
        return self.request(body)

    def scopes(self, scope: str | None) -> dict:
        pattern = "*" if not scope else f"{scope}/*"
        body = fr'''
set __xdb_scope {_tcl_string(scope or "")}
set __xdb_pattern {_tcl_string(pattern)}
set __xdb_scopes [get_scopes $__xdb_pattern]
xdb_reply_ok_fields $__xdb_request_id "\"scope\":[xdb_json_string $__xdb_scope],\"scopes\":[xdb_json_array_strings $__xdb_scopes],\"metadata\":[xdb_json_object_metadata_array $__xdb_scopes \"module\" 0]"
'''
        return self.request(body)

    def objects(self, scope: str) -> dict:
        body = fr'''
set __xdb_scope {_tcl_string(scope)}
set __xdb_pattern [format "%s/*" $__xdb_scope]
set __xdb_objects [get_objects $__xdb_pattern]
xdb_reply_ok_fields $__xdb_request_id "\"scope\":[xdb_json_string $__xdb_scope],\"objects\":[xdb_json_array_strings $__xdb_objects],\"metadata\":[xdb_json_object_metadata_array $__xdb_objects \"signal\" 1]"
'''
        return self.request(body)

    def snapshot_scope(self, scope: str, *, name: str | None = None) -> dict:
        body = fr'''
set __xdb_scope {_tcl_string(scope)}
set __xdb_objects [xdb_collect_snapshot_value_objects $__xdb_scope]
set __xdb_time [current_time]
xdb_reply_ok_fields $__xdb_request_id "\"scope\":[xdb_json_string $__xdb_scope],\"time\":[xdb_json_string $__xdb_time],\"objects\":[xdb_json_object_metadata_array $__xdb_objects \"signal\" 1]"
'''
        result = self.request(body)
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
    def _diff_snapshot_payload(before: dict, after: dict) -> dict:
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

    def diff_snapshot(self, before: str, after: str) -> dict:
        before_snapshot = self._snapshots.get(before)
        if before_snapshot is None:
            raise XdbError(f"unknown snapshot: {before}")
        after_snapshot = self._snapshots.get(after)
        if after_snapshot is None:
            raise XdbError(f"unknown snapshot: {after}")
        return self._diff_snapshot_payload(before_snapshot, after_snapshot)

    def watch_changes(self, scope: str, *, duration_tokens: list[str]) -> dict:
        before_id = f"watch-before-{uuid.uuid4().hex[:10]}"
        after_id = f"watch-after-{uuid.uuid4().hex[:10]}"
        before = self.snapshot_scope(scope, name=before_id)
        run_result = self.run(duration_tokens)
        after = self.snapshot_scope(scope, name=after_id)
        diff = self._diff_snapshot_payload(before, after)
        return {
            "scope": scope,
            "duration": " ".join(duration_tokens),
            "run": run_result,
            "before": before,
            "after": after,
            "diff": diff,
        }
