"""The persistent case-facts block.

Transactional facts (ids, amounts, dates, refund and ticket numbers) are
extracted by the program from *successful tool results*, never from
customer prose or model output, and rendered into every request as their
own system block. They never pass through the summarizer, so compaction
can shorten the narrative but cannot round, paraphrase, or drop them.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

OPEN_TAG = "<case_facts>"
CLOSE_TAG = "</case_facts>"


@dataclass
class CustomerFact:
    customer_id: str
    name: str
    email: str
    tier: str
    verified: bool
    order_ids: list[str]
    recorded_turn: int


@dataclass
class OrderFact:
    order_id: str
    status: str
    placed_on: str
    delivered_on: str | None
    total: float
    shipping: float
    currency: str
    refunded_amount: float
    refundable_until: str | None
    items: list[str]
    recorded_turn: int


@dataclass
class RefundFact:
    refund_id: str
    order_id: str
    amount: float
    currency: str
    status: str
    recorded_turn: int


@dataclass
class TicketFact:
    ticket_id: str
    queue: str
    reason_category: str
    recorded_turn: int


@dataclass
class FailureFact:
    tool: str
    failure_type: str
    attempted: str
    recorded_turn: int


@dataclass
class CaseFacts:
    customer: CustomerFact | None = None
    orders: dict[str, OrderFact] = field(default_factory=dict)
    refunds: list[RefundFact] = field(default_factory=list)
    tickets: list[TicketFact] = field(default_factory=list)
    unresolved_failures: list[FailureFact] = field(default_factory=list)

    def record_success(self, tool: str, trimmed: dict[str, Any], turn: int) -> None:
        if tool == "get_customer" and trimmed.get("verified"):
            self.customer = CustomerFact(
                customer_id=trimmed["customer_id"], name=trimmed["name"], email=trimmed["email"],
                tier=trimmed["tier"], verified=True, order_ids=list(trimmed["order_ids"]),
                recorded_turn=turn,
            )
        elif tool == "lookup_order":
            self.orders[trimmed["order_id"]] = OrderFact(
                order_id=trimmed["order_id"], status=trimmed["status"],
                placed_on=trimmed["placed_on"], delivered_on=trimmed.get("delivered_on"),
                total=trimmed["total"], shipping=trimmed["shipping"], currency=trimmed["currency"],
                refunded_amount=trimmed["refunded_amount"],
                refundable_until=trimmed.get("refundable_until"),
                items=[f'{i["qty"]} x {i["name"]}' for i in trimmed["items"]],
                recorded_turn=turn,
            )
            self._clear_failures("lookup_order", trimmed["order_id"])
        elif tool == "process_refund":
            self.refunds.append(RefundFact(
                refund_id=trimmed["refund_id"], order_id=trimmed["order_id"],
                amount=trimmed["amount"], currency=trimmed["currency"],
                status=trimmed["status"], recorded_turn=turn,
            ))
            order = self.orders.get(trimmed["order_id"])
            if order:
                order.refunded_amount = round(order.refunded_amount + trimmed["amount"], 2)
        elif tool == "escalate_to_human":
            self.tickets.append(TicketFact(
                ticket_id=trimmed["ticket_id"], queue=trimmed["queue"],
                reason_category=trimmed["reason_category"], recorded_turn=turn,
            ))
            self.unresolved_failures.clear()  # handed to a human, with the handoff context

    def record_failure(self, tool: str, payload: dict[str, Any], turn: int) -> None:
        if payload.get("errorCategory") in ("transient", "internal"):
            self.unresolved_failures.append(FailureFact(
                tool=tool, failure_type=payload.get("failureType", "unknown"),
                attempted=payload.get("attempted", tool), recorded_turn=turn,
            ))

    def _clear_failures(self, tool: str, needle: str) -> None:
        self.unresolved_failures = [
            f for f in self.unresolved_failures if not (f.tool == tool and needle in f.attempted)
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "customer": asdict(self.customer) if self.customer else None,
            "orders": {k: asdict(v) for k, v in sorted(self.orders.items())},
            "refunds": [asdict(r) for r in self.refunds],
            "tickets": [asdict(t) for t in self.tickets],
            "unresolved_failures": [asdict(f) for f in self.unresolved_failures],
        }

    def render(self) -> str:
        body = json.dumps(self.to_dict(), sort_keys=True)
        return (
            f"{OPEN_TAG}\n"
            "Authoritative facts for this case, extracted by the system from tool results. "
            "Quote amounts, dates and ids from here exactly. If the conversation summary "
            "disagrees with these facts, these facts win.\n"
            f"{body}\n{CLOSE_TAG}"
        )


def parse_rendered(text: str) -> dict[str, Any] | None:
    """Inverse of `render`, used by the mock backend to read what the request carries."""
    start, end = text.find(OPEN_TAG), text.find(CLOSE_TAG)
    if start == -1 or end == -1:
        return None
    inner = text[start + len(OPEN_TAG):end]
    return json.loads(inner[inner.find("{"):])
