"""MCP server exposing the four support tools over stdio.

A thin adapter: every tool delegates to one `ToolSession` per server process.
stdio is one client per process, so that session *is* the conversation, and
the refund prerequisite, deadlines, retry budget and trimming all hold for any
MCP client -- including one that never went through `agent.py`.

Failures raise the SDK's `ToolError` with our JSON payload as the message.
Any other exception type is replaced by the SDK with a generic
"Error executing tool <name>" string, which would lose `errorCategory`,
`isRetryable`, `attempted` and `partialResults`.
"""

from __future__ import annotations

import os
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from .handlers import Faults
from .prompts import tool_specs
from .session import ToolSession

INSTRUCTIONS = """\
Customer-support tools for order, refund and delivery questions.

process_refund is rejected unless get_customer has returned verified: true
for that customer_id in this session. Every failure is JSON with
errorCategory, failureType, isRetryable, attempted and, where some work
finished before the failure, partialResults. Retry only when isRetryable is
true.
"""

_DESCRIPTIONS = {spec["name"]: spec["description"] for spec in tool_specs()}


def session_from_env() -> ToolSession:
    """Fault injection for exercising the timeout path from any MCP client."""
    tool = (os.getenv("SUPPORT_SIMULATE_TIMEOUT") or "").strip()
    times = int(os.getenv("SUPPORT_SIMULATE_TIMES") or "1")
    timeout = float(os.getenv("SUPPORT_TOOL_TIMEOUT") or "3")
    faults = Faults(timeouts={tool: times} if tool else {}, hang_s=30.0)
    return ToolSession(tool_timeout_s=timeout, faults=faults)


def build_server(session: ToolSession | None = None) -> MCPServer:
    session = session or session_from_env()
    server = MCPServer(
        name="support-desk",
        version="1.0.0",
        instructions=INSTRUCTIONS,
        log_level=os.getenv("SUPPORT_LOG_LEVEL", "INFO"),  # type: ignore[arg-type]
    )

    def run(tool: str, args: dict[str, Any]) -> dict[str, Any]:
        outcome = session.call(tool, args)
        if outcome.is_error:
            raise ToolError(outcome.content)
        return outcome.payload

    @server.tool(name="get_customer", description=_DESCRIPTIONS["get_customer"])
    def get_customer(
        email: Annotated[str, Field(description="Email address on the account.", min_length=3)],
        postal_code: Annotated[str, Field(description="Five-digit postal code on the account.",
                                          pattern=r"^\d{5}$")],
    ) -> dict[str, Any]:
        return run("get_customer", {"email": email, "postal_code": postal_code})

    @server.tool(name="lookup_order", description=_DESCRIPTIONS["lookup_order"])
    def lookup_order(
        order_id: Annotated[str, Field(description="Order id, e.g. 'ORD-5521'.", pattern=r"^ORD-\d{4}$")],
    ) -> dict[str, Any]:
        return run("lookup_order", {"order_id": order_id})

    @server.tool(name="process_refund", description=_DESCRIPTIONS["process_refund"])
    def process_refund(
        customer_id: Annotated[str, Field(description="Verified customer id, e.g. 'C-1001'.",
                                          pattern=r"^C-\d{4}$")],
        order_id: Annotated[str, Field(description="Order to refund.", pattern=r"^ORD-\d{4}$")],
        amount: Annotated[float, Field(description="Amount in the order currency.", gt=0)],
        reason: Annotated[str, Field(description="Short reason, in the customer's words.", min_length=1)],
    ) -> dict[str, Any]:
        return run("process_refund", {"customer_id": customer_id, "order_id": order_id,
                                      "amount": amount, "reason": reason})

    @server.tool(name="escalate_to_human", description=_DESCRIPTIONS["escalate_to_human"])
    def escalate_to_human(
        reason_category: Annotated[
            Literal["customer_request", "policy_gap", "policy_limit", "unable_to_progress"],
            Field(description="Which escalation criterion applies."),
        ],
        summary: Annotated[str, Field(description="What the customer wants, what was tried.", min_length=1)],
    ) -> dict[str, Any]:
        return run("escalate_to_human", {"reason_category": reason_category, "summary": summary})

    return server


def main() -> None:
    build_server().run("stdio")
