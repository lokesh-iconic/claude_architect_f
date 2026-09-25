"""The offline stand-in for the model: a rule-based support agent.

It follows the system prompt's rules (verify before refunding, decompose
multi-concern messages, escalate on the listed criteria, retry a retryable
error once) so the harness around it can be exercised end to end. It is not
a language model, and nothing it does is evidence of how Claude behaves.

What keeps it honest is that it is **stateless**: every decision is derived
from the request alone -- the case-facts system block if present, the tool
results still visible in the verbatim window, and the conversation summary.
If a fact is not in the request, the mock does not know it. With case facts
turned off it can only recall what the summary says, which is how the
`--no-case-facts` ablation measures drift.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from . import data
from .backend import ModelTurn
from .escalation import explicit_human_request
from .facts import parse_rendered
from .memory import SUMMARY_CLOSE, SUMMARY_OPEN

ORDER_RE = re.compile(r"\bORD-\d{4}\b", re.I)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[a-z]{2,}(?:\.[a-z]{2,})?", re.I)
POSTAL_RE = re.compile(r"\b\d{5}\b")
AMOUNT_RE = re.compile(r"\$\s?(\d[\d,]*(?:\.\d{2})?)")
MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"

ITEM_HINTS = ("espresso", "machine", "grinder", "burr", "frother", "kettle")

INTENTS: list[tuple[str, re.Pattern[str]]] = [
    ("identify", re.compile(r"[\w.+-]+@[\w-]+\.|\bcustomer id\b|\bC-\d{4}\b", re.I)),
    ("policy_gap", re.compile(r"\b(on sale|price (?:drop|match|adjust\w*)|the difference|compensat\w*|goodwill)\b", re.I)),
    ("policy_question", re.compile(r"\b(how long do i have|return window|return policy|refund policy|warranty)\b", re.I)),
    ("refund", re.compile(r"\b(refund|money back)\b", re.I)),
    ("recall_amount", re.compile(r"\b(how much|what did i pay|what was the total)\b", re.I)),
    ("recall_date", re.compile(r"\b(what date|when did i (?:order|buy|place))\b", re.I)),
    ("recall_email", re.compile(r"\b(which|what) email\b", re.I)),
    ("status", re.compile(r"\b(where is|status|track\w*|arrived|ship(?:ped)?|when will|look up|check)\b", re.I)),
    ("thanks", re.compile(r"\b(thanks|thank you|cheers)\b", re.I)),
    ("greeting", re.compile(r"^\s*(hi|hello|hey)\b", re.I)),
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
                view.nudged = i > turn_start
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


def _escalate(view: View, kn: Knowledge, reason: str, why: str) -> Call | Result:
    done = _last(view, "escalate_to_human", reason_category=reason)
    if done and not done.is_error:
        return done
    who = kn.customer["customer_id"] if kn.customer else "unverified customer"
    return Call("escalate_to_human", {
        "reason_category": reason,
        "summary": f"{why} Customer ({who}) wrote: \"{view.customer_text[:200]}\"",
    })


def _ticket_line(ticket: Result) -> str:
    p = ticket.payload
    return (f"I've passed this to a colleague (ticket {p['ticket_id']}, expected wait about "
            f"{p['estimated_wait_minutes']} minutes). They'll see everything we've covered, so you "
            "won't need to repeat yourself.")


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
            esc = _escalate(view, kn, "unable_to_progress",
                            f"lookup_order for {oid} failed twice ({r.payload.get('failureType')}).")
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


def h_refund(c: Concern, kn: Knowledge, view: View):
    if not kn.customer or not kn.customer.get("verified"):
        return ("I can help with that refund, but first I need to verify the account. Please send "
                "the email address and postal code on it.")
    oid = _resolve_order(c, kn, view)
    if isinstance(oid, Call):
        return oid
    if isinstance(oid, list):
        return _ask_which(oid, kn)
    if oid is None:
        return "Which order would you like refunded? The order number starts with ORD-."
    if oid not in kn.orders:
        if _needs(view, "lookup_order", order_id=oid):
            return Call("lookup_order", {"order_id": oid})
        return f"I couldn't load {oid}, so I can't refund it yet."
    order = kn.orders[oid]
    r = _last(view, "process_refund", order_id=oid)
    if r is None:
        if any(rf["order_id"] == oid for rf in kn.refunds):
            prior = next(rf for rf in kn.refunds if rf["order_id"] == oid)
            return f"{oid} was already refunded ({prior['refund_id']}, {_money(prior['amount'])})."
        stated = AMOUNT_RE.search(c.text)
        amount = float(stated.group(1).replace(",", "")) if stated else round(
            order["total"] - order.get("refunded_amount", 0.0), 2)
        return Call("process_refund", {"customer_id": kn.customer["customer_id"], "order_id": oid,
                                       "amount": amount, "reason": c.text[:120]})
    if not r.is_error:
        p = r.payload
        return (f"Done: refund {p['refund_id']} for {_money(p['amount'])} on {oid} is {p['status']}. "
                f"It should reach your original payment method within {p['settlement_eta_days']} business days.")
    if r.payload.get("errorCategory") == "policy":
        esc = _escalate(view, kn, "policy_limit", f"process_refund for {oid} rejected: {r.payload['description']}.")
        if isinstance(esc, Call):
            return esc
        return f"I can't approve that refund myself: {r.payload['description']}. {_ticket_line(esc)}"
    if r.payload.get("errorCategory") == "prerequisite":
        return "I need to verify the account before refunding. Please send the email address and postal code on it."
    return f"I couldn't process that refund: {r.payload['description']}."


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
    esc = _escalate(view, kn, "policy_gap", "Request not covered by the refund policy (price adjustment/compensation).")
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


def h_thanks(c, kn, view):
    return "You're welcome!"


def h_greeting(c, kn, view):
    return "Hi! Happy to help. What can I do for you?"


def h_other(c: Concern, kn: Knowledge, view: View):
    return ("I can help with orders, refunds and deliveries, but I don't have information on that, "
            "so I won't guess.")


HANDLERS = {
    "identify": h_identify, "status": h_status, "refund": h_refund,
    "policy_question": h_policy_question, "policy_gap": h_policy_gap,
    "recall_amount": h_recall_amount, "recall_date": h_recall_date, "recall_email": h_recall_email,
    "thanks": h_thanks, "greeting": h_greeting, "other": h_other,
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
        esc = _escalate(view, kn, "customer_request", "Customer explicitly asked for a human.")
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
