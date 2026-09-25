"""The offline stand-in for the model: a rule-based support agent.

It follows the system prompt's rules (verify before refunding, decompose
multi-concern messages, escalate on the listed criteria with a full handoff
record, fill every action record from the case facts, retry a retryable
error once) so the harness around it can be exercised end to end. It is not
a language model, and nothing it does is evidence of how Claude behaves.

What keeps it honest is that it is **stateless**: every decision is derived
from the request alone -- the case-facts system block if present, the tool
results still visible in the verbatim window, and the conversation summary.
If a fact is not in the request, the mock does not know it. With case facts
turned off it can only recall what the summary says, which is how the
ablation measures drift.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from ..tools import data
from .backend import ModelTurn
from ..conversation.escalation import explicit_human_request
from ..conversation.facts import parse_rendered
from ..conversation.memory import SUMMARY_CLOSE, SUMMARY_OPEN

ORDER_RE = re.compile(r"\bORD-\d{4}\b", re.I)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[a-z]{2,}(?:\.[a-z]{2,})?", re.I)
POSTAL_RE = re.compile(r"\b\d{5}\b")
AMOUNT_RE = re.compile(r"\$\s?(\d[\d,]*(?:\.\d{2})?)")
ACK_RE = re.compile(r"^\s*(?:ok(?:ay)?|great|got it|sounds good|perfect|alright)[\s.!,]*$", re.I)
MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"

ITEM_HINTS = ("espresso", "machine", "grinder", "burr", "frother", "kettle")
ACCOUNT_FIELDS = ("email address", "email", "address", "phone number", "phone", "password", "name")

INTENTS: list[tuple[str, re.Pattern[str]]] = [
    ("identify", re.compile(r"[\w.+-]+@[\w-]+\.|\bcustomer id\b|\bC-\d{4}\b", re.I)),
    ("recall_refund", re.compile(r"\brefund (?:reference|number|id)\b|\bwhat(?:'s| is| was) the refund\b", re.I)),
    ("policy_gap", re.compile(r"\b(on sale|price (?:drop|match|adjust\w*)|the difference|compensat\w*|goodwill)\b", re.I)),
    ("policy_question", re.compile(r"\b(how long do i have|return window|return policy|refund policy|warranty)\b", re.I)),
    ("account_change", re.compile(r"\b(?:change|update|correct)\b.{0,30}\b(?:email|address|phone|password|name)\b", re.I)),
    ("billing_dispute", re.compile(r"\b(charged (?:me )?twice|double[- ]charged|duplicate charge|two charges|charged two times)\b", re.I)),
    ("refund", re.compile(r"\b(refund|money back)\b", re.I)),
    ("damage", re.compile(r"\b(cracked|broken|damaged|dented|shattered)\b", re.I)),
    ("billing_question", re.compile(r"\b(why was i charged|charged \$|billed|bank shows)\b", re.I)),
    ("recall_amount", re.compile(r"\b(how much|what did i pay|what was the total)\b", re.I)),
    ("recall_date", re.compile(r"\b(what date|when did i (?:order|buy|place))\b", re.I)),
    ("recall_email", re.compile(r"\b(which|what) email\b", re.I)),
    ("status", re.compile(r"\b(where is|status|track\w*|arrived|ship(?:ped)?|when will|look up|check)\b", re.I)),
    ("thanks", re.compile(r"\b(thanks|thank you|cheers)\b", re.I)),
    ("greeting", re.compile(r"^\s*(hi|hello|hey)\b", re.I)),
]

REASON_CODES: list[tuple[str, re.Pattern[str]]] = [
    ("damaged_item", re.compile(r"\b(cracked|broken|damaged|dented|shattered)\b", re.I)),
    ("defective", re.compile(r"\b(defective|leak\w*|stopped working|doesn't work|won't turn on)\b", re.I)),
    ("wrong_item", re.compile(r"\bwrong (?:item|colou?r|size|model)\b", re.I)),
    ("not_as_described", re.compile(r"\bnot as described\b", re.I)),
    ("late_delivery", re.compile(r"\b(late|took too long)\b", re.I)),
    ("changed_mind", re.compile(r"\b(too big|too small|changed my mind|don't need|don't want)\b", re.I)),
]

_CLAUSE_SPLIT = re.compile(
    r"(?<=[.?!])\s+|[,;]\s*(?:and\s+)?(?:also\s+)?(?=(?:where|how|what|when|which|can|could|please)\b)"
    r"|\s+and\s+(?:also\s+)?(?=(?:where|how|what|when|which|please)\b)"
    r"|\s+also\s+",
    re.I,
)


# --------------------------------------------------------------------------
# Reading the request
# --------------------------------------------------------------------------


@dataclass
class Result:
    name: str
    input: dict[str, Any]
    payload: dict[str, Any]
    is_error: bool


@dataclass
class View:
    customer_text: str = ""
    nudged: bool = False
    summary: str = ""
    facts: dict[str, Any] | None = None
    visible_results: list[Result] = field(default_factory=list)
    turn_results: list[Result] = field(default_factory=list)


def _text_blocks(content: Any) -> list[str]:
    if isinstance(content, str):
        return [content]
    return [b.get("text", "") for b in content if b.get("type") == "text"]


def read_view(params: dict[str, Any]) -> View:
    view = View()
    for block in params.get("system", []):
        text = block["text"] if isinstance(block, dict) else str(block)
        view.facts = parse_rendered(text) or view.facts

    uses: dict[str, tuple[str, dict[str, Any]]] = {}
    turn_start = 0
    messages = params["messages"]
    for i, msg in enumerate(messages):
        if msg["role"] == "assistant":
            for b in msg["content"] if isinstance(msg["content"], list) else []:
                if b.get("type") == "tool_use":
                    uses[b["id"]] = (b["name"], dict(b.get("input") or {}))
            continue
        for text in _text_blocks(msg["content"]):
            if text.startswith(SUMMARY_OPEN):
                view.summary = text[len(SUMMARY_OPEN):text.find(SUMMARY_CLOSE)].strip()
            elif text.startswith("<harness_notice>"):
                view.nudged = i > turn_start and "explicitly asked for a human" in text
            elif text.strip():
                view.customer_text, turn_start, view.nudged = text, i, False
        if isinstance(msg["content"], list):
            for b in msg["content"]:
                if b.get("type") != "tool_result" or b.get("tool_use_id") not in uses:
                    continue
                name, args = uses[b["tool_use_id"]]
                raw = b.get("content")
                raw = raw if isinstance(raw, str) else "".join(_text_blocks(raw or []))
                try:
                    payload = json.loads(raw)
                except (TypeError, ValueError):
                    payload = {"raw": raw}
                view.visible_results.append(Result(name, args, payload, bool(b.get("is_error"))))
                if i > turn_start:
                    view.turn_results.append(view.visible_results[-1])
    return view


@dataclass
class Knowledge:
    customer: dict[str, Any] | None = None
    orders: dict[str, dict[str, Any]] = field(default_factory=dict)
    approx: dict[str, dict[str, str]] = field(default_factory=dict)
    refunds: list[dict[str, Any]] = field(default_factory=list)


def _order_from_result(p: dict[str, Any]) -> dict[str, Any]:
    return {**p, "item_names": [i["name"] for i in p.get("items", [])]}


def build_knowledge(view: View) -> Knowledge:
    kn = Knowledge()
    for r in view.visible_results:
        if r.is_error:
            continue
        if r.name == "get_customer" and r.payload.get("verified"):
            kn.customer = r.payload
        elif r.name == "lookup_order":
            kn.orders[r.payload["order_id"]] = _order_from_result(r.payload)
        elif r.name == "process_refund":
            kn.refunds.append(r.payload)

    for line in view.summary.splitlines():
        ids = ORDER_RE.findall(line.split("Agent", 1)[-1])
        amount = re.search(r"about \$\d+(?:,\d{3})*", line)
        month = re.search(rf"in (?:{MONTHS}) \d{{4}}", line)
        if ids and (amount or month):
            slot = kn.approx.setdefault(ids[0].upper(), {})
            if amount:
                slot.setdefault("total", amount.group(0))
            if month:
                slot.setdefault("placed", month.group(0))

    if view.facts:  # authoritative: overrides anything read from the transcript
        if view.facts.get("customer"):
            kn.customer = view.facts["customer"]
        for oid, o in view.facts.get("orders", {}).items():
            merged = {**kn.orders.get(oid, {}), **o}
            merged["item_names"] = [s.split(" x ", 1)[-1] for s in o.get("items", [])]
            kn.orders[oid] = merged
        kn.refunds = list(view.facts.get("refunds", []))
    return kn


# --------------------------------------------------------------------------
# Concerns
# --------------------------------------------------------------------------


@dataclass
class Concern:
    intent: str
    text: str
    order_ids: list[str]


def classify(text: str) -> str | None:
    for name, pattern in INTENTS:
        if pattern.search(text):
            return name
    return None


def split_concerns(text: str) -> list[Concern]:
    clauses = [c.strip(" ,;") for c in _CLAUSE_SPLIT.split(text) if c and c.strip(" ,;")]
    concerns: list[Concern] = []
    pending = ""
    last_ids: list[str] = []
    for clause in clauses:
        merged = f"{pending} {clause}".strip()
        intent = classify(merged)
        if intent is None:
            pending = merged
            continue
        ids = [i.upper() for i in ORDER_RE.findall(merged)] or list(last_ids)
        last_ids = ids or last_ids
        if concerns and intent == concerns[-1].intent and ids == concerns[-1].order_ids:
            concerns[-1].text += " " + merged
        else:
            concerns.append(Concern(intent, merged, ids))
        pending = ""
    if pending:
        if concerns:
            concerns[-1].text += " " + pending
        else:
            concerns.append(Concern("other", pending, [i.upper() for i in ORDER_RE.findall(pending)]))
    return concerns or [Concern("other", text, [])]


# --------------------------------------------------------------------------
# Decisions
# --------------------------------------------------------------------------


@dataclass
class Call:
    name: str
    input: dict[str, Any]


def _last(view: View, name: str, **match: Any) -> Result | None:
    for r in reversed(view.turn_results):
        if r.name == name and all(str(r.input.get(k, "")).upper() == str(v).upper() for k, v in match.items()):
            return r
    return None


def _needs(view: View, name: str, **match: Any) -> bool:
    r = _last(view, name, **match)
    return r is None or (r.is_error and bool(r.payload.get("isRetryable")))


def _money(value: float, currency: str = "USD") -> str:
    return f"${value:,.2f}" if currency == "USD" else f"{value:,.2f} {currency}"


def _actions_taken(view: View) -> list[str]:
    out = []
    for r in view.turn_results:
        target = r.input.get("order_id") or r.input.get("email") or ""
        result = r.payload.get("failureType") if r.is_error else (
            r.payload.get("status") or ("verified" if r.payload.get("verified") else "ok"))
        out.append(f"{r.name} {target}: {result}".replace("  ", " "))
    return out


def _escalate(view: View, kn: Knowledge, reason: str, root_cause: str, recommended: str,
              order_ids: list[str] | None = None) -> Call | Result:
    done = _last(view, "escalate_to_human", reason_category=reason)
    if done and not done.is_error:
        return done
    customer_id = kn.customer["customer_id"] if kn.customer else None
    mentioned = [i.upper() for i in ORDER_RE.findall(view.customer_text)]
    return Call("escalate_to_human", {
        "reason_category": reason,
        "customer_id": customer_id,
        "identity_verified": customer_id is not None,
        "order_ids": sorted(set(order_ids or []) | set(mentioned)),
        "customer_request": f'The customer wrote: "{view.customer_text[:240]}"',
        "root_cause": root_cause,
        "recommended_action": recommended,
        "actions_taken": _actions_taken(view),
    })


def _ticket_line(ticket: Result) -> str:
    p = ticket.payload
    return (f"I've passed this to a colleague (ticket {p['ticket_id']}, expected wait about "
            f"{p['estimated_wait_minutes']} minutes). They'll see everything we've covered, so you "
            "won't need to repeat yourself.")


def _verify_first(what: str) -> str:
    return (f"I can help with {what}, but first I need to verify the account. Please send the email address "
            "and postal code on it.")


def _resolve_order(c: Concern, kn: Knowledge, view: View) -> str | Call | list[str] | None:
    if c.order_ids:
        return c.order_ids[0]
    hints = [h for h in ITEM_HINTS if h in c.text.lower()]
    if not hints:
        return None
    if kn.customer:
        for oid in kn.customer.get("order_ids", []):
            if oid not in kn.orders and _needs(view, "lookup_order", order_id=oid):
                return Call("lookup_order", {"order_id": oid})
    matches = [oid for oid, o in kn.orders.items()
               if any(h in " ".join(o.get("item_names", [])).lower() for h in hints)]
    return matches[0] if len(matches) == 1 else (matches or None)


def _ask_which(candidates: list[str], kn: Knowledge) -> str:
    listed = "; ".join(
        f"{oid} ({', '.join(kn.orders[oid]['item_names'])}, {_money(kn.orders[oid]['total'])}, "
        f"placed {kn.orders[oid]['placed_on']})" for oid in candidates)
    return f"I can see more than one order that could be: {listed}. Which one do you mean?"


def _order_or_step(c: Concern, kn: Knowledge, view: View, ask: str) -> tuple[str | None, Any]:
    """Resolve the concern's order and make sure it is loaded; returns (order_id, early_step)."""
    oid = _resolve_order(c, kn, view)
    if isinstance(oid, Call):
        return None, oid
    if isinstance(oid, list):
        return None, _ask_which(oid, kn)
    if oid is None:
        return None, ask
    if oid not in kn.orders:
        if _needs(view, "lookup_order", order_id=oid):
            return None, Call("lookup_order", {"order_id": oid})
        r = _last(view, "lookup_order", order_id=oid)
        return None, f"I couldn't load {oid}: {r.payload.get('description', 'no response')}." if r else ask
    return oid, None


def h_identify(c: Concern, kn: Knowledge, view: View):
    email = EMAIL_RE.search(c.text)
    postal = POSTAL_RE.search(c.text)
    if not email or not postal:
        return "To verify your account I need both the email address and the postal code on it."
    args = {"email": email.group(0), "postal_code": postal.group(0)}
    if _needs(view, "get_customer", email=args["email"]):
        return Call("get_customer", args)
    r = _last(view, "get_customer", email=args["email"])
    if r.is_error:
        return "I couldn't find an account with that email address. Could you check it?"
    if not r.payload.get("verified"):
        return "That postal code doesn't match the account. Could you double-check it?"
    return f"Thanks, {r.payload['name'].split()[0]}: your account is verified."


def h_status(c: Concern, kn: Knowledge, view: View):
    oid = _resolve_order(c, kn, view)
    if isinstance(oid, Call):
        return oid
    if isinstance(oid, list):
        return _ask_which(oid, kn)
    if oid is None:
        return "Which order is this about? The order number starts with ORD-."
    if _needs(view, "lookup_order", order_id=oid):
        return Call("lookup_order", {"order_id": oid})
    r = _last(view, "lookup_order", order_id=oid)
    if r.is_error:
        if r.payload.get("errorCategory") == "transient":
            esc = _escalate(
                view, kn, "unable_to_progress",
                f"lookup_order for {oid} failed twice ({r.payload.get('failureType')} on the "
                f"{r.payload.get('incompleteStep') or 'order'} step), so live tracking could not be retrieved.",
                f"Check the shipment status for {oid} in the carrier system and send the customer the latest "
                "scan; if the order service is still timing out, flag it to the on-call engineer.",
                [oid])
            if isinstance(esc, Call):
                return esc
            partial = (r.payload.get("partialResults") or {}).get("order_header")
            known = (f" What did come back: {oid} was placed on {partial['placed_on']} for "
                     f"{_money(partial['total'])} and is marked {partial['status']}."
                     if partial else "")
            return (f"I can't see the live tracking for {oid} right now: the order system timed out "
                    f"twice, and I won't guess.{known} {_ticket_line(esc)}")
        return f"I couldn't find {oid}. Could you check the order number?"
    o = r.payload
    items = ", ".join(i["name"] for i in o["items"])
    head = f"{oid} ({items}, {_money(o['total'])}, placed {o['placed_on']})"
    if o.get("delivered_on"):
        return f"{head} was delivered on {o['delivered_on']}."
    scan = o.get("latest_tracking") or {}
    return f"{head} is {o['status'].replace('_', ' ')}; latest scan: {scan.get('description', 'none')} on {scan.get('at', 'n/a')}."


def _reason_code(text: str) -> str:
    return next((code for code, pattern in REASON_CODES if pattern.search(text)), "other")


def _refund_record(c: Concern, kn: Knowledge, oid: str) -> dict[str, Any]:
    order = kn.orders[oid]
    refunded = order.get("refunded_amount", 0.0)
    refundable = round(order["total"] - refunded, 2)
    stated = AMOUNT_RE.search(c.text)
    amount = float(stated.group(1).replace(",", "")) if stated else refundable
    code = _reason_code(c.text)
    window = f", window open until {order['refundable_until']}" if order.get("refundable_until") else ""
    return {
        "customer_id": kn.customer["customer_id"], "order_id": oid, "amount": amount,
        "currency": order.get("currency", data.CURRENCY),
        "refund_type": "full" if abs(amount - refundable) <= 0.005 else "partial",
        "reason_code": code,
        "customer_statement": c.text[:200],
        "justification": (f"{oid} total {order['total']:.2f}, already refunded {refunded:.2f}, refundable "
                          f"{refundable:.2f}{window}; the customer asked for {amount:.2f} "
                          f"({code.replace('_', ' ')})."),
    }


def h_refund(c: Concern, kn: Knowledge, view: View):
    if not kn.customer or not kn.customer.get("verified"):
        return _verify_first("that refund")
    oid, step = _order_or_step(c, kn, view, "Which order would you like refunded? The order number starts with ORD-.")
    if step is not None:
        return step
    r = _last(view, "process_refund", order_id=oid)
    if r is None:
        prior = next((rf for rf in kn.refunds if rf["order_id"] == oid), None)
        if prior and not AMOUNT_RE.search(c.text):
            return f"{oid} was already refunded ({prior['refund_id']}, {_money(prior['amount'])})."
        return Call("process_refund", _refund_record(c, kn, oid))
    if not r.is_error:
        p = r.payload
        return (f"Done: refund {p['refund_id']} for {_money(p['amount'])} on {oid} is {p['status']}. "
                f"It should reach your original payment method within {p['settlement_eta_days']} business days.")
    p = r.payload
    if p.get("failureType") == "not_yet_delivered":
        return f"{oid} hasn't been delivered yet, so it can't be refunded; you can ask again once it arrives."
    if p.get("errorCategory") == "policy":
        amount = r.input.get("amount", 0.0)
        if p.get("failureType") == "above_auto_refund_limit":
            nxt = (f"Approve or decline the {_money(amount)} refund on {oid}, which is above the "
                   f"{_money(data.AUTO_REFUND_LIMIT)} auto-approval limit; if approved, issue it as a single refund "
                   "and confirm with the customer.")
        else:
            nxt = (f"Decide whether to grant an exception for {oid} ({_money(amount)}) given "
                   f"'{p['description']}'; if approved, issue the refund manually and confirm with the customer.")
        esc = _escalate(view, kn, "policy_limit", f"process_refund for {oid} was rejected by policy: "
                                                  f"{p['description']}.", nxt, [oid])
        if isinstance(esc, Call):
            return esc
        return f"I can't approve that refund myself: {p['description']}. {_ticket_line(esc)}"
    if p.get("errorCategory") == "prerequisite":
        return "I need to verify the account before refunding. Please send the email address and postal code on it."
    return f"I couldn't process that refund: {p['description']}."


def h_damage(c: Concern, kn: Knowledge, view: View):
    if not kn.customer:
        return "I'm sorry to hear that. " + _verify_first("a refund for it")
    oid, step = _order_or_step(c, kn, view, "I'm sorry to hear that. Which order was it? The number starts with ORD-.")
    if step is not None:
        return step
    o = kn.orders[oid]
    return (f"I'm sorry about that. For {oid} ({', '.join(o['item_names'])}, {_money(o['total'])}) I can refund "
            "the whole order, or part of it for the damaged part. Which would you prefer? If part, tell me the "
            "amount.")


def h_billing_question(c: Concern, kn: Knowledge, view: View):
    oid, step = _order_or_step(c, kn, view, "Which order is the charge for? The number starts with ORD-.")
    if step is not None:
        return step
    o = kn.orders[oid]
    shipping = o.get("shipping", 0.0)
    goods = round(o["total"] - shipping, 2)
    refunded = o.get("refunded_amount", 0.0)
    tail = f" {_money(refunded)} of it has been refunded." if refunded else " That's the only charge on the order."
    return (f"{oid} came to {_money(o['total'])}: {_money(goods)} for {', '.join(o['item_names'])} plus "
            f"{_money(shipping)} shipping.{tail}")


def h_billing_dispute(c: Concern, kn: Knowledge, view: View):
    if not kn.customer:
        return _verify_first("a charge query")
    oid, step = _order_or_step(c, kn, view, "Which order are the charges for? The number starts with ORD-.")
    if step is not None:
        return step
    o = kn.orders[oid]
    esc = _escalate(
        view, kn, "unable_to_progress",
        f"The customer reports being charged twice for {oid}; the order record shows a single charge of "
        f"{_money(o['total'])} and {_money(o.get('refunded_amount', 0.0))} refunded, so the second charge cannot be "
        "confirmed from the order or refund tools.",
        f"Check the payment processor for a second capture against {oid}. If one exists, refund the duplicate "
        f"{_money(o['total'])} to the original payment method and confirm with the customer; if not, explain that "
        "the second line is likely a pending authorisation that will drop off.",
        [oid])
    if isinstance(esc, Call):
        return esc
    return (f"I can see one charge of {_money(o['total'])} on {oid} and nothing extra. I can't see your bank's "
            f"side, so rather than guess or refund against a charge I can't confirm, I've sent it to billing with "
            f"exactly what to check. {_ticket_line(esc)}")


def h_account_change(c: Concern, kn: Knowledge, view: View):
    lowered = c.text.lower()
    thing = next((f for f in ACCOUNT_FIELDS if f in lowered), "details")
    who = kn.customer["customer_id"] if kn.customer else "the account"
    esc = _escalate(
        view, kn, "policy_gap",
        f"The customer asked to change the {thing} on their account; this agent has no account-change tool, and "
        "account changes need identity re-verification by the account team.",
        f"Re-verify the customer's identity through the account-security process, then update the {thing} on "
        f"{who} to the value the customer provides and confirm the change to both the old and new contact.")
    if isinstance(esc, Call):
        return esc
    return (f"I can't change account details myself: the account team has to re-verify you first, so I've passed "
            f"your {thing} change on. {_ticket_line(esc)}")


def h_policy_question(c: Concern, kn: Knowledge, view: View):
    base = f"Refunds are available for {data.REFUND_WINDOW_DAYS} days after delivery."
    if "warranty" in c.text.lower():
        return (base + " I don't have warranty terms in the policy I can apply, so I won't guess. "
                "If you'd like, I can hand this to a colleague who can check.")
    oid = _resolve_order(c, kn, view)
    if isinstance(oid, Call):
        return oid
    if not isinstance(oid, str):
        return base
    if oid not in kn.orders and _needs(view, "lookup_order", order_id=oid):
        return Call("lookup_order", {"order_id": oid})
    o = kn.orders.get(oid)
    if not o or not o.get("refundable_until"):
        return base + f" {oid} hasn't been delivered yet, so its window hasn't started."
    closed = o["refundable_until"] < data.POLICY_TODAY.isoformat()
    verb = "closed on" if closed else "is open until"
    tail = " so it can't be refunded automatically any more." if closed else "."
    return f"{base} For {oid} (delivered {o['delivered_on']}) the window {verb} {o['refundable_until']},{tail}".replace(",.", ".")


def h_policy_gap(c: Concern, kn: Knowledge, view: View):
    where = f" on {c.order_ids[0]}" if c.order_ids else ""
    esc = _escalate(
        view, kn, "policy_gap",
        f"The customer is asking for a price adjustment or compensation{where}, which the refund policy does not "
        "cover, so the agent cannot grant or refuse it.",
        f"Decide whether to offer a goodwill credit or price adjustment{where}; if yes, apply it and confirm with "
        "the customer, otherwise explain the policy to them.",
        c.order_ids)
    if isinstance(esc, Call):
        return esc
    return ("Our refund policy covers returns, not price adjustments or compensation, and I don't want "
            f"to make one up. {_ticket_line(esc)}")


def _recall(c: Concern, kn: Knowledge, view: View, field_name: str, approx_key: str, label: str):
    oid = c.order_ids[0] if c.order_ids else None
    if oid is None:
        return f"Which order do you mean? I can tell you the {label} once I know."
    if oid in kn.orders:
        value = kn.orders[oid][field_name]
        return f"{oid}: {label} {_money(value) if field_name == 'total' else value}."
    if approx_key in kn.approx.get(oid, {}):
        return f"{oid}: {label} {kn.approx[oid][approx_key]}."
    if _needs(view, "lookup_order", order_id=oid):
        return Call("lookup_order", {"order_id": oid})
    return f"I can't load {oid} right now."


def h_recall_amount(c, kn, view):
    return _recall(c, kn, view, "total", "total", "you paid")


def h_recall_date(c, kn, view):
    return _recall(c, kn, view, "placed_on", "placed", "you placed it")


def h_recall_email(c: Concern, kn: Knowledge, view: View):
    if kn.customer:
        return f"The email on your account is {kn.customer['email']}."
    return ("I don't have your account details in front of me any more. Could you confirm the email "
            "address and postal code so I can look them up?")


def h_recall_refund(c: Concern, kn: Knowledge, view: View):
    if not kn.refunds:
        return "I don't see a refund issued in this conversation."
    return " ".join(f"Refund {rf['refund_id']}: {_money(rf['amount'])} on {rf['order_id']} ({rf['status']})."
                    for rf in kn.refunds)


def h_thanks(c, kn, view):
    return "You're welcome!"


def h_greeting(c, kn, view):
    return "Hi! Happy to help. What can I do for you?"


def h_other(c: Concern, kn: Knowledge, view: View):
    if ACK_RE.match(c.text):
        return "Glad that helps. Anything else I can do?"
    return ("I can help with orders, refunds and billing, but I don't have information on that, "
            "so I won't guess.")


HANDLERS = {
    "identify": h_identify, "status": h_status, "refund": h_refund, "damage": h_damage,
    "billing_question": h_billing_question, "billing_dispute": h_billing_dispute,
    "account_change": h_account_change, "policy_question": h_policy_question, "policy_gap": h_policy_gap,
    "recall_amount": h_recall_amount, "recall_date": h_recall_date, "recall_email": h_recall_email,
    "recall_refund": h_recall_refund, "thanks": h_thanks, "greeting": h_greeting, "other": h_other,
}


def _tool_turn(call: Call, view: View, params: dict[str, Any]) -> ModelTurn:
    # Unique within any request, including after compaction drops older ids.
    seed = json.dumps([len(params["messages"]), view.customer_text, len(view.turn_results),
                       call.name, call.input], sort_keys=True)
    tool_id = "toolu_mock_" + hashlib.sha1(seed.encode()).hexdigest()[:16]
    block = {"type": "tool_use", "id": tool_id, "name": call.name, "input": call.input}
    return ModelTurn("tool_use", [block])


def _text_turn(text: str) -> ModelTurn:
    return ModelTurn("end_turn", [{"type": "text", "text": text}])


def decide(params: dict[str, Any]) -> ModelTurn:
    view = read_view(params)
    kn = build_knowledge(view)

    if view.nudged or explicit_human_request(view.customer_text):
        verify = ("" if kn.customer else " Verify their identity (email and postal code) before discussing "
                                          "account or order details.")
        esc = _escalate(
            view, kn, "customer_request",
            "The customer explicitly asked to speak to a person, so the agent escalated before attempting any fix.",
            f'Contact the customer promptly about the issue they raised: "{view.customer_text[:160]}".{verify}')
        if isinstance(esc, Call):
            return _tool_turn(esc, view, params)
        return _text_turn(f"Of course. {_ticket_line(esc)}")

    concerns = split_concerns(view.customer_text)
    sections: list[str] = []
    for concern in concerns:
        step = HANDLERS.get(concern.intent, h_other)(concern, kn, view)
        if isinstance(step, Call):
            return _tool_turn(step, view, params)
        sections.append(step)
    if len(sections) == 1:
        return _text_turn(sections[0])
    return _text_turn("\n\n".join(f"{i}. {s}" for i, s in enumerate(sections, 1)))
