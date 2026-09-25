"""Property check for the refund prerequisite.

"Every time, not just usually" is a claim about all call sequences, not a
handful of scripted ones. This drives thousands of seeded random sequences of
tool calls -- right and wrong emails and postal codes, own and other
customers' ids, refunds at every position -- straight at `ToolSession`, and
checks one invariant against the session's own log: a `process_refund`
handler never ran unless an earlier `get_customer` in the same session
returned `verified: true` for exactly that `customer_id`.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any

from . import data
from .session import ToolSession

_EMAILS = [c.email for c in data.CUSTOMERS.values()] + ["nobody@example.com"]
_POSTALS = [c.postal_code for c in data.CUSTOMERS.values()] + ["00000"]
_CUSTOMER_IDS = list(data.CUSTOMERS) + ["C-9999", ""]
_ORDER_IDS = list(data.ORDERS) + ["ORD-0000"]


@dataclass
class FuzzReport:
    sequences: int
    calls: int = 0
    refund_attempts: int = 0
    blocked_by_prerequisite: int = 0
    reached_handler: int = 0
    reached_handler_verified: int = 0
    bypasses: list[dict[str, Any]] = field(default_factory=list)


def _random_call(rng: random.Random) -> tuple[str, dict[str, Any]]:
    tool = rng.choices(["get_customer", "lookup_order", "process_refund"], weights=[3, 2, 5])[0]
    if tool == "get_customer":
        return tool, {"email": rng.choice(_EMAILS), "postal_code": rng.choice(_POSTALS)}
    if tool == "lookup_order":
        return tool, {"order_id": rng.choice(_ORDER_IDS)}
    return tool, {"customer_id": rng.choice(_CUSTOMER_IDS), "order_id": rng.choice(_ORDER_IDS),
                  "amount": rng.choice([1.0, 10.0, 39.0, 64.0, 89.5, 249.99, 600.0]), "reason": "fuzz"}


def fuzz(sequences: int = 2000, max_len: int = 8, seed: int = 20260925) -> FuzzReport:
    rng = random.Random(seed)
    report = FuzzReport(sequences=sequences)
    for _ in range(sequences):
        session = ToolSession(tool_timeout_s=2.0)
        verified_so_far: set[str] = set()
        for _ in range(rng.randint(1, max_len)):
            tool, args = _random_call(rng)
            invoked_before = session.state.invocations["process_refund"]
            outcome = session.call(tool, args)
            report.calls += 1
            if tool == "get_customer" and not outcome.is_error and outcome.payload.get("verified"):
                verified_so_far.add(outcome.payload["customer_id"])
            if tool != "process_refund":
                continue
            report.refund_attempts += 1
            reached = session.state.invocations["process_refund"] > invoked_before
            if outcome.payload.get("errorCategory") == "prerequisite":
                report.blocked_by_prerequisite += 1
            if reached:
                report.reached_handler += 1
                if args["customer_id"] in verified_so_far:
                    report.reached_handler_verified += 1
                else:
                    report.bypasses.append({"args": args, "log": [o.tool for o in session.log]})
    return report
