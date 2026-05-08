from __future__ import annotations

from typing import Any

from .types import SimRequest


OP_STATUS = "status"
OP_RUN = "run"
OP_RESTART = "restart"
OP_CLOSE = "close"
OP_TIME = "time"
OP_GET = "get"
OP_GET_MANY = "get_many"
OP_SCOPES = "scopes"
OP_OBJECTS = "objects"
OP_TOP = "top"
OP_WAVE_ADD = "wave_add"
OP_STEP = "step"
OP_BREAKPOINT_ADD = "breakpoint_add"
OP_BREAKPOINT_CLEAR = "breakpoint_clear"
OP_TCL = "tcl"


ALL_OPERATIONS = {
    OP_STATUS,
    OP_RUN,
    OP_RESTART,
    OP_CLOSE,
    OP_TIME,
    OP_GET,
    OP_GET_MANY,
    OP_SCOPES,
    OP_OBJECTS,
    OP_TOP,
    OP_WAVE_ADD,
    OP_STEP,
    OP_BREAKPOINT_ADD,
    OP_BREAKPOINT_CLEAR,
    OP_TCL,
}


def make_request(op: str, **args: Any) -> SimRequest:
    if op not in ALL_OPERATIONS:
        raise ValueError(f"unsupported sim operation: {op}")
    return {"op": op, "args": args}
