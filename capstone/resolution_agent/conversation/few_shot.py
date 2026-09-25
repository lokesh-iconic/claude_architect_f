"""Few-shot examples for the three most ambiguous request types.

Chosen for where the rules alone are weakest, not for typical requests:

1. **Money back for a defect, amount stated.** Looks like the price-drop
   request ("can I get $20 back?"), but a defect is a refund and a price
   change is a policy gap. Shows a partial refund record and the contrast.
2. **A duplicate charge the records don't show.** The customer is probably
   right about their bank statement, and the order record shows one charge.
   Refunding on the claim alone is wrong, and so is telling them they are
   mistaken. Shows the handoff record: root cause and a concrete next step.
3. **One reference, two matching orders.** Shows a clarifying question and
   no action.

The ids are fictional and not in the data store (C-20xx, ORD-80xx), so an
example can't be mistaken for a live case. A test runs each example's
action through the same schema and case-fact checks the agent faces, so an
example can't teach a record the validator would reject.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .facts import CaseFacts, CustomerFact, OrderFact


@dataclass(frozen=True)
class FewShotExample:
    name: str
    request_type: str
    customer: str
    reasoning: str
    tool: str | None
    action: dict[str, Any] | None
    reply: str
    facts: CaseFacts

    def render(self) -> str:
        facts = json.dumps({"customer": self.facts.customer.customer_id if self.facts.customer else None,
                            "orders": {k: {"total": o.total, "refunded_amount": o.refunded_amount,
                                           "items": o.items, "refundable_until": o.refundable_until}
                                       for k, o in self.facts.orders.items()}}, sort_keys=True)
        action = (f"Action: {self.tool}({json.dumps(self.action, sort_keys=True)})"
                  if self.tool else "Action: none -- ask first.")
        return (f"### {self.request_type}\n"
                f"Case facts (abridged): {facts}\n"
                f'Customer: "{self.customer}"\n'
                f"Reasoning: {self.reasoning}\n"
                f"{action}\n"
                f'Reply: "{self.reply}"')


def _customer(cid: str, name: str, email: str, orders: list[str]) -> CustomerFact:
    return CustomerFact(cid, name, email, "standard", True, orders, recorded_turn=1)


def _order(oid: str, total: float, items: list[str], placed: str, delivered: str | None,
           until: str | None, refunded: float = 0.0, shipping: float = 0.0) -> OrderFact:
    status = "delivered" if delivered else "in_transit"
    return OrderFact(oid, status, placed, delivered, total, shipping, "USD", refunded, until, items, recorded_turn=1)


PARTIAL_REFUND_FOR_DEFECT = FewShotExample(
    name="partial_refund_for_defect",
    request_type="Money back for a defect, with an amount",
    customer="The lid on my blender arrived cracked. Could I get $25 back for it instead of returning the whole thing?",
    reasoning=(
        "A damaged part is a defect, which the refund policy covers, and the customer named an amount. "
        "ORD-8101 is delivered, inside its window (until 2026-10-12), total 180.00 with nothing refunded, "
        "so 25.00 is within the refundable amount and is a partial refund. Contrast: 'it went on sale, can I "
        "get $25 back' is a price adjustment, not a defect, so that is policy_gap and no refund. If no "
        "amount is named, ask whether they want the whole order refunded or part of it; don't pick one."
    ),
    tool="process_refund",
    action={
        "customer_id": "C-2001", "order_id": "ORD-8101", "amount": 25.00, "currency": "USD",
        "refund_type": "partial", "reason_code": "damaged_item",
        "customer_statement": "The lid on my blender arrived cracked.",
        "justification": "ORD-8101 total 180.00, nothing refunded yet, window open until 2026-10-12; the customer "
                         "asked for 25.00 back for a cracked lid, which is within the refundable 180.00.",
    },
    reply="Done: I've refunded $25.00 on ORD-8101 for the cracked lid (refund RF-...). It should reach your "
          "card within 5 business days.",
    facts=CaseFacts(customer=_customer("C-2001", "Sam Ortiz", "sam.ortiz@example.com", ["ORD-8101"]),
                    orders={"ORD-8101": _order("ORD-8101", 180.00, ["1 x Countertop blender"],
                                               "2026-09-08", "2026-09-12", "2026-10-12")}),
)

DUPLICATE_CHARGE_NOT_IN_RECORDS = FewShotExample(
    name="duplicate_charge_not_in_records",
    request_type="A duplicate charge the records don't show",
    customer="My bank statement shows two charges of $54.00 for ORD-8230. I only ordered once!",
    reasoning=(
        "The order record shows one captured payment of 54.00 and nothing refunded. I can't see the bank's "
        "side, so I can't confirm or rule out a second capture. Refunding 54.00 on the claim alone would "
        "move money I can't justify, and telling the customer they are wrong would be a guess. The records "
        "and the customer disagree in a way my tools can't resolve, so this is unable_to_progress. The "
        "handoff names what to check and what to do if it's confirmed."
    ),
    tool="escalate_to_human",
    action={
        "reason_category": "unable_to_progress", "customer_id": "C-2002", "identity_verified": True,
        "order_ids": ["ORD-8230"],
        "customer_request": "Customer reports being charged twice (2 x 54.00) for ORD-8230 and wants the duplicate back.",
        "root_cause": "The order record shows a single captured payment of 54.00 and no refund; the second charge "
                      "the customer sees cannot be confirmed from the order or refund tools.",
        "recommended_action": "Check the payment processor for a second capture against ORD-8230. If one exists, "
                              "refund the duplicate 54.00 to the original card and confirm with the customer; if "
                              "not, explain that the second line is likely a pending authorisation that will drop off.",
        "actions_taken": ["lookup_order ORD-8230: one captured payment of 54.00, nothing refunded"],
    },
    reply="I can see one charge of $54.00 on ORD-8230 and nothing refunded. I can't see your bank's side, so rather "
          "than guess I've passed this to our billing team (ticket HT-...) with exactly what to check.",
    facts=CaseFacts(customer=_customer("C-2002", "Lee Park", "lee.park@example.com", ["ORD-8230"]),
                    orders={"ORD-8230": _order("ORD-8230", 54.00, ["2 x Pour-over filter pack"],
                                               "2026-09-20", None, None)}),
)

AMBIGUOUS_ORDER_REFERENCE = FewShotExample(
    name="ambiguous_order_reference",
    request_type="One reference, two matching orders",
    customer="Please refund the mug, the handle came off.",
    reasoning=(
        "Two delivered orders contain a mug (ORD-8310 and ORD-8311, both 18.00). Refunding either is a guess "
        "that moves money on the wrong order half the time. Ask which one, naming both, and do nothing yet."
    ),
    tool=None,
    action=None,
    reply="I can see two orders with a mug: ORD-8310 (placed 2026-09-01) and ORD-8311 (placed 2026-09-14), both "
          "$18.00. Which one had the handle come off?",
    facts=CaseFacts(customer=_customer("C-2003", "Ana Ruiz", "ana.ruiz@example.com", ["ORD-8310", "ORD-8311"]),
                    orders={"ORD-8310": _order("ORD-8310", 18.00, ["1 x Stoneware mug"], "2026-09-01",
                                               "2026-09-04", "2026-10-04"),
                            "ORD-8311": _order("ORD-8311", 18.00, ["1 x Stoneware mug"], "2026-09-14",
                                               "2026-09-17", "2026-10-17")}),
)

EXAMPLES = [PARTIAL_REFUND_FOR_DEFECT, DUPLICATE_CHARGE_NOT_IN_RECORDS, AMBIGUOUS_ORDER_REFERENCE]


def render_all() -> str:
    return "\n\n".join(e.render() for e in EXAMPLES)
