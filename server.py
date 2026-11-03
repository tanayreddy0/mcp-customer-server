"""Customer support MCP server using stdio.

The server provides customer lookup and refund tools. It keeps protocol output
on stdout, sends logs to stderr, and validates tool arguments with Pydantic.

Run with: python server.py
"""
from __future__ import annotations

import json
import logging
import math
import sys
from io import TextIOWrapper
from typing import Annotated, Any, get_args


import anyio
import mcp.types as types
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import McpError
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
)


# The MCP client expects stdout to contain only JSON-RPC messages. Keep a handle
# to the original stream for the transport and send ordinary prints to stderr.
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("customer-mcp")
_PROTOCOL_STDOUT = sys.stdout
sys.stdout = sys.stderr


# Shared input model settings: reject coercion and unexpected fields.
class StrictModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


CustomerId = Annotated[
    str,
    StringConstraints(pattern=r"^CUST-[0-9]{5}$"),
    Field(
        description="Customer ID in the format CUST- followed by five digits.",
        examples=["CUST-00042"],
    ),
]

MAX_REFUND_AMOUNT = 10_000.00


class GetCustomerRecordInput(StrictModel):
    customer_id: CustomerId


class TriggerRefundInput(StrictModel):
    customer_id: CustomerId
    amount: Annotated[
        float,
        Field(
            gt=0,
            le=MAX_REFUND_AMOUNT,
            allow_inf_nan=False,
            description=(
                f"Refund amount in USD; greater than 0 and no more than "
                f"{MAX_REFUND_AMOUNT:.2f}, with at most two decimal places."
            ),
            examples=[49.99],
        ),
    ]
    reason: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=10, max_length=500),
        Field(
            description="Reason for the refund (10 to 500 characters).",
            examples=["Item arrived damaged in shipping"],
        ),
    ]

    @field_validator("amount")
    @classmethod
    def validate_cents(cls, value: float) -> float:
        # Allow normal floating-point rounding, but not fractions of a cent.
        cents = value * 100
        if not math.isclose(cents, round(cents), abs_tol=1e-6):
            raise ValueError("amount must have at most 2 decimal places")
        return round(value, 2)


# In-memory sample data. Replace this with a database or CRM in production.
CUSTOMERS: dict[str, dict[str, Any]] = {
    "CUST-00001": {
        "customer_id": "CUST-00001",
        "name": "Ada Lovelace",
        "email": "ada@example.com",
        "tier": "gold",
        "status": "active",
    },
    "CUST-00042": {
        "customer_id": "CUST-00042",
        "name": "Alan Turing",
        "email": "alan@example.com",
        "tier": "silver",
        "status": "active",
    },
    "CUST-12345": {
        "customer_id": "CUST-12345",
        "name": "Grace Hopper",
        "email": "grace@example.com",
        "tier": "bronze",
        "status": "suspended",
    },
}
REFUNDS: list[dict[str, Any]] = []


def tool_success(data: dict[str, Any]) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(data, indent=2))],
        structuredContent=data,
        isError=False,
    )


def tool_failure(message: str) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=message)],
        isError=True,
    )


def get_customer_record(args: GetCustomerRecordInput) -> types.CallToolResult:
    customer = CUSTOMERS.get(args.customer_id)
    if customer is None:
        return tool_failure(f"No customer found with id {args.customer_id}")

    logger.info("Fetched customer %s", args.customer_id)
    return tool_success(customer)



def trigger_refund(args: TriggerRefundInput) -> types.CallToolResult:
    customer = CUSTOMERS.get(args.customer_id)
    if customer is None:
        return tool_failure(f"No customer found with id {args.customer_id}")
    if customer["status"] != "active":
        return tool_failure(
            f"Refunds are not allowed for customer {args.customer_id} "
            f"(status: {customer['status']})"
        )

    refund = {
        "refund_id": f"RFD-{len(REFUNDS) + 1:06d}",
        "customer_id": args.customer_id,
        "amount": args.amount,
        "currency": "USD",
        "reason": args.reason,
        "status": "processed",
    }
    REFUNDS.append(refund)
    logger.info(
        "Refund %s for %.2f issued to %s",
        refund["refund_id"],
        refund["amount"],
        args.customer_id,
    )
    return tool_success(refund)


# Keep each tool's schema and handler together so tools/list and tools/call use
# the same validation model.
TOOLS: dict[str, tuple[type[StrictModel], Any, str]] = {
    "get_customer_record": (
        GetCustomerRecordInput,
        get_customer_record,
        "Look up a customer's profile using a CUST-XXXXX customer ID.",
    ),
    "trigger_refund": (
        TriggerRefundInput,
        trigger_refund,
        "Issue a refund to an active customer. The amount must be positive and "
        "the reason must contain at least 10 characters.",
    ),
}

server = Server("customer-support-server")


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name=name,
            description=description,
            inputSchema=model.model_json_schema(),
        )
        for name, (model, _handler, description) in TOOLS.items()
    ]


async def handle_call_tool(request: types.CallToolRequest) -> types.ServerResult:
    """Validate a tool request and return the handler's result.

    Invalid arguments and unknown tools are JSON-RPC errors; handler-level
    business failures are returned as tool results with isError=True.
    """
    name = request.params.name
    arguments = request.params.arguments or {}
    tool = TOOLS.get(name)


    if tool is None:
        raise McpError(
            types.ErrorData(
                code=types.INVALID_PARAMS,
                message=f"Unknown tool: {name}",
                data={"available_tools": sorted(TOOLS)},
            )
        )

    model, handler, _description = tool
    try:
        validated_args = model.model_validate(arguments)
    except ValidationError as exc:
        errors = [
            {
                "field": ".".join(str(part) for part in error["loc"]) or "(root)",
                "message": error["msg"],
                "type": error["type"],
            }
            for error in exc.errors(
                include_url=False,
                include_context=False,
                include_input=False,
            )
        ]
        logger.warning("Invalid arguments for %s: %s", name, errors)
        raise McpError(
            types.ErrorData(
                code=types.INVALID_PARAMS,
                message=f"Invalid arguments for tool '{name}'",
                data={"errors": errors},
            )
        )

    try:
        return types.ServerResult(handler(validated_args))
    except Exception as exc:
        logger.exception("Unexpected error while running %s", name)
        raise McpError(
            types.ErrorData(
                code=types.INTERNAL_ERROR,
                message=f"Internal error while executing tool '{name}'",
            )
        ) from exc


server.request_handlers[types.CallToolRequest] = handle_call_tool


# The SDK handles normal JSON-RPC messages. This wrapper catches malformed
# input lines and unknown methods before they reach the SDK.
def known_client_methods() -> frozenset[str]:
    methods: set[str] = set()
    for request_type in get_args(types.ClientRequestType):
        methods.update(get_args(request_type.model_fields["method"].annotation))
    return frozenset(methods)


KNOWN_METHODS = known_client_methods()


class SerializedStdout:
    """Serialize writes to stdout and track requests still awaiting replies."""

    def __init__(self, stream: anyio.AsyncFile[str]) -> None:
        self.stream = stream
        self.lock = anyio.Lock()
        self.pending: set[str | int] = set()

    async def write(self, data: str) -> None:
        async with self.lock:
            await self.stream.write(data)
            await self.stream.flush()

        try:
            message = json.loads(data)
            if "result" in message or "error" in message:
                self.pending.discard(message.get("id"))
        except (ValueError, TypeError, AttributeError):
            pass


    async def flush(self) -> None:
        # write() flushes after each complete message.
        return None


    async def send_error(
        self,
        request_id: str | int | None,
        code: int,
        message: str,
    ) -> None:
        response = {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }
        await self.write(json.dumps(response) + "\n")


class GuardedStdin:
    """Filter malformed JSON-RPC lines and send protocol errors when needed."""
    def __init__(self, stream: anyio.AsyncFile[str], output: SerializedStdout) -> None:
        self.stream = stream
        self.output = output


    async def __aiter__(self):
        async for line in self.stream:
            if not line.strip():
                continue


            try:
                message = json.loads(line, parse_constant=self.reject_constant)
                types.JSONRPCMessage.model_validate(message)
                request_id = message.get("id") if isinstance(message, dict) else None
                if isinstance(request_id, bool) or not isinstance(request_id, (str, int)):
                    request_id = None
            except ValueError:
                logger.warning("Received malformed JSON")
                await self.output.send_error(None, types.PARSE_ERROR, "Parse error")
                continue
            except ValidationError:
                logger.warning("Received an invalid JSON-RPC envelope")
                await self.output.send_error(request_id, types.INVALID_REQUEST, "Invalid Request")
                continue

            
            is_request = "method" in message and "id" in message
            if is_request and message["method"] not in KNOWN_METHODS:
                logger.warning("Unknown JSON-RPC method: %s", message["method"])
                await self.output.send_error(
                    request_id,
                    types.METHOD_NOT_FOUND,
                    f"Method not found: {message['method']}",
                )
                continue

            if is_request:
                self.output.pending.add(message["id"])
            yield line


            # Let any already-dispatched request finish before the transport closes.
            with anyio.move_on_after(30):
                while self.output.pending:
                    await anyio.sleep(0.01)
            if self.output.pending:
                logger.warning("Closing with pending request IDs: %s", self.output.pending)
        

    @staticmethod
    def reject_constant(token: str) -> float:
        raise ValueError(f"{token} is not a valid JSON number")


async def main() -> None:
    output_stream = anyio.wrap_file(
        TextIOWrapper(_PROTOCOL_STDOUT.buffer, encoding="utf-8")
    )
    input_stream = anyio.wrap_file(
        TextIOWrapper(sys.stdin.buffer, encoding="utf-8", errors="replace")
    )


    output = SerializedStdout(output_stream)
    async with stdio_server(stdin=GuardedStdin(input_stream, output), stdout=output) as (
        read_stream,
        write_stream,
    ):
        logger.info("Customer support MCP server started")
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="customer-support-server",
                server_version="1.0.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


if __name__ == "__main__":
    try:
        anyio.run(main)
    except KeyboardInterrupt:
        logger.info("Server stopped")
