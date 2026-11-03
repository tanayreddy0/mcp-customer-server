"""End-to-end tests: launch server.py as a real subprocess and speak raw JSON-RPC.

These tests prove the three evaluation criteria directly:
  * STDIO isolation   - every byte on stdout is a JSON-RPC 2.0 message
  * Protocol          - correct error codes for every failure class
  * Validation        - malformed tool arguments are rejected with field details
"""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server.py"

PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS = -32700, -32600, -32601, -32602


class RawClient:
    """Minimal JSON-RPC-over-stdio client that records every raw stdout line."""

    def __init__(self) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, str(SERVER)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", bufsize=1,
        )
        self.raw_stdout: list[str] = []
        self._messages: queue.Queue[dict] = queue.Queue()
        self._stderr: list[str] = []
        threading.Thread(target=self._pump_stdout, daemon=True).start()
        threading.Thread(target=lambda: self._stderr.extend(self.proc.stderr), daemon=True).start()
        self._next_id = 100

    def _pump_stdout(self) -> None:
        for line in self.proc.stdout:
            self.raw_stdout.append(line)
            try:
                self._messages.put(json.loads(line))
            except json.JSONDecodeError:
                self._messages.put({"__unparseable__": line})

    def send_raw(self, line: str) -> None:
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()

    def send(self, msg: dict) -> None:
        self.send_raw(json.dumps(msg))

    def wait_for(self, match, timeout: float = 10.0) -> dict:
        """Return the first incoming message for which match(msg) is true."""
        while True:
            msg = self._messages.get(timeout=timeout)
            if match(msg):
                return msg

    def request(self, method: str, params: dict | None = None) -> dict:
        self._next_id += 1
        rid = self._next_id
        msg = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        self.send(msg)
        return self.wait_for(lambda m: m.get("id") == rid)

    def call_tool(self, name: str, arguments) -> dict:
        return self.request("tools/call", {"name": name, "arguments": arguments})

    def initialize(self) -> dict:
        resp = self.request("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "1.0"},
        })
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return resp

    def close(self) -> str:
        self.proc.stdin.close()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        return "".join(self._stderr)


@pytest.fixture
def client():
    c = RawClient()
    c.initialize()
    yield c
    c.close()


def assert_error(resp: dict, code: int) -> dict:
    assert "result" not in resp, resp
    assert resp["error"]["code"] == code, resp
    assert isinstance(resp["error"]["message"], str) and resp["error"]["message"]
    return resp["error"]


# =============================== STDIO ISOLATION ===========================

def test_stdout_is_pure_jsonrpc_and_logs_go_to_stderr():
    c = RawClient()
    c.initialize()
    c.request("tools/list")
    c.call_tool("get_customer_record", {"customer_id": "CUST-00042"})
    c.call_tool("get_customer_record", {"customer_id": "bad"})
    c.call_tool("trigger_refund", {"customer_id": "CUST-00042", "amount": 10, "reason": "Damaged on arrival"})
    c.call_tool("trigger_refund", {"customer_id": "CUST-00042", "amount": -1, "reason": "x"})
    c.call_tool("no_such_tool", {})
    c.send_raw("this is not json")
    c.wait_for(lambda m: m.get("error", {}).get("code") == PARSE_ERROR)
    c.request("no/such/method")
    stderr = c.close()

    assert c.raw_stdout, "server produced no output"
    for line in c.raw_stdout:
        assert line.endswith("\n")
        msg = json.loads(line)                       # every line is valid JSON...
        assert isinstance(msg, dict)
        assert msg.get("jsonrpc") == "2.0"           # ...and a JSON-RPC 2.0 message
        if "id" in msg:                              # a response: exactly one of result/error
            assert ("result" in msg) != ("error" in msg), msg
        else:                                        # otherwise a notification
            assert "method" in msg, msg

    assert "customer-support-server started on stdio" in stderr
    assert "Rejected trigger_refund call" in stderr
    assert "started on stdio" not in "".join(c.raw_stdout)


def test_stray_print_is_redirected_to_stderr():
    """Layer-2 guard: once server.py is imported, print() cannot reach stdout."""
    code = "import server; print('ACCIDENTAL DEBUG PRINT'); print('also this', flush=True)"
    proc = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""
    assert "ACCIDENTAL DEBUG PRINT" in proc.stderr


# =============================== PROTOCOL: HAPPY PATH ======================

def test_initialize_handshake():
    c = RawClient()
    resp = c.initialize()
    c.close()
    result = resp["result"]
    assert result["serverInfo"]["name"] == "customer-support-server"
    assert "tools" in result["capabilities"]


def test_tools_list_exposes_both_tools_with_strict_schemas(client):
    tools = {t["name"]: t for t in client.request("tools/list")["result"]["tools"]}
    assert set(tools) == {"get_customer_record", "trigger_refund"}

    s1 = tools["get_customer_record"]["inputSchema"]
    assert s1["required"] == ["customer_id"]
    assert s1["additionalProperties"] is False
    assert s1["properties"]["customer_id"]["pattern"] == "^CUST-[0-9]{5}$"

    s2 = tools["trigger_refund"]["inputSchema"]
    assert set(s2["required"]) == {"customer_id", "amount", "reason"}
    assert s2["properties"]["amount"]["exclusiveMinimum"] == 0
    assert s2["properties"]["reason"]["minLength"] == 10


def test_get_customer_record_success(client):
    result = client.call_tool("get_customer_record", {"customer_id": "CUST-00042"})["result"]
    assert result["isError"] is False
    assert result["structuredContent"]["name"] == "Alan Turing"
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]


def test_trigger_refund_success(client):
    result = client.call_tool("trigger_refund", {
        "customer_id": "CUST-00001", "amount": 49.99, "reason": "  Item arrived damaged  ",
    })["result"]
    assert result["isError"] is False
    refund = result["structuredContent"]
    assert refund["refund_id"].startswith("RFD-")
    assert refund["amount"] == 49.99
    assert refund["reason"] == "Item arrived damaged"   # whitespace stripped


# ======================= PROTOCOL: BUSINESS ERRORS (isError) ===============
# Valid input, but the operation can't be completed -> a normal result with
# isError=true (so the LLM can read the reason), NOT a JSON-RPC error.

def test_unknown_customer_is_tool_error_not_protocol_error(client):
    resp = client.call_tool("get_customer_record", {"customer_id": "CUST-77777"})
    assert "error" not in resp
    assert resp["result"]["isError"] is True
    assert "CUST-77777" in resp["result"]["content"][0]["text"]


def test_refund_for_suspended_customer_is_tool_error(client):
    resp = client.call_tool("trigger_refund", {
        "customer_id": "CUST-12345", "amount": 5, "reason": "Goodwill gesture refund",
    })
    assert resp["result"]["isError"] is True
    assert "suspended" in resp["result"]["content"][0]["text"]


# ================== VALIDATION: INVALID ARGUMENTS -> -32602 ================

@pytest.mark.parametrize("arguments, bad_field", [
    ({"customer_id": "cust-00042"}, "customer_id"),
    ({"customer_id": "CUST-123"}, "customer_id"),
    ({"customer_id": "CUST-123456"}, "customer_id"),
    ({"customer_id": "CUST-٠٠٠٤٢"}, "customer_id"),
    ({"customer_id": 12345}, "customer_id"),
    ({"customer_id": None}, "customer_id"),
    ({}, "customer_id"),
    ({"customer_id": "CUST-00042", "extra": 1}, "extra"),
])
def test_get_customer_record_rejects_bad_input(client, arguments, bad_field):
    err = assert_error(client.call_tool("get_customer_record", arguments), INVALID_PARAMS)
    assert bad_field in [e["field"] for e in err["data"]["errors"]]


GOOD = {"customer_id": "CUST-00042", "amount": 25.5, "reason": "Late delivery refund"}


@pytest.mark.parametrize("override, bad_field", [
    ({"amount": 0}, "amount"),
    ({"amount": -10}, "amount"),
    ({"amount": "25.50"}, "amount"),
    ({"amount": True}, "amount"),
    ({"amount": 10.999}, "amount"),
    ({"amount": 1_000_000}, "amount"),
    ({"amount": None}, "amount"),
    ({"reason": "too short"}, "reason"),
    ({"reason": "          "}, "reason"),
    ({"reason": 1234567890}, "reason"),
    ({"customer_id": "CUST-1"}, "customer_id"),
])
def test_trigger_refund_rejects_bad_input(client, override, bad_field):
    err = assert_error(client.call_tool("trigger_refund", {**GOOD, **override}), INVALID_PARAMS)
    assert bad_field in [e["field"] for e in err["data"]["errors"]]


@pytest.mark.parametrize("field", ["customer_id", "amount", "reason"])
def test_trigger_refund_missing_field(client, field):
    args = {k: v for k, v in GOOD.items() if k != field}
    err = assert_error(client.call_tool("trigger_refund", args), INVALID_PARAMS)
    assert [e["field"] for e in err["data"]["errors"]] == [field]


@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity"])
def test_nan_and_infinity_are_parse_errors(client, token):
    # Python's json module emits these tokens, but they are not valid JSON (RFC 8259).
    line = ('{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"trigger_refund",'
            '"arguments":{"customer_id":"CUST-00042","amount":%s,"reason":"Late delivery refund"}}}') % token
    client.send_raw(line)
    resp = client.wait_for(lambda m: "error" in m)
    assert resp["error"]["code"] == PARSE_ERROR
    assert resp["id"] is None


def test_all_invalid_fields_reported_together(client):
    err = assert_error(
        client.call_tool("trigger_refund", {"customer_id": "x", "amount": -1, "reason": "no"}),
        INVALID_PARAMS,
    )
    assert {e["field"] for e in err["data"]["errors"]} == {"customer_id", "amount", "reason"}


def test_unknown_tool(client):
    err = assert_error(client.call_tool("delete_everything", {}), INVALID_PARAMS)
    assert err["data"]["available_tools"] == ["get_customer_record", "trigger_refund"]


def test_arguments_not_an_object(client):
    assert_error(client.call_tool("get_customer_record", "CUST-00042"), INVALID_PARAMS)


def test_tools_call_without_params(client):
    assert_error(client.request("tools/call"), INVALID_PARAMS)


# ===================== PROTOCOL: FRAMING / ENVELOPE ERRORS =================

def test_parse_error_has_null_id(client):
    client.send_raw('{"jsonrpc": "2.0", "id": 1, "method": "tools/list"')  # truncated JSON
    resp = client.wait_for(lambda m: "error" in m and m["error"]["code"] == PARSE_ERROR)
    assert resp["id"] is None


@pytest.mark.parametrize("payload, expected_id", [
    ('{"id": 7, "method": "tools/list"}', 7),                    # missing "jsonrpc"
    ('{"jsonrpc": "1.0", "id": 8, "method": "tools/list"}', 8),  # wrong version
    ('{"jsonrpc": "2.0", "id": 9}', 9),                          # no method/result/error
    ('[1, 2, 3]', None),                                         # not an object
    ('"just a string"', None),
])
def test_invalid_request_envelope(client, payload, expected_id):
    client.send_raw(payload)
    resp = client.wait_for(lambda m: "error" in m and m["error"]["code"] == INVALID_REQUEST)
    assert resp["id"] == expected_id


def test_unknown_method(client):
    err = assert_error(client.request("customers/delete_all"), METHOD_NOT_FOUND)
    assert "customers/delete_all" in err["message"]


def test_server_keeps_working_after_errors(client):
    client.send_raw("garbage")
    client.send_raw("{]")
    client.call_tool("trigger_refund", {"amount": "lots"})
    client.request("nope")
    ok = client.call_tool("get_customer_record", {"customer_id": "CUST-00001"})
    assert ok["result"]["isError"] is False


def test_piped_input_gets_every_response_before_exit():
    """Write all requests at once, close stdin immediately: no response may be dropped."""
    lines = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "pipe", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "get_customer_record", "arguments": {"customer_id": "CUST-00042"}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "get_customer_record", "arguments": {"customer_id": "cust-42"}}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
         "params": {"name": "trigger_refund",
                    "arguments": {"customer_id": "CUST-00001", "amount": 9.99, "reason": "Wrong size shipped"}}},
    ]
    stdin = "\n".join(json.dumps(m) for m in lines) + "\n"
    proc = subprocess.run([sys.executable, str(SERVER)], input=stdin, capture_output=True, text=True, timeout=30)
    responses = {json.loads(l)["id"]: json.loads(l) for l in proc.stdout.splitlines()}
    assert set(responses) == {1, 2, 3, 4, 5}
    assert responses[4]["error"]["code"] == INVALID_PARAMS
    assert responses[5]["result"]["isError"] is False
    assert proc.returncode == 0
