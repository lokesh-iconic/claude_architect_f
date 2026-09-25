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
from .scenarios import Check, SuiteResult
from ..config.settings import MODULE_DIR

SERVER_SCRIPT = MODULE_DIR / "server.py"


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


async def _call(client: Client, tool: str, args: dict[str, Any]) -> Call:
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


REFUND = {"customer_id": "C-1001", "order_id": "ORD-5521", "amount": 249.99, "reason": "cracked drip tray"}
VERIFY = {"email": "dana.whitfield@example.com", "postal_code": "94107"}


async def probe() -> tuple[list[str], list[Call], list[Call]]:
    async with Client(server_params()) as client:
        tools = sorted(t.name for t in (await client.list_tools()).tools)
        gate = [await _call(client, "process_refund", REFUND),
                await _call(client, "get_customer", VERIFY),
                await _call(client, "process_refund", REFUND)]
    timeout_env = {"SUPPORT_SIMULATE_TIMEOUT": "lookup_order", "SUPPORT_SIMULATE_TIMES": "2",
                   "SUPPORT_TOOL_TIMEOUT": "0.5"}
    async with Client(server_params(timeout_env)) as client:
        order = {"order_id": "ORD-5530"}
        timeouts = [await _call(client, "lookup_order", order) for _ in range(3)]
    return tools, gate, timeouts


async def run_mcp() -> SuiteResult:
    suite = SuiteResult("mcp")
    tools, gate, timeouts = await probe()
    suite.checks.append(Check(
        "The server lists exactly the four support tools",
        tools == ["escalate_to_human", "get_customer", "lookup_order", "process_refund"], str(tools)))
    blocked, verified, refunded = gate
    suite.checks.append(Check(
        "Over MCP, process_refund before get_customer comes back isError with a prerequisite payload",
        blocked.is_error and blocked.payload.get("errorCategory") == "prerequisite"
        and blocked.payload.get("failureType") == "identity_not_verified",
        json.dumps(blocked.payload)[:300]))
    suite.checks.append(Check(
        "Over MCP, the same refund succeeds once get_customer has verified that customer",
        not verified.is_error and verified.payload.get("verified") is True
        and not refunded.is_error and refunded.payload.get("amount") == 249.99,
        json.dumps(refunded.payload)))
    first, second, third = timeouts
    suite.checks.append(Check(
        "Over MCP, a timeout arrives as structured JSON with attempted and partialResults, not a generic error",
        first.is_error and first.payload.get("failureType") == "timeout"
        and first.payload.get("attempted") == "lookup_order(order_id='ORD-5530')"
        and "order_header" in (first.payload.get("partialResults") or {}),
        json.dumps(first.payload)[:400]))
    suite.checks.append(Check(
        "Over MCP, the retry budget flips isRetryable to false on the second timeout, and recovery works after",
        second.is_error and second.payload.get("isRetryable") is False and not third.is_error,
        f"second isRetryable={second.payload.get('isRetryable')!r}; third call error={third.is_error}"))
    suite.tables["calls"] = [
        {"tool": c.tool, "is_error": c.is_error,
         "result": c.payload.get("failureType") or c.payload.get("refund_id") or c.payload.get("status")
         or ("verified" if c.payload.get("verified") else "")}
        for c in [*gate, *timeouts]
    ]
    suite.tables["timeout_payload"] = [first.payload]
    return suite
