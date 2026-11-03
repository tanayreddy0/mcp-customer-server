"""Interop test: drive the server with the official MCP Python client over stdio."""

import sys
from pathlib import Path

import anyio
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import McpError
from mcp.types import INVALID_PARAMS

SERVER = Path(__file__).resolve().parent.parent / "server.py"
PARAMS = StdioServerParameters(command=sys.executable, args=[str(SERVER)])


async def _session_run(body):
    async with stdio_client(PARAMS) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await body(session)


def run(body):
    return anyio.run(_session_run, body)


def test_official_client_lists_and_calls_tools():
    async def body(session: ClientSession):
        tools = await session.list_tools()
        names = sorted(t.name for t in tools.tools)
        record = await session.call_tool("get_customer_record", {"customer_id": "CUST-00001"})
        refund = await session.call_tool(
            "trigger_refund",
            {"customer_id": "CUST-00001", "amount": 12.5, "reason": "Duplicate charge on card"},
        )
        return names, record, refund

    names, record, refund = run(body)
    assert names == ["get_customer_record", "trigger_refund"]
    assert record.isError is False and record.structuredContent["name"] == "Ada Lovelace"
    assert refund.isError is False and refund.structuredContent["status"] == "processed"


def test_official_client_receives_jsonrpc_invalid_params():
    async def body(session: ClientSession):
        with pytest.raises(McpError) as exc:
            await session.call_tool("trigger_refund", {"customer_id": "CUST-00001", "amount": -3, "reason": "bad"})
        return exc.value.error

    error = run(body)
    assert error.code == INVALID_PARAMS
    assert {e["field"] for e in error.data["errors"]} == {"amount", "reason"}
