from __future__ import annotations

from typing import Any

from xdb.sim.tcl_helpers import _tcl_list, _tcl_string


class TclRaw:
    def __init__(self, code: str):
        self.code = code


def _tcl_arg(value: Any) -> str:
    if isinstance(value, TclRaw):
        return value.code
    if isinstance(value, list):
        return _tcl_list([str(v) for v in value])
    if value is None:
        return _tcl_string("")
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    return _tcl_string(str(value))


def build_proc_request(proc_name: str, *args: Any) -> str:
    rendered_args = " ".join(_tcl_arg(arg) for arg in args)
    call = proc_name if not rendered_args else f"{proc_name} {rendered_args}"
    return f"set __xdb_fields [{call}]\nxdb_reply_ok_fields $__xdb_request_id $__xdb_fields"
