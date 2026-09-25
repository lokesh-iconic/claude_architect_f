"""MCP server exposing exactly the four support tools over stdio.

A thin adapter: every tool delegates to one `ToolSession` per server process.
stdio is one client per process, so that session *is* the conversation, and
the refund prerequisite, action-record validation, deadlines, retry budgets,
trimming and the ledger all hold for any MCP client -- including one that
never went through `agent.py`.

Arguments carry the same constraints as `schema.py`, so the SDK rejects a
malformed call before the handler runs. Failures raise the SDK's `ToolError`
with our JSON payload as the message; any other exception type is replaced
by the SDK with a generic "Error executing tool <name>" string.
"""

from __future__ import annotations

import os
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from .handlers import Faults
from .schema import DESCRIPTIONS, REFUND_REASON_CODES, REFUND_TYPES
from .session import ToolSession

INSTRUCTIONS = """\
Customer-support tools for returns, billing questions and order problems.

process_refund is rejected unless get_customer has returned verified: true
for that customer_id in this session. process_refund and escalate_to_human
take a full action record and are validated against the case facts before
anything runs. Every failure is JSON with errorCategory, isRetryable,
description and attempted. Retry only when isRetryable is true.
"""

CUSTOMER_ID = r"^C-\d{4}$"
ORDER_ID = r"^ORD-\d{4}$"


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
        name="support-resolution",
        version="1.0.0",
        instructions=INSTRUCTIONS,
        log_level=os.getenv("SUPPORT_LOG_LEVEL", "INFO"),  # type: ignore[arg-type]
    )

    def run(tool: str, args: dict[str, Any]) -> dict[str, Any]:
        outcome = session.call(tool, args)
        if outcome.is_error:
            raise ToolError(outcome.content)
        return outcome.payload

    @server.tool(name="get_customer", description=DESCRIPTIONS["get_customer"])
    def get_customer(
        email: Annotated[str, Field(description="Email address on the account.", min_length=3)],
        postal_code: Annotated[str, Field(description="Five-digit postal code on the account.",
                                          pattern=r"^\d{5}$")],
    ) -> dict[str, Any]:
        return run("get_customer", {"email": email, "postal_code": postal_code})

    @server.tool(name="lookup_order", description=DESCRIPTIONS["lookup_order"])
    def lookup_order(
        order_id: Annotated[str, Field(description="Order number, e.g. 'ORD-5521'.", pattern=ORDER_ID)],
    ) -> dict[str, Any]:
        return run("lookup_order", {"order_id": order_id})

    @server.tool(name="process_refund", description=DESCRIPTIONS["process_refund"])
    def process_refund(
        customer_id: Annotated[str, Field(description="Verified customer id, e.g. 'C-1001'.", pattern=CUSTOMER_ID)],
        order_id: Annotated[str, Field(description="Order to refund, already fetched.", pattern=ORDER_ID)],
        amount: Annotated[float, Field(description="Amount to return, at most the refundable remainder.", gt=0)],
        currency: Annotated[Literal["USD"], Field(description="The order's currency.")],
        refund_type: Annotated[Literal[tuple(REFUND_TYPES)], Field(  # type: ignore[valid-type]
            description="full if amount is the whole refundable remainder, otherwise partial.")],
        reason_code: Annotated[Literal[tuple(REFUND_REASON_CODES)], Field(  # type: ignore[valid-type]
            description="Why the customer is owed money back.")],
        customer_statement: Annotated[str, Field(description="The customer's words about the problem.",
                                                 min_length=3)],
        justification: Annotated[str, Field(description="Why this amount is right under the policy, citing "
                                                        "the case facts.", min_length=20)],
    ) -> dict[str, Any]:
        return run("process_refund", {
            "customer_id": customer_id, "order_id": order_id, "amount": amount, "currency": currency,
            "refund_type": refund_type, "reason_code": reason_code,
            "customer_statement": customer_statement, "justification": justification,
        })

    @server.tool(name="escalate_to_human", description=DESCRIPTIONS["escalate_to_human"])
    def escalate_to_human(
        reason_category: Annotated[
            Literal["customer_request", "policy_gap", "policy_limit", "unable_to_progress"],
            Field(description="Which escalation criterion applies."),
        ],
        customer_id: Annotated[str | None, Field(description="Verified customer id, or null if none.",
                                                 pattern=CUSTOMER_ID)],
        identity_verified: Annotated[bool, Field(description="Whether get_customer verified the customer.")],
        order_ids: Annotated[list[Annotated[str, Field(pattern=ORDER_ID)]],
                             Field(description="Orders the case is about.")],
        customer_request: Annotated[str, Field(description="What the customer wants.", min_length=10)],
        root_cause: Annotated[str, Field(description="Why it can't be resolved here.", min_length=20)],
        recommended_action: Annotated[str, Field(description="The concrete next step for the human.",
                                                 min_length=20)],
        actions_taken: Annotated[list[str], Field(description="What was already done.")],
    ) -> dict[str, Any]:
        return run("escalate_to_human", {
            "reason_category": reason_category, "customer_id": customer_id,
            "identity_verified": identity_verified, "order_ids": order_ids,
            "customer_request": customer_request, "root_cause": root_cause,
            "recommended_action": recommended_action, "actions_taken": actions_taken,
        })

    return server


def main() -> None:
    build_server().run("stdio")
