from __future__ import annotations

import argparse
from pathlib import Path
from typing import NoReturn

from xdb.errors import XdbError
from xdb.sim.coyote import parse_hex_bytes
from xdb.sim.protocol import (
    OP_CLEAR_COMPLETED,
    OP_COMPLETED,
    OP_COYOTE_STATUS,
    OP_CSR_READ,
    OP_CSR_WRITE,
    OP_SERVICE_CSR_READ,
    OP_SERVICE_CSR_WRITE,
    OP_INVOKE,
    OP_IRQ_WAIT,
    OP_MEM_LIST,
    OP_MEM_MAP,
    OP_MEM_READ,
    OP_MEM_RESET,
    OP_MEM_UNMAP,
    OP_MEM_WRITE,
    OP_RUN,
    OP_STEP,
    OP_UNTIL,
    OP_UNTIL_SIGNAL,
    make_request,
)
from xdb.sim.types import SimRequest


class _WithTraceArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise XdbError(message)

    def exit(self, status: int = 0, message: str | None = None) -> NoReturn:
        if status:
            raise XdbError((message or "invalid wrapped command arguments").strip())
        raise XdbError((message or "unexpected parser exit").strip())


def _with_trace_parser() -> argparse.ArgumentParser:
    return _WithTraceArgumentParser(add_help=False)


def _parse_with_trace_wait_args(
    rest: list[str], *, positional_count: int
) -> tuple[list[str], float | None, int | None, list[str]]:
    step_tokens = ["10", "ns"]
    timeout_seconds: float | None = None
    max_iterations: int | None = None
    positionals: list[str] = []
    index = 0
    while index < len(rest):
        token = rest[index]
        if token == "--timeout":
            if index + 1 >= len(rest):
                raise XdbError("--timeout requires a value")
            timeout_seconds = float(rest[index + 1])
            index += 2
            continue
        if token == "--max-iterations":
            if index + 1 >= len(rest):
                raise XdbError("--max-iterations requires a value")
            max_iterations = int(rest[index + 1])
            index += 2
            continue
        if token == "--step":
            step_start = index + 1
            step_end = step_start
            while step_end < len(rest) and not rest[step_end].startswith("--"):
                remaining_after = len(rest) - (step_end + 1)
                if remaining_after < positional_count:
                    break
                step_end += 1
            if step_end == step_start:
                raise XdbError("--step requires a duration")
            step_tokens = rest[step_start:step_end]
            index = step_end
            continue
        positionals = rest[index:]
        break
    if len(positionals) < positional_count:
        raise XdbError("missing wrapped wait command argument")
    return step_tokens, timeout_seconds, max_iterations, positionals


def parse_with_trace_command(command: list[str]) -> SimRequest:
    if not command:
        raise XdbError("missing command after '--'")
    tokens = list(command)
    if tokens and tokens[0] == "--":
        tokens = tokens[1:]
    if tokens[:2] == ["xdb", "sim"]:
        tokens = tokens[2:]
    elif tokens[:1] == ["sim"]:
        tokens = tokens[1:]
    else:
        raise XdbError("with-trace currently only supports wrapped 'xdb sim ...' commands")
    if not tokens:
        raise XdbError("missing wrapped 'xdb sim' subcommand")

    subcommand, rest = tokens[0], tokens[1:]
    if subcommand == "run":
        if not rest:
            raise XdbError("with-trace wrapped 'xdb sim run' requires an explicit duration")
        return make_request(OP_RUN, tokens=rest)
    if subcommand == "step":
        if not rest:
            return make_request(OP_STEP, count=1)
        if len(rest) == 1 and rest[0].isdigit():
            count = int(rest[0])
            if count <= 0:
                raise XdbError("step count must be > 0")
            return make_request(OP_STEP, count=count)
        return make_request(OP_STEP, time_tokens=rest)
    if subcommand in {"until", "wait", "wait-on-condition"}:
        wait_step, wait_timeout, wait_max_iterations, positionals = _parse_with_trace_wait_args(
            rest,
            positional_count=1,
        )
        return make_request(
            OP_UNTIL,
            expr=" ".join(positionals),
            step_tokens=wait_step,
            timeout_seconds=wait_timeout,
            max_iterations=wait_max_iterations,
        )
    if subcommand in {"until-signal", "wait-signal", "wait-on-signal"}:
        wait_step, wait_timeout, wait_max_iterations, positionals = _parse_with_trace_wait_args(
            rest,
            positional_count=2,
        )
        return make_request(
            OP_UNTIL_SIGNAL,
            signal=positionals[0],
            value=positionals[1],
            step_tokens=wait_step,
            timeout_seconds=wait_timeout,
            max_iterations=wait_max_iterations,
        )
    if subcommand == "coyote-status":
        if rest:
            raise XdbError("xdb sim coyote-status does not accept extra arguments in with-trace")
        return make_request(OP_COYOTE_STATUS)
    if subcommand == "clear-completed":
        if rest:
            raise XdbError("xdb sim clear-completed does not accept extra arguments in with-trace")
        return make_request(OP_CLEAR_COMPLETED)
    if subcommand == "invoke":
        parser = _with_trace_parser()
        parser.add_argument("opcode")
        parser.add_argument("--addr", default=None)
        parser.add_argument("--len", dest="length", default=None)
        parser.add_argument("--stream", default="host")
        parser.add_argument("--dest", default="0")
        parser.add_argument("--last", default=True)
        parser.add_argument("--src-addr", default=None)
        parser.add_argument("--src-len", default=None)
        parser.add_argument("--src-stream", default="host")
        parser.add_argument("--src-dest", default="0")
        parser.add_argument("--dst-addr", default=None)
        parser.add_argument("--dst-len", default=None)
        parser.add_argument("--dst-stream", default="host")
        parser.add_argument("--dst-dest", default="0")
        ns = parser.parse_args(rest)
        return make_request(
            OP_INVOKE,
            opcode=ns.opcode,
            addr=None if ns.addr is None else int(ns.addr, 0),
            length=None if ns.length is None else int(ns.length, 0),
            stream_name=ns.stream,
            dest=int(ns.dest, 0),
            last=bool(ns.last),
            src_addr=None if ns.src_addr is None else int(ns.src_addr, 0),
            src_length=None if ns.src_len is None else int(ns.src_len, 0),
            src_stream_name=ns.src_stream,
            src_dest=int(ns.src_dest, 0),
            dst_addr=None if ns.dst_addr is None else int(ns.dst_addr, 0),
            dst_length=None if ns.dst_len is None else int(ns.dst_len, 0),
            dst_stream_name=ns.dst_stream,
            dst_dest=int(ns.dst_dest, 0),
        )
    if subcommand == "completed":
        parser = _with_trace_parser()
        parser.add_argument("opcode")
        parser.add_argument("--count", type=int, default=None)
        parser.add_argument("--timeout", type=float, default=None)
        ns = parser.parse_args(rest)
        return make_request(
            OP_COMPLETED,
            opcode=ns.opcode,
            target_count=ns.count,
            timeout_seconds=ns.timeout,
        )
    if subcommand == "irq" and rest[:1] == ["wait"]:
        parser = _with_trace_parser()
        parser.add_argument("wait")
        parser.add_argument("--timeout", type=float, default=None)
        ns = parser.parse_args(rest)
        return make_request(OP_IRQ_WAIT, timeout_seconds=ns.timeout)
    if subcommand == "csr":
        if not rest:
            raise XdbError("missing xdb sim csr subcommand")
        csr_sub = rest[0]
        parser = _with_trace_parser()
        if csr_sub == "read":
            parser.add_argument("read")
            parser.add_argument("addr")
            parser.add_argument("--timeout", type=float, default=None)
            ns = parser.parse_args(rest)
            return make_request(
                OP_CSR_READ,
                addr=int(ns.addr, 0),
                timeout_seconds=ns.timeout,
            )
        if csr_sub == "write":
            parser.add_argument("write")
            parser.add_argument("addr")
            parser.add_argument("value")
            ns = parser.parse_args(rest)
            return make_request(OP_CSR_WRITE, addr=int(ns.addr, 0), value=int(ns.value, 0))
        raise XdbError(f"unsupported xdb sim csr subcommand for with-trace: {csr_sub}")
    if subcommand == "service-csr":
        if not rest:
            raise XdbError("missing xdb sim service-csr subcommand")
        service_csr_sub = rest[0]
        parser = _with_trace_parser()
        if service_csr_sub == "read":
            parser.add_argument("read")
            parser.add_argument("addr")
            parser.add_argument("--timeout", type=float, default=None)
            ns = parser.parse_args(rest)
            return make_request(
                OP_SERVICE_CSR_READ,
                addr=int(ns.addr, 0),
                timeout_seconds=ns.timeout,
            )
        if service_csr_sub == "write":
            parser.add_argument("write")
            parser.add_argument("addr")
            parser.add_argument("value")
            ns = parser.parse_args(rest)
            return make_request(
                OP_SERVICE_CSR_WRITE,
                addr=int(ns.addr, 0),
                value=int(ns.value, 0),
            )
        raise XdbError(
            f"unsupported xdb sim service-csr subcommand for with-trace: {service_csr_sub}"
        )
    if subcommand == "mem":
        if not rest:
            raise XdbError("missing xdb sim mem subcommand")
        mem_sub = rest[0]
        parser = _with_trace_parser()
        if mem_sub == "map":
            parser.add_argument("map")
            parser.add_argument("space")
            parser.add_argument("addr")
            parser.add_argument("size")
            ns = parser.parse_args(rest)
            return make_request(
                OP_MEM_MAP, space=ns.space, addr=int(ns.addr, 0), size=int(ns.size, 0)
            )
        if mem_sub == "unmap":
            parser.add_argument("unmap")
            parser.add_argument("space")
            parser.add_argument("addr")
            ns = parser.parse_args(rest)
            return make_request(OP_MEM_UNMAP, space=ns.space, addr=int(ns.addr, 0))
        if mem_sub == "list":
            parser.add_argument("list")
            parser.add_argument("space", nargs="?", default="host")
            ns = parser.parse_args(rest)
            return make_request(OP_MEM_LIST, space=ns.space)
        if mem_sub == "reset":
            parser.add_argument("reset")
            parser.add_argument("space", nargs="?", default="host")
            ns = parser.parse_args(rest)
            return make_request(OP_MEM_RESET, space=ns.space)
        if mem_sub == "read":
            parser.add_argument("read")
            parser.add_argument("space")
            parser.add_argument("addr")
            parser.add_argument("size")
            ns = parser.parse_args(rest)
            return make_request(
                OP_MEM_READ, space=ns.space, addr=int(ns.addr, 0), size=int(ns.size, 0)
            )
        if mem_sub == "write":
            parser.add_argument("write")
            parser.add_argument("space")
            parser.add_argument("addr")
            parser.add_argument("--hex", dest="hex_data", default=None)
            parser.add_argument("--text", dest="text_data", default=None)
            parser.add_argument("--file", default=None)
            ns = parser.parse_args(rest)
            data_bytes = b""
            if ns.hex_data is not None:
                data_bytes = parse_hex_bytes(ns.hex_data)
            elif ns.text_data is not None:
                data_bytes = ns.text_data.encode("utf-8")
            elif ns.file is not None:
                data_bytes = Path(ns.file).expanduser().read_bytes()
            else:
                raise XdbError("xdb sim mem write requires --hex, --text, or --file")
            return make_request(
                OP_MEM_WRITE,
                space=ns.space,
                addr=int(ns.addr, 0),
                data_hex=data_bytes.hex(),
            )
        raise XdbError(f"unsupported xdb sim mem subcommand for with-trace: {mem_sub}")
    raise XdbError(f"unsupported wrapped xdb sim subcommand for with-trace: {subcommand}")
