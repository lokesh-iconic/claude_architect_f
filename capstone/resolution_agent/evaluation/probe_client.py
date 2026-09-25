"""Drive the MCP server the way a real client does, over stdio.

Everything here goes to a separately spawned server process, so the payloads
it reports are what any MCP client (Claude Code included) would receive.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from typing import Any

from mcp import Client, StdioServerParameters

from ..tools.errors import parse_error_payload
from .results import Check
from ..config.settings import MODULE_DIR

SERVER_SCRIPT = MODULE_DIR / "server.py"
FOUR_TOOLS = ["escalate_to_human", "get_customer", "lookup_order", "process_refund"]


def server_params(extra_env: dict[str, str] | None = None) -> StdioServerParameters:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "SUPPORT_LOG_LEVEL": "CRITICAL"}
    env.update(extra_env or {})
    return StdioServerParameters(command=sys.executable, args=[str(SERVER_SCRIPT)], env=env)


@dataclass
class Call:
    tool: str
    args: dict[str, Any]
    is_error: bool
    payload: dict[str, Any]


async def call(client: Client, tool: str, args: dict[str, Any]) -> Call:
    result = await client.call_tool(tool, args)
    text = result.content[0].text if result.content else ""
    if result.is_error:
        try:
            payload = parse_error_payload(text)
        except ValueError:
            payload = {"unstructured": text}
    else:
        payload = getattr(result, "structured_content", None) or json.loads(text or "{}")
    return Call(tool, args, bool(result.is_error), payload)


VERIFY = {"email": "dana.whitfield@example.com", "postal_code": "94107"}
REFUND = {"customer_id": "C-1001", "order_id": "ORD-5521", "amount": 249.99, "currency": "USD",
          "refund_type": "full", "reason_code": "damaged_item", "customer_statement": "cracked drip tray",
          "justification": "ORD-5521 total 249.99, nothing refunded, window open until 2026-10-06."}
OVER_REFUND = {**REFUND, "amount": 300.00}


async def probe() -> dict[str, Any]:
    async with Client(server_params()) as client:
        tools = sorted(t.name for t in (await client.list_tools()).tools)
        gate = [await call(client, "process_refund", REFUND),
                await call(client, "get_customer", VERIFY),
                await call(client, "lookup_order", {"order_id": "ORD-5521"}),
                await call(client, "process_refund", OVER_REFUND),
                await call(client, "process_refund", REFUND)]
    timeout_env = {"SUPPORT_SIMULATE_TIMEOUT": "lookup_order", "SUPPORT_SIMULATE_TIMES": "2",
                   "SUPPORT_TOOL_TIMEOUT": "0.5"}
    async with Client(server_params(timeout_env)) as client:
        timeouts = [await call(client, "lookup_order", {"order_id": "ORD-5530"}) for _ in range(3)]
    return {"tools": tools, "gate": gate, "timeouts": timeouts}


def mcp_checks(result: dict[str, Any]) -> tuple[list[Check], dict[str, list[dict[str, Any]]]]:
    tools, gate, timeouts = result["tools"], result["gate"], result["timeouts"]
    blocked, verified, _, over, refunded = gate
    first, second, third = timeouts
    checks = [
        Check("Over MCP, the server lists exactly the four workflow tools", tools == FOUR_TOOLS, str(tools)),
        Check("Over MCP, process_refund before get_customer is a structured prerequisite error",
              blocked.is_error and blocked.payload.get("errorCategory") == "prerequisite"
              and blocked.payload.get("isRetryable") is False and bool(blocked.payload.get("description")),
              json.dumps(blocked.payload)[:300]),
        Check("Over MCP, a refund for more than is refundable is rejected by record validation before it runs",
              over.is_error and over.payload.get("failureType") == "invalid_action_record"
              and over.payload.get("issues", [{}])[0].get("code") == "exceeds_refundable",
              json.dumps(over.payload)[:300]),
        Check("Over MCP, the corrected refund succeeds once the customer is verified",
              verified.payload.get("verified") is True and not refunded.is_error
              and refunded.payload.get("amount") == 249.99, json.dumps(refunded.payload)),
        Check("Over MCP, a timeout is structured JSON with attempted and partialResults, not a generic error",
              first.is_error and first.payload.get("failureType") == "timeout"
              and first.payload.get("attempted") == "lookup_order(order_id='ORD-5530')"
              and "order_header" in (first.payload.get("partialResults") or {}), json.dumps(first.payload)[:300]),
        Check("Over MCP, the second timeout is isRetryable false, and the call recovers after",
              second.payload.get("isRetryable") is False and not third.is_error,
              f"second isRetryable={second.payload.get('isRetryable')!r}; third error={third.is_error}"),
    ]
    table = {"mcp_calls": [
        {"tool": c.tool, "is_error": c.is_error,
         "result": c.payload.get("failureType") or c.payload.get("refund_id") or c.payload.get("status")
         or ("verified" if c.payload.get("verified") else "")}
        for c in [*gate, *timeouts]]}
    return checks, table
