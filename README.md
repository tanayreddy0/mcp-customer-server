# Customer Support MCP Server

A Model Context Protocol (MCP) server, written in Python with the official `mcp` SDK, that exposes two tools over the **stdio** transport with **strict Pydantic validation** and **standard JSON-RPC error codes**.

| Tool | Inputs | Rules |
|---|---|---|
| `get_customer_record` | `customer_id` | Must match `^CUST-[0-9]{5}$` |
| `trigger_refund` | `customer_id`, `amount`, `reason` | `amount`: number, `0 < amount ≤ 10000`, max 2 decimals. `reason`: string, 10–500 chars after trimming |

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python server.py                     # waits for JSON-RPC on stdin (Ctrl+C to stop)
pytest -v                            # 104 tests
```

### Try it by hand

```bash
printf '%s\n' \
 '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"cli","version":"1"}}}' \
 '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
 '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"get_customer_record","arguments":{"customer_id":"cust-42"}}}' \
 | python server.py 2>/dev/null
```

`2>/dev/null` hides the logs (stderr). What remains is pure JSON-RPC:

```json
{"jsonrpc":"2.0","id":2,"error":{"code":-32602,"message":"Invalid arguments for tool 'get_customer_record'","data":{"errors":[{"field":"customer_id","message":"String should match pattern '^CUST-[0-9]{5}$'","type":"string_pattern_mismatch"}]}}}
```

### Use it from an MCP host

MCP Inspector (visual debugger, needs Node.js):

```bash
npx @modelcontextprotocol/inspector python server.py
```

Claude Desktop (`claude_desktop_config.json`), using absolute paths:

```json
{
  "mcpServers": {
    "customer-support": {
      "command": "/absolute/path/to/.venv/bin/python",
      "args": ["/absolute/path/to/server.py"]
    }
  }
}
```

Mock customers: `CUST-00001` (active), `CUST-00042` (active), `CUST-12345` (suspended).

## Design

### 1. STDIO isolation

Over stdio, the client parses stdout line by line, so any non-JSON-RPC byte breaks the connection. These layers prevent that:

1. **Logging goes to stderr.** `logging.basicConfig(stream=sys.stderr)`. The code contains no `print()` calls.
2. **`sys.stdout` is redirected to stderr at import time.** The real stdout handle is saved first and handed *only* to the transport. Any accidental `print()`, from our code or a third-party library, lands on stderr. Proven by `test_stray_print_is_redirected_to_stderr`.
3. **One serialized writer.** All protocol output goes through `SerializedStdout`, which writes whole lines under a lock, so concurrent messages can never interleave.

4. **Graceful end-of-input.** When stdin closes (for example `printf ... | python server.py`), the server finishes every in-flight request before exiting instead of dropping it. Proven by `test_piped_input_gets_every_response_before_exit`.

`test_stdout_is_pure_jsonrpc_and_logs_go_to_stderr` runs a mixed session (valid calls, invalid calls, garbage input) and asserts that every stdout line is a valid JSON-RPC 2.0 message.

### 2. Protocol compliance: error mapping

| Situation | Response | Code |
|---|---|---|
| Line is not valid JSON (including `NaN`/`Infinity`) | JSON-RPC error, `"id": null` | `-32700` Parse error |
| Valid JSON but not a JSON-RPC message | JSON-RPC error | `-32600` Invalid Request |
| Unknown method | JSON-RPC error | `-32601` Method not found |
| Unknown tool name | JSON-RPC error, `data.available_tools` | `-32602` Invalid params |
| Tool arguments fail validation | JSON-RPC error, `data.errors[]` per field | `-32602` Invalid params |
| Unexpected exception inside a tool | JSON-RPC error (no stack trace leaked; logged to stderr) | `-32603` Internal error |
| Valid input, but the operation can't proceed (customer not found, account suspended) | Normal result with `isError: true` | — |

The last row is deliberate: the MCP spec separates **protocol errors** (the request itself is wrong) from **tool execution errors** (the request was fine, the outcome wasn't). Execution errors are returned as results so the model can read the reason and react.

Two implementation notes:

- **Why not `@server.call_tool()`?** In the 1.x SDK that decorator catches *every* exception and converts it into an `isError: true` result. It never emits a JSON-RPC error. To return `-32602`, the handler is registered directly in `server.request_handlers[CallToolRequest]` and raises `McpError`, which the SDK dispatcher serializes as a JSON-RPC error.
- **Framing guard.** Out of the box, the SDK answers a malformed line with a log notification (not `-32700`) and an unknown method with `-32602` (not `-32601`). `GuardedStdin` screens each line before the SDK sees it and answers those cases correctly. It wraps the SDK's own `stdio_server` rather than replacing it.

### 3. Validation

Pydantic models are the **single source of truth**: they validate the input *and* generate the `inputSchema` returned by `tools/list`, so the advertised and enforced rules cannot drift apart.

- `strict=True`: no silent coercion. `"50"` is not `50.0`, `12345` is not `"12345"`, `true` is not a number.
- `extra="forbid"`: unknown fields such as a misspelled `customerId` are rejected.
- `[0-9]` instead of `\d`: `\d` matches any Unicode digit, so `CUST-٠٠٠٤٢` would otherwise pass.
- `amount`: `gt=0`, `le=10000`, `allow_inf_nan=False`, at most 2 decimal places (tolerant of float noise such as `0.1 + 0.2`).
- `reason`: whitespace is stripped *before* the length check, so 10 spaces fail.
- All failing fields are reported in one response, not just the first.

## Project layout

```
server.py                     the MCP server
tests/test_validation.py      Pydantic edge cases (no process)
tests/test_protocol.py        raw JSON-RPC against a real subprocess
tests/test_official_client.py interop with the official MCP Python client
requirements.txt
pytest.ini
```

## Assumptions

- `XXXXX` in `CUST-XXXXX` means exactly five ASCII digits.
- The `10000` refund cap and 2-decimal rule are business guardrails added on top of "positive float"; change `MAX_REFUND_AMOUNT` to adjust.
- Data is an in-memory mock; refunds reset on restart.
