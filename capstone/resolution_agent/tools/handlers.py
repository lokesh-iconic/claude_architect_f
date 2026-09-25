"""Tool implementations, transport-agnostic.

Each handler returns the *raw* upstream record. Trimming, the refund
prerequisite, deadlines and case-fact extraction all live one layer up in
`session.py`, so they apply identically whether the call arrives from the
in-process agent or over MCP stdio.

Handlers report work in progress to a `Progress` object. When a call blows
its deadline, that is where `partialResults` comes from -- the session can
say "the order header came back, the shipment lookup did not" instead of
just "timed out".
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from . import data
from .errors import SupportToolError, describe_call

TOOL_NAMES = ("get_customer", "lookup_order", "process_refund", "escalate_to_human")
ESCALATION_REASONS = ("customer_request", "policy_gap", "policy_limit", "unable_to_progress")


@dataclass
class Faults:
    """Deterministic failure injection: hang the next N calls to a tool."""

    timeouts: dict[str, int] = field(default_factory=dict)
    hang_s: float = 30.0

    def consume_timeout(self, tool: str) -> bool:
        left = self.timeouts.get(tool, 0)
        if left <= 0:
            return False
        self.timeouts[tool] = left - 1
        return True


@dataclass
class Progress:
    steps: list[tuple[str, Any]] = field(default_factory=list)

    def record(self, step: str, value: Any) -> None:
        self.steps.append((step, value))


@dataclass
class UpstreamState:
    """Mutable per-session state: refunds issued, tickets opened, calls made."""

    refunded: dict[str, float] = field(default_factory=lambda: {
        oid: o.refunded for oid, o in data.ORDERS.items()
    })
    refunds: list[dict[str, Any]] = field(default_factory=list)
    tickets: list[dict[str, Any]] = field(default_factory=list)
    invocations: dict[str, int] = field(default_factory=lambda: {t: 0 for t in TOOL_NAMES})


class _Cancelled(Exception):
    pass


def _hang(faults: Faults, tool: str, cancel: threading.Event) -> None:
    if faults.consume_timeout(tool):
        cancel.wait(faults.hang_s)
        raise _Cancelled(tool)


def get_customer(args: dict[str, Any], state: UpstreamState, faults: Faults,
                 progress: Progress, cancel: threading.Event) -> dict[str, Any]:
    email = str(args.get("email") or "")
    postal = str(args.get("postal_code") or "").strip()
    _hang(faults, "get_customer", cancel)
    customer = data.customer_by_email(email)
    if customer is None:
        raise SupportToolError(
            "no customer account uses that email address",
            category="validation", failure_type="customer_not_found",
            attempted=describe_call("get_customer", args),
            remediation="Ask the customer to confirm the email on their account; do not guess one.",
        )
    raw = data.raw_customer(customer)
    progress.record("profile", raw)
    raw["verification"] = {
        "method": "email+postal_code",
        "matched": postal == customer.postal_code,
        "checked_fields": ["email", "postal_code"],
    }
    return raw


def lookup_order(args: dict[str, Any], state: UpstreamState, faults: Faults,
                 progress: Progress, cancel: threading.Event) -> dict[str, Any]:
    order_id = str(args.get("order_id") or "").strip().upper()
    order = data.ORDERS.get(order_id)
    if order is None:
        raise SupportToolError(
            f"no order {order_id!r}",
            category="validation", failure_type="order_not_found",
            attempted=describe_call("lookup_order", args),
            remediation="Ask the customer for the order number exactly as printed on the confirmation.",
        )
    header = data.raw_order_header(order)
    header["amounts"]["refunded"] = state.refunded.get(order_id, 0.0)
    progress.record("order_header", header)
    # The shipment service is the slow dependency; this is where a timeout lands.
    _hang(faults, "lookup_order", cancel)
    shipment = data.raw_shipment(order)
    progress.record("shipment", shipment)
    return {**header, "shipment": shipment,
            "refundable_until": order.refundable_until.isoformat() if order.refundable_until else None,
            "delivered_at": f"{order.delivered_on.isoformat()}T17:40:00Z" if order.delivered_on else None}


def process_refund(args: dict[str, Any], state: UpstreamState, faults: Faults,
                   progress: Progress, cancel: threading.Event) -> dict[str, Any]:
    attempted = describe_call("process_refund", args)
    customer_id = str(args.get("customer_id") or "")
    order_id = str(args.get("order_id") or "").strip().upper()
    amount = round(float(args.get("amount") or 0.0), 2)
    _hang(faults, "process_refund", cancel)

    order = data.ORDERS.get(order_id)
    if order is None:
        raise SupportToolError(f"no order {order_id!r}", category="validation",
                               failure_type="order_not_found", attempted=attempted)
    if order.customer_id != customer_id:
        raise SupportToolError(
            "that order does not belong to the verified customer",
            category="permission", failure_type="order_not_owned", attempted=attempted,
            remediation="Do not refund it. Confirm the order number with the customer.",
        )
    if order.delivered_on is None:
        raise SupportToolError(
            "the order has not been delivered, so it cannot be refunded yet",
            category="policy", failure_type="not_yet_delivered", attempted=attempted,
            remediation="Tell the customer the refund can be requested once it arrives.",
        )
    refundable = round(order.total - state.refunded.get(order_id, 0.0), 2)
    if amount <= 0 or amount > refundable + 0.005:
        raise SupportToolError(
            f"amount {amount:.2f} is outside the refundable range 0.01-{refundable:.2f}",
            category="validation", failure_type="amount_out_of_range", attempted=attempted,
            remediation="Use the order total from lookup_order, less anything already refunded.",
            detail={"refundable": refundable},
        )
    if data.POLICY_TODAY > order.refundable_until:
        raise SupportToolError(
            f"the refund window closed on {order.refundable_until.isoformat()}",
            category="policy", failure_type="outside_refund_window", attempted=attempted,
            remediation="Policy does not allow this refund automatically. Escalate with "
                        "reason_category policy_limit if the customer wants an exception.",
            detail={"refundable_until": order.refundable_until.isoformat()},
        )
    if amount > data.AUTO_REFUND_LIMIT:
        raise SupportToolError(
            f"refunds above {data.AUTO_REFUND_LIMIT:.2f} need a human approver",
            category="policy", failure_type="above_auto_refund_limit", attempted=attempted,
            remediation="Escalate with reason_category policy_limit; do not split the refund.",
            detail={"auto_refund_limit": data.AUTO_REFUND_LIMIT},
        )

    state.refunded[order_id] = round(state.refunded.get(order_id, 0.0) + amount, 2)
    refund = {
        "id": f"RF-{9000 + len(state.refunds) + 1}",
        "object": "refund",
        "order_id": order_id,
        "customer_id": customer_id,
        "amount": amount,
        "currency": data.CURRENCY,
        "status": "approved",
        "refund_type": args.get("refund_type"),
        "reason_code": args.get("reason_code"),
        "customer_statement": str(args.get("customer_statement") or ""),
        "processor": {"name": "ExamplePay", "ref": f"re_{order_id.lower()}_{len(state.refunds) + 1}",
                      "settlement_eta_days": 5},
        "ledger_entries": [
            {"account": "refunds_payable", "debit": amount},
            {"account": "cash", "credit": amount},
        ],
        "created_at": f"{data.POLICY_TODAY.isoformat()}T12:00:00Z",
    }
    state.refunds.append(refund)
    return refund


def escalate_to_human(args: dict[str, Any], state: UpstreamState, faults: Faults,
                      progress: Progress, cancel: threading.Event) -> dict[str, Any]:
    reason = str(args.get("reason_category") or "")
    if reason not in ESCALATION_REASONS:
        raise SupportToolError(
            f"unknown reason_category {reason!r}", category="validation",
            failure_type="bad_reason_category", attempted=describe_call("escalate_to_human", args),
            remediation=f"Use one of {', '.join(ESCALATION_REASONS)}.",
        )
    _hang(faults, "escalate_to_human", cancel)
    ticket = {
        "id": f"HT-{3000 + len(state.tickets) + 1}",
        "object": "handoff_ticket",
        "queue": "billing-exceptions" if reason in ("policy_gap", "policy_limit") else "tier2-support",
        "priority": "high" if reason == "customer_request" else "normal",
        "estimated_wait_minutes": 4 if reason == "customer_request" else 30,
        "reason_category": reason,
        "handoff_record": {k: args.get(k) for k in (
            "customer_id", "identity_verified", "order_ids", "customer_request", "root_cause",
            "recommended_action", "actions_taken")},
        "handoff": args.get("handoff") or {},
        "routing_rules_evaluated": [
            {"rule": r, "matched": r.startswith(reason)}
            for r in ("customer_request->tier2", "policy_gap->billing", "policy_limit->billing",
                      "unable_to_progress->tier2", "vip->priority")
        ],
        "created_at": f"{data.POLICY_TODAY.isoformat()}T12:00:00Z",
    }
    state.tickets.append(ticket)
    return ticket


Handler = Callable[[dict[str, Any], UpstreamState, Faults, Progress, threading.Event], dict[str, Any]]

HANDLERS: dict[str, Handler] = {
    "get_customer": get_customer,
    "lookup_order": lookup_order,
    "process_refund": process_refund,
    "escalate_to_human": escalate_to_human,
}
