"""Action records: semantic validation against the case facts, and the ledger.

`schema.validate` checks that an action record has the right shape. This
module checks that it is *true*: a refund is grounded in an order fetched in
this conversation, for no more than is refundable, with a `refund_type` that
matches the amount; a handoff names the customer the session actually
verified, and gives a root cause and next step a person can act on instead
of "please review".

Everything here reads `CaseFacts` -- the same program-extracted facts the
model sees -- so the validator and the model are judged against one source
of truth.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..conversation.facts import CaseFacts
from .schema import Issue

TOLERANCE = 0.005

_GENERIC = re.compile(
    r"^\s*(?:please\s+)?(?:review|check|look\s+into|investigate|handle|help|assist|follow\s+up|escalate)"
    r"(?:\s+(?:this|it|the|a|case|customer|issue|request|ticket|asap|please|for|me|us))*\s*[.!]*\s*$"
    r"|^\s*(?:n/?a|tbd|none|unknown|see\s+(?:above|transcript|chat)|customer\s+needs\s+help)\s*[.!]*\s*$",
    re.I,
)
MIN_WORDS = 4


def generic_text(text: str) -> bool:
    return bool(_GENERIC.match(text or "")) or len((text or "").split()) < MIN_WORDS


def _check_amount(amount: float, refund_type: Any, total: float, refunded: float) -> list[Issue]:
    refundable = round(total - refunded, 2)
    if amount > refundable + TOLERANCE:
        return [Issue("amount", "exceeds_refundable",
                      f"{amount:.2f} is more than the refundable {refundable:.2f} "
                      f"(total {total:.2f} less {refunded:.2f} already refunded)")]
    whole = abs(amount - refundable) <= TOLERANCE
    if refund_type == "full" and not whole:
        return [Issue("refund_type", "type_amount_mismatch",
                      f"refund_type is full but {amount:.2f} is not the whole refundable {refundable:.2f}; "
                      "use partial, or refund the whole remainder")]
    if refund_type == "partial" and whole:
        return [Issue("refund_type", "type_amount_mismatch",
                      f"{amount:.2f} is the whole refundable amount, so refund_type must be full")]
    return []


def check_refund(args: dict[str, Any], facts: CaseFacts) -> list[Issue]:
    order_id = str(args.get("order_id", "")).upper()
    order = facts.orders.get(order_id)
    if order is None:
        return [Issue("order_id", "not_in_case_facts",
                      f"{order_id} has not been fetched in this conversation; call lookup_order first so the "
                      "refund is grounded in the order record")]
    issues: list[Issue] = []
    if args.get("currency") != order.currency:
        issues.append(Issue("currency", "currency_mismatch",
                            f"{order_id} is in {order.currency}, not {args.get('currency')!r}"))
    if "amount" in args:
        issues += _check_amount(float(args["amount"]), args.get("refund_type"), order.total, order.refunded_amount)
    if generic_text(str(args.get("justification", ""))):
        issues.append(Issue("justification", "generic_text",
                            "say why this amount is right under the policy, citing the case facts"))
    return issues


def check_handoff(args: dict[str, Any], facts: CaseFacts) -> list[Issue]:
    issues: list[Issue] = []
    verified = facts.customer.customer_id if facts.customer and facts.customer.verified else None
    if verified and args.get("customer_id") != verified:
        issues.append(Issue("customer_id", "not_the_verified_customer",
                            f"the verified customer in this conversation is {verified}"))
    if not verified and args.get("customer_id") is not None:
        issues.append(Issue("customer_id", "unverified_id",
                            "no customer is verified in this conversation, so customer_id must be null"))
    if args.get("identity_verified") is not bool(verified):
        issues.append(Issue("identity_verified", "contradicts_case_facts",
                            f"identity_verified must be {str(bool(verified)).lower()}"))
    for name in ("customer_request", "root_cause", "recommended_action"):
        if generic_text(str(args.get(name, ""))):
            issues.append(Issue(name, "generic_text",
                                f"{name} must be specific enough for a person to act on without reading "
                                "the transcript (at least a sentence; not 'please review')"))
    return issues


SEMANTIC_CHECKS = {"process_refund": check_refund, "escalate_to_human": check_handoff}


@dataclass
class ActionRecord:
    """One money- or case-state action that passed validation and reached its handler."""

    action_id: str
    tool: str
    turn: int
    authored_by: str
    record: dict[str, Any]
    validation_attempts: int
    rejected_attempts: list[list[dict[str, str]]] = field(default_factory=list)
    status: str = "executed"
    result_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)
