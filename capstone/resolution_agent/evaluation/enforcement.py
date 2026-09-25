"""Property checks for the two "no exceptions" claims.

"Every time" is a claim about all call sequences, not a handful of scripted
ones. This drives thousands of seeded random tool-call sequences -- right and
wrong emails and postal codes, own and other customers' ids, well-formed and
malformed action records, refunds and handoffs at every position -- straight
at `ToolSession`, and checks two invariants against the session's own state:

1. A `process_refund` handler never ran unless an earlier `get_customer` in
   the same session returned `verified: true` for exactly that `customer_id`.
2. Every handler run of a money or case-state tool has exactly one ledger
   record, and every ledger record passes the full schema and the case-fact
   checks it was admitted under.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any

from ..tools import data
from ..tools.schema import ACTION_TOOLS, REFUND_REASON_CODES, validate_input
from ..tools.session import ToolSession

_EMAILS = [c.email for c in data.CUSTOMERS.values()] + ["nobody@example.com"]
_POSTALS = [c.postal_code for c in data.CUSTOMERS.values()] + ["00000"]
_CUSTOMER_IDS = list(data.CUSTOMERS) + ["C-9999", ""]
_ORDER_IDS = list(data.ORDERS) + ["ORD-0000"]
_AMOUNTS = [1.0, 10.0, 39.0, 64.0, 89.5, 249.99, 600.0, 1420.0, -5.0]


@dataclass
class FuzzReport:
    sequences: int
    calls: int = 0
    refund_attempts: int = 0
    blocked_by_prerequisite: int = 0
    rejected_by_validation: int = 0
    reached_handler: int = 0
    reached_handler_verified: int = 0
    action_handler_runs: int = 0
    ledger_records: int = 0
    invalid_ledger_records: int = 0
    bypasses: list[dict[str, Any]] = field(default_factory=list)


def _plausible_refund(rng: random.Random, focus: str) -> dict[str, Any]:
    """A well-formed refund for the focus customer's own order -- the kind that should get through."""
    order = rng.choice(data.orders_for(focus))
    amount = rng.choice([order.total, round(order.total / 2, 2)])
    return {
        "customer_id": order.customer_id, "order_id": order.order_id, "amount": amount, "currency": "USD",
        "refund_type": "full" if amount == order.total else "partial", "reason_code": "damaged_item",
        "customer_statement": "it arrived cracked",
        "justification": "Order total and window checked in the case facts; amount within refundable.",
    }


def _refund(rng: random.Random, focus: str) -> dict[str, Any]:
    if rng.random() < 0.5:
        return _plausible_refund(rng, focus)
    args: dict[str, Any] = {
        "customer_id": rng.choice(_CUSTOMER_IDS), "order_id": rng.choice(_ORDER_IDS),
        "amount": rng.choice(_AMOUNTS), "currency": rng.choice(["USD", "USD", "EUR"]),
        "refund_type": rng.choice(["full", "partial"]), "reason_code": rng.choice(REFUND_REASON_CODES),
        "customer_statement": rng.choice(["it arrived cracked", "x"]),
        "justification": rng.choice(["Order total and window checked in the case facts; amount within refundable.",
                                     "please review"]),
    }
    if rng.random() < 0.15:
        args.pop(rng.choice(sorted(args)))
    return args


def _handoff(rng: random.Random) -> dict[str, Any]:
    args: dict[str, Any] = {
        "reason_category": rng.choice(["customer_request", "policy_gap", "policy_limit", "unable_to_progress", "vip"]),
        "customer_id": rng.choice(_CUSTOMER_IDS[:-1] + [None]),
        "identity_verified": rng.choice([True, False]),
        "order_ids": rng.sample(_ORDER_IDS, rng.randint(0, 2)),
        "customer_request": "Customer wants help with an order problem.",
        "root_cause": rng.choice(["The refund was rejected by policy as outside the window.", "please review"]),
        "recommended_action": rng.choice(["Decide on an exception and refund manually if approved.", "n/a"]),
        "actions_taken": [],
    }
    if rng.random() < 0.15:
        args.pop(rng.choice(sorted(args)))
    return args


def _random_call(rng: random.Random, focus: str) -> tuple[str, dict[str, Any]]:
    """Half the identity and order calls target the sequence's focus customer; the rest are noise."""
    tool = rng.choices(["get_customer", "lookup_order", "process_refund", "escalate_to_human"],
                       weights=[3, 3, 5, 1])[0]
    customer = data.CUSTOMERS[focus]
    if tool == "get_customer":
        if rng.random() < 0.5:
            return tool, {"email": customer.email, "postal_code": rng.choice([customer.postal_code, "00000"])}
        return tool, {"email": rng.choice(_EMAILS), "postal_code": rng.choice(_POSTALS)}
    if tool == "lookup_order":
        pool = [o.order_id for o in data.orders_for(focus)] if rng.random() < 0.5 else _ORDER_IDS
        return tool, {"order_id": rng.choice(pool)}
    return tool, _refund(rng, focus) if tool == "process_refund" else _handoff(rng)


def fuzz(sequences: int = 2000, max_len: int = 10, seed: int = 20260925) -> FuzzReport:
    rng = random.Random(seed)
    report = FuzzReport(sequences=sequences)
    for _ in range(sequences):
        session = ToolSession(tool_timeout_s=2.0)
        session.begin_turn()
        verified_so_far: set[str] = set()
        focus = rng.choice(sorted(data.CUSTOMERS))
        for _ in range(rng.randint(1, max_len)):
            tool, args = _random_call(rng, focus)
            invoked_before = session.state.invocations["process_refund"]
            outcome = session.call(tool, args)
            report.calls += 1
            if tool == "get_customer" and not outcome.is_error and outcome.payload.get("verified"):
                verified_so_far.add(outcome.payload["customer_id"])
            if tool != "process_refund":
                continue
            report.refund_attempts += 1
            reached = session.state.invocations["process_refund"] > invoked_before
            category = outcome.payload.get("errorCategory")
            report.blocked_by_prerequisite += category == "prerequisite"
            report.rejected_by_validation += outcome.payload.get("failureType") == "invalid_action_record"
            if reached:
                report.reached_handler += 1
                if args.get("customer_id") in verified_so_far:
                    report.reached_handler_verified += 1
                else:
                    report.bypasses.append({"args": args, "log": [o.tool for o in session.log]})
        runs = sum(session.state.invocations[t] for t in ACTION_TOOLS)
        report.action_handler_runs += runs
        report.ledger_records += len(session.ledger)
        report.invalid_ledger_records += sum(bool(validate_input(r.tool, r.record)) for r in session.ledger)
        if runs != len(session.ledger):
            report.bypasses.append({"ledger_mismatch": {"handler_runs": runs, "ledger": len(session.ledger)}})
    return report
