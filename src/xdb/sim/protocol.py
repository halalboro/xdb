from __future__ import annotations

from typing import Any

from xdb.sim.types import SimRequest


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
OP_UNTIL = "until"
OP_UNTIL_SIGNAL = "until_signal"
OP_BREAKPOINT_ADD = "breakpoint_add"
OP_BREAKPOINT_LIST = "breakpoint_list"
OP_BREAKPOINT_REMOVE = "breakpoint_remove"
OP_BREAKPOINT_CLEAR = "breakpoint_clear"
OP_TCL = "tcl"
OP_SOURCE = "source"
OP_FORCE = "force"
OP_RELEASE = "release"
OP_CSR_READ = "csr_read"
OP_CSR_WRITE = "csr_write"
OP_MEM_MAP = "mem_map"
OP_MEM_UNMAP = "mem_unmap"
OP_MEM_LIST = "mem_list"
OP_MEM_RESET = "mem_reset"
OP_MEM_WRITE = "mem_write"
OP_MEM_READ = "mem_read"
OP_INVOKE = "invoke"
OP_COMPLETED = "completed"
OP_CLEAR_COMPLETED = "clear_completed"
OP_IRQ_WAIT = "irq_wait"
OP_COYOTE_STATUS = "coyote_status"
OP_READ_SIGNALS = "read_signals"
OP_SNAPSHOT = "snapshot"
OP_DIFF_SNAPSHOT = "diff_snapshot"
OP_WATCH_CHANGES = "watch_changes"
OP_VCD_START = "vcd_start"
OP_VCD_STOP = "vcd_stop"
OP_VCD_STATUS = "vcd_status"
OP_DESCRIBE = "describe"
OP_ASSERT_SIGNAL = "assert_signal"
OP_ASSERT_TCL = "assert_tcl"
OP_EXPECT_SIGNAL = "expect_signal"
OP_EXPECT_CHANGE = "expect_change"
OP_EXPECT_CONDITION = "expect_condition"
OP_TRACE_TRANSACTIONS = "trace_transactions"
OP_TRACE_EVENTS_CLEAR = "trace_events_clear"
OP_TRACE_EVENTS_GET = "trace_events_get"
OP_WITH_TRACE = "with_trace"


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
    OP_UNTIL,
    OP_UNTIL_SIGNAL,
    OP_BREAKPOINT_ADD,
    OP_BREAKPOINT_LIST,
    OP_BREAKPOINT_REMOVE,
    OP_BREAKPOINT_CLEAR,
    OP_TCL,
    OP_SOURCE,
    OP_FORCE,
    OP_RELEASE,
    OP_CSR_READ,
    OP_CSR_WRITE,
    OP_MEM_MAP,
    OP_MEM_UNMAP,
    OP_MEM_LIST,
    OP_MEM_RESET,
    OP_MEM_WRITE,
    OP_MEM_READ,
    OP_INVOKE,
    OP_COMPLETED,
    OP_CLEAR_COMPLETED,
    OP_IRQ_WAIT,
    OP_COYOTE_STATUS,
    OP_READ_SIGNALS,
    OP_SNAPSHOT,
    OP_DIFF_SNAPSHOT,
    OP_WATCH_CHANGES,
    OP_VCD_START,
    OP_VCD_STOP,
    OP_VCD_STATUS,
    OP_DESCRIBE,
    OP_ASSERT_SIGNAL,
    OP_ASSERT_TCL,
    OP_EXPECT_SIGNAL,
    OP_EXPECT_CHANGE,
    OP_EXPECT_CONDITION,
    OP_TRACE_TRANSACTIONS,
    OP_TRACE_EVENTS_CLEAR,
    OP_TRACE_EVENTS_GET,
    OP_WITH_TRACE,
}


def make_request(op: str, **args: Any) -> SimRequest:
    if op not in ALL_OPERATIONS:
        raise ValueError(f"unsupported sim operation: {op}")
    return {"op": op, "args": args}
