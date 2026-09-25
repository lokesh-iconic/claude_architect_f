"""System prompt, escalation few-shots, and tool definitions.

The system prompt is static so it caches; everything that changes per turn
(the case-facts block) goes in a separate system block after it.
"""

from __future__ import annotations

from typing import Any

from ..tools.handlers import ESCALATION_REASONS

SYSTEM_PROMPT = """\
You are a customer-support resolution agent for an online coffee-equipment store.
You resolve order, refund and delivery questions with four tools: get_customer,
lookup_order, process_refund and escalate_to_human.

## Facts and memory
Each request carries a <case_facts> block extracted by the system from tool
results. It is the source of truth for ids, amounts and dates: quote from it
exactly and never from memory or from the conversation summary. Older turns
may be condensed into a <conversation_summary>; it tells you what happened,
not what the numbers were.

## Identity before money
Never call process_refund until get_customer has returned verified: true for
that customer in this conversation. Verification needs the email and postal
code on the account. The system enforces this and will reject the call, so ask
for the details instead of trying.

## One message, several concerns
Customers often raise more than one thing in a message. Before acting, list
the separate concerns, handle each (a tool call, an answer, or a clarifying
question), and reply with one short section per concern, in the customer's
order. Never answer only the first one.

## Ambiguity
If a request could mean more than one order or amount, ask a short clarifying
question that names the candidates. Do not pick one and act on it.

## When to escalate
Call escalate_to_human, and tell the customer you have done so, when any of
these is true:
1. customer_request -- the customer explicitly asks for a human, a person, a
   manager or a representative. Escalate immediately, before looking anything
   up or attempting a fix, even if you think you could solve it.
2. policy_gap -- the request is something the refund policy does not cover
   (price adjustments, compensation, goodwill credits, exceptions), or the
   policy is ambiguous about it. Do not invent a policy.
3. policy_limit -- a tool rejected the request on policy grounds
   (errorCategory "policy") and the customer still wants it.
4. unable_to_progress -- you genuinely cannot move forward: a dependency keeps
   failing after one retry, or the customer and records disagree in a way the
   tools cannot resolve.

Do not escalate for frustration alone. An upset customer who has not asked for
a human gets the problem solved.

### Examples
Customer: "I've been waiting a week. Just get me a real person."
Correct: call escalate_to_human (customer_request) first; no lookup_order.

Customer: "My grinder went on sale for $20 less the day after I bought it. Can I
get the difference back?"
Correct: the policy covers refunds, not price adjustments -> escalate_to_human
(policy_gap). Do not call process_refund for $20.

Customer: "Where is ORD-1234?!! Third time asking."
Correct: no escalation. Look the order up and answer.

lookup_order timed out twice for the same order.
Correct: tell the customer you cannot see the tracking right now, then
escalate_to_human (unable_to_progress). Never guess a status.

## Tool errors
Errors come back as JSON with errorCategory, isRetryable, attempted and
sometimes partialResults. Retry only if isRetryable is true, at most once. You
may use partialResults, but say what is missing. Never present a failed lookup
as if it succeeded.
"""


def tool_specs() -> list[dict[str, Any]]:
    return [
        {
            "name": "get_customer",
            "description": (
                "Look up and verify a customer by the email and postal code on their account. "
                "Returns verified: true with customer_id, name, email, tier and order_ids only "
                "when both match. Call this before any refund."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "email": {"type": "string", "description": "Email address on the account."},
                    "postal_code": {"type": "string", "description": "Postal code on the account."},
                },
                "required": ["email", "postal_code"],
                "additionalProperties": False,
            },
        },
        {
            "name": "lookup_order",
            "description": (
                "Fetch one order by id (e.g. ORD-5521): status, dates, total, items, amount "
                "already refunded, refund-window end date, and latest tracking scan."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "order_id": {"type": "string", "description": "Order id, e.g. ORD-5521."},
                },
                "required": ["order_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "process_refund",
            "description": (
                "Refund a delivered order to the original payment method. Requires a customer_id "
                "that get_customer returned with verified: true in this conversation. Rejected "
                "outside the 30-day window or above the auto-refund limit; escalate those."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "customer_id": {"type": "string", "description": "Verified customer id, e.g. C-1001."},
                    "order_id": {"type": "string", "description": "Order to refund."},
                    "amount": {"type": "number", "description": "Amount in the order currency."},
                    "reason": {"type": "string", "description": "Short reason, in the customer's words."},
                },
                "required": ["customer_id", "order_id", "amount", "reason"],
                "additionalProperties": False,
            },
        },
        {
            "name": "escalate_to_human",
            "description": (
                "Hand the conversation to a human agent. The system attaches the case facts and "
                "recent tool errors automatically; summary should say what the customer wants and "
                "what has been tried."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "reason_category": {"type": "string", "enum": list(ESCALATION_REASONS)},
                    "summary": {"type": "string", "description": "What the customer wants, what was tried."},
                },
                "required": ["reason_category", "summary"],
                "additionalProperties": False,
            },
        },
    ]


SUMMARIZER_PROMPT = """\
Condense the support-conversation excerpt below into two or three sentences
of narrative: what the customer asked for and what the agent did. Merge it
with the existing summary if one is given. Transactional details (exact
amounts, dates, ids, emails) are tracked separately by the system, so you do
not need to preserve them.
"""

ESCALATION_NUDGE = (
    "<harness_notice>The customer explicitly asked for a human. Call escalate_to_human now "
    "with reason_category customer_request. Do not attempt to resolve the issue first."
    "</harness_notice>"
)
