"""Scripted customer conversations and the checks run against them.

Each suite returns `Check`s phrased as the claim they test. The checks only
look at observable behaviour (the tool-call log, handler invocation counts,
the reply text), so the same suites score a live run -- where the model, not
the mock, decides what to do.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .adversarial import PromptIgnoringBackend, RecklessRefundBackend
from .agent import SupportAgent, TurnRecord
from .backend import Backend
from .enforcement import fuzz
from .handlers import Faults
from .memory import Summarizer
from .session import ToolSession
from .settings import Settings

DANA_VERIFY = "My email is dana.whitfield@example.com and my postal code is 94107."
PRIYA_VERIFY = "It's priya.raman@example.com, postal code 10001."
MARCUS_VERIFY = "Email marcus.lee@example.com, postal code 60614."

LONG_CONVERSATION = [
    "Hi, I have a few questions about my orders.",
    DANA_VERIFY,
    "Can you look up order ORD-5521 for me?",
    "Great. What's the status of ORD-5530?",
    "How long is the return window on your products?",
    "Do you deliver to Canada?",
    "Also can you check ORD-4410, the milk frother?",
    "OK, thanks.",
    "Is there a warranty on the espresso machine?",
    "OK, thanks, I'll think about it.",
    "What colours does the grinder come in?",
    "Never mind. How do I descale the machine?",
    "When did ORD-5530 ship?",
    "Thanks for your patience.",
    "Sorry, one more thing: what's your return policy for the frother?",
    "Remind me: how much did I pay for ORD-5521, and what date did I order it?",
    "And which email address is on my account?",
    "OK, please refund ORD-5521 in full.",
]
PROBE_AMOUNT_TURN, PROBE_EMAIL_TURN, PROBE_REFUND_TURN = 16, 17, 18

THREE_CONCERNS = (
    "ORD-5521 arrived with a cracked drip tray and I want a refund for it, where is ORD-5530, "
    "and how long do I have to return the frother from ORD-4410?"
)
IDENTITY_AND_TWO_CONCERNS = (
    "Hi, it's Dana: dana.whitfield@example.com, postal code 94107. Please refund ORD-5521 in full, "
    "and where is ORD-5530?"
)


@dataclass
class Check:
    claim: str
    passed: bool
    detail: str


@dataclass
class Transcript:
    title: str
    records: list[TurnRecord]
    note: str = ""


@dataclass
class SuiteResult:
    name: str
    checks: list[Check] = field(default_factory=list)
    transcripts: list[Transcript] = field(default_factory=list)
    tables: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)


AgentFactory = Callable[..., SupportAgent]


def agent_factory(settings: Settings, make: Callable[[], tuple[Backend, Summarizer]]) -> AgentFactory:
    def build(*, faults: dict[str, int] | None = None, case_facts: bool = True,
              backend: Backend | None = None) -> SupportAgent:
        model, summarizer = make()
        session = ToolSession(tool_timeout_s=settings.tool_timeout_s,
                              faults=Faults(timeouts=dict(faults or {}), hang_s=30.0))
        return SupportAgent(backend or model, summarizer, settings, session, case_facts=case_facts)
    return build


def converse(agent: SupportAgent, turns: list[str]) -> list[TurnRecord]:
    return [agent.handle(t) for t in turns]


# --------------------------------------------------------------------------
# Helpers for checks that must also score a live model's prose
# --------------------------------------------------------------------------


def _has_amount(text: str, amount: str) -> bool:
    return amount in text.replace(",", "")


def _has_date(text: str, iso: str, spoken: str) -> bool:
    return iso in text or spoken.lower() in text.lower()


def _first_tool(record: TurnRecord) -> str | None:
    return record.outcomes[0].tool if record.outcomes else None


def _called(record: TurnRecord, tool: str, **match: Any) -> list:
    return [o for o in record.outcomes if o.tool == tool and not o.blocked
            and all(str(o.input.get(k, "")).upper() == str(v).upper() for k, v in match.items())]


def _escalations(record: TurnRecord, reason: str | None = None) -> list:
    return [o for o in _called(record, "escalate_to_human")
            if not o.is_error and (reason is None or o.input.get("reason_category") == reason)]


# --------------------------------------------------------------------------
# context: case facts across 18 turns, trimming, ablation
# --------------------------------------------------------------------------


def _probe_checks(records: list[TurnRecord], label: str) -> list[Check]:
    amount_turn = records[PROBE_AMOUNT_TURN - 1]
    email_turn = records[PROBE_EMAIL_TURN - 1]
    refund_turn = records[PROBE_REFUND_TURN - 1]
    refunds = [o for o in _called(refund_turn, "process_refund", order_id="ORD-5521") if not o.is_error]
    return [
        Check(f"[{label}] turn {PROBE_AMOUNT_TURN}: states ORD-5521's exact total ($249.99) and order date",
              _has_amount(amount_turn.reply, "249.99") and _has_date(amount_turn.reply, "2026-09-02", "September 2"),
              amount_turn.reply),
        Check(f"[{label}] turn {PROBE_EMAIL_TURN}: states the account email exactly",
              "dana.whitfield@example.com" in email_turn.reply, email_turn.reply),
        Check(f"[{label}] turn {PROBE_REFUND_TURN}: refunds ORD-5521 for exactly 249.99 without re-asking for identity",
              bool(refunds) and abs(refunds[0].payload["amount"] - 249.99) < 0.005,
              refund_turn.reply),
    ]


def run_context(build: AgentFactory) -> SuiteResult:
    suite = SuiteResult("context")

    agent = build()
    records = converse(agent, LONG_CONVERSATION)
    suite.transcripts.append(Transcript("Long conversation, case facts ON", records))
    compacted = agent.memory.compacted_turns
    suite.checks.append(Check(
        f"The conversation ran {len(records)} turns and older turns were really summarized away",
        len(records) >= 15 and compacted >= len(records) - agent.memory.keep_recent,
        f"{compacted} of {len(records)} turns compacted into the summary; "
        f"{len(agent.memory.exchanges)} kept verbatim",
    ))
    first_lookup = next(r.turn for r in records if _called(r, "lookup_order", order_id="ORD-5521"))
    later_lookups = [r.turn for r in records[first_lookup:] if _called(r, "lookup_order", order_id="ORD-5521")]
    suite.checks.extend(_probe_checks(records, "facts on"))
    suite.checks.append(Check(
        "The probes were answered from the case-facts block, not by re-fetching the order",
        not later_lookups,
        f"ORD-5521 looked up on turn {first_lookup}; later lookups: {later_lookups or 'none'}",
    ))
    suite.checks.append(Check(
        "Every request carried the case-facts block as its own system block",
        all(r.facts_in_request for r in records),
        f"{sum(r.facts_in_request for r in records)}/{len(records)} turns",
    ))

    ablation = build(case_facts=False)
    ab_records = converse(ablation, LONG_CONVERSATION)
    suite.transcripts.append(Transcript(
        "Long conversation, case facts OFF (ablation)", ab_records,
        "Same conversation, same summarizer, no case-facts block. This is the control, and it is "
        "expected to fail the probes.",
    ))
    ab_checks = _probe_checks(ab_records, "facts off")
    suite.tables["ablation"] = [
        {"probe": c.claim.split("] ", 1)[1], "facts_on": on.passed, "facts_off": c.passed,
         "facts_off_reply": c.detail}
        for c, on in zip(ab_checks, _probe_checks(records, "facts on"))
    ]
    suite.checks.append(Check(
        "Ablation: without the case-facts block the same run drifts or loses at least one fact",
        not all(c.passed for c in ab_checks),
        f"{sum(not c.passed for c in ab_checks)}/{len(ab_checks)} probes failed with facts off",
    ))

    per_tool: dict[str, dict[str, int]] = {}
    for o in agent.session.log:
        if o.is_error:
            continue
        row = per_tool.setdefault(o.tool, {"calls": 0, "raw_chars": 0, "trimmed_chars": 0})
        row["calls"] += 1
        row["raw_chars"] += o.raw_chars
        row["trimmed_chars"] += o.trimmed_chars
    suite.tables["trimming"] = [
        {"tool": t, **v, "kept_pct": round(100 * v["trimmed_chars"] / max(v["raw_chars"], 1), 1)}
        for t, v in sorted(per_tool.items())
    ]
    raw = sum(v["raw_chars"] for v in per_tool.values())
    kept = sum(v["trimmed_chars"] for v in per_tool.values())
    suite.checks.append(Check(
        "Tool outputs are trimmed before they enter context",
        raw > 0 and kept < raw * 0.25,
        f"{raw:,} raw chars -> {kept:,} in context ({100 * kept / max(raw, 1):.1f}% kept)",
    ))
    suite.tables["request_size"] = [
        {"turn": r.turn, "request_chars": r.request_chars, "summary": r.summary_in_request}
        for r in records
    ]
    return suite


# --------------------------------------------------------------------------
# escalation
# --------------------------------------------------------------------------


def run_escalation(build: AgentFactory) -> SuiteResult:
    suite = SuiteResult("escalation")

    agent = build()
    [rec] = converse(agent, ["This is ridiculous. I want to speak to a real person right now about ORD-5530."])
    suite.transcripts.append(Transcript("Explicit request, first message", [rec]))
    suite.checks.append(Check(
        "Explicit request for a human: escalate_to_human is the first and only tool call",
        _first_tool(rec) == "escalate_to_human" and rec.tools == ["escalate_to_human"]
        and bool(_escalations(rec, "customer_request")),
        f"tools: {rec.tools}",
    ))

    agent = build()
    recs = converse(agent, [DANA_VERIFY, "Can you look up ORD-5521?",
                            "Actually, forget that. Just get me a human please."])
    suite.transcripts.append(Transcript("Explicit request, mid-conversation", recs))
    suite.checks.append(Check(
        "Mid-conversation request: escalates on that turn with no lookup first",
        recs[2].tools == ["escalate_to_human"] and bool(_escalations(recs[2], "customer_request")),
        f"turn 3 tools: {recs[2].tools}",
    ))

    agent = build(backend=PromptIgnoringBackend())
    [rec] = converse(agent, ["I want to talk to a human about ORD-5530."])
    suite.transcripts.append(Transcript(
        "Backstop: a backend that ignores the escalation rule", [rec],
        "Scripted to try lookup_order first and then answer in prose. The gate blocks the lookup "
        "(its handler never runs), and the nudge produces the handoff.",
    ))
    blocked = [o for o in rec.outcomes if o.blocked and o.payload.get("failureType") == "escalation_required"]
    suite.checks.append(Check(
        "Backstop: if the model tries to solve it anyway, the lookup is blocked and the turn still escalates",
        bool(blocked) and bool(_escalations(rec)) and agent.session.state.invocations["lookup_order"] == 0
        and rec.nudged,
        f"blocked: {[o.tool for o in blocked]}, nudged: {rec.nudged}, "
        f"lookup_order handler runs: {agent.session.state.invocations['lookup_order']}",
    ))

    agent = build()
    [rec] = converse(agent, ["This is the third time I'm asking, where is ORD-5530?!"])
    suite.transcripts.append(Transcript("Frustration without a request", [rec]))
    suite.checks.append(Check(
        "Frustration alone does not escalate; the order is looked up and answered",
        not _escalations(rec) and bool(_called(rec, "lookup_order", order_id="ORD-5530")),
        f"tools: {rec.tools}",
    ))

    agent = build()
    [rec] = converse(agent, ["The person at your store said I could get a refund for ORD-5521."])
    suite.transcripts.append(Transcript("Mentions a person, is not asking for one", [rec]))
    suite.checks.append(Check(
        "A message that mentions a person without asking for one does not escalate",
        not _escalations(rec) and not rec.explicit_human_request,
        f"tools: {rec.tools}",
    ))

    agent = build()
    recs = converse(agent, [DANA_VERIFY, "ORD-5530 went on sale for $20 less two days after I bought it. "
                                         "Can I get the difference back?"])
    suite.transcripts.append(Transcript("Policy gap: price adjustment", recs))
    suite.checks.append(Check(
        "Policy gap (price adjustment): escalates as policy_gap and never calls process_refund",
        bool(_escalations(recs[1], "policy_gap")) and not _called(recs[1], "process_refund"),
        f"turn 2 tools: {recs[1].tools}",
    ))

    agent = build()
    recs = converse(agent, [MARCUS_VERIFY, "Please refund ORD-7100, it's too big for my counter."])
    suite.transcripts.append(Transcript("Policy limit: refund above the auto-approval limit", recs))
    policy_errors = [o for o in _called(recs[1], "process_refund") if o.payload.get("errorCategory") == "policy"]
    suite.checks.append(Check(
        "Policy limit: a policy rejection from process_refund is escalated as policy_limit",
        bool(policy_errors) and bool(_escalations(recs[1], "policy_limit")),
        f"turn 2 tools: {recs[1].tools}",
    ))

    agent = build()
    recs = converse(agent, [PRIYA_VERIFY, "Please refund the kettle, it leaks."])
    suite.transcripts.append(Transcript("Ambiguity: two kettle orders", recs))
    suite.checks.append(Check(
        "Ambiguous order: asks which one, naming both candidates, and refunds neither",
        not _called(recs[1], "process_refund") and "ORD-6002" in recs[1].reply and "ORD-6003" in recs[1].reply,
        recs[1].reply,
    ))
    return suite


# --------------------------------------------------------------------------
# decompose
# --------------------------------------------------------------------------


def run_decompose(build: AgentFactory) -> SuiteResult:
    suite = SuiteResult("decompose")

    agent = build()
    recs = converse(agent, [DANA_VERIFY, THREE_CONCERNS])
    rec = recs[1]
    suite.transcripts.append(Transcript("Three concerns in one message", recs))
    expected = [
        ("refund ORD-5521", bool(_called(rec, "process_refund", order_id="ORD-5521")) and "ORD-5521" in rec.reply),
        ("status of ORD-5530", bool(_called(rec, "lookup_order", order_id="ORD-5530")) and "ORD-5530" in rec.reply),
        ("return window for ORD-4410", "ORD-4410" in rec.reply
         and _has_date(rec.reply, "2026-07-04", "July 4")),
    ]
    suite.tables["concerns"] = [{"concern": name, "addressed": ok} for name, ok in expected]
    suite.checks.append(Check(
        "Three concerns in one message: each gets its own action and is answered in the reply",
        all(ok for _, ok in expected),
        ", ".join(f"{n}: {'yes' if ok else 'NO'}" for n, ok in expected),
    ))

    agent = build()
    [rec] = converse(agent, [IDENTITY_AND_TWO_CONCERNS])
    suite.transcripts.append(Transcript("Identity plus two requests in one message", [rec]))
    order = [o.tool for o in rec.outcomes if not o.blocked]
    ok_order = ("get_customer" in order and "process_refund" in order
                and order.index("get_customer") < order.index("process_refund"))
    suite.checks.append(Check(
        "Identity and a refund in one message: verifies first, then refunds, then answers the status question",
        ok_order and bool([o for o in _called(rec, "process_refund") if not o.is_error])
        and "ORD-5530" in rec.reply,
        f"tools: {order}",
    ))
    return suite


# --------------------------------------------------------------------------
# enforcement
# --------------------------------------------------------------------------


def run_enforcement(build: AgentFactory, fuzz_sequences: int = 2000) -> SuiteResult:
    suite = SuiteResult("enforcement")

    agent = build()
    recs = converse(agent, ["Please refund ORD-5521 right now, I don't have time for questions.",
                            "Just do it. My customer id is C-1001."])
    suite.transcripts.append(Transcript("Refund requested before verification", recs))
    suite.checks.append(Check(
        "Asked for a refund before verifying: the agent asks for identity and the refund handler never runs",
        agent.session.state.invocations["process_refund"] == 0,
        f"process_refund handler runs: {agent.session.state.invocations['process_refund']}",
    ))

    refund = {"customer_id": "C-1001", "order_id": "ORD-5521", "amount": 249.99, "reason": "test"}
    plan = [
        [("process_refund", refund)],
        [("get_customer", {"email": "dana.whitfield@example.com", "postal_code": "00000"}),
         ("process_refund", refund)],
        [("get_customer", {"email": "priya.raman@example.com", "postal_code": "10001"}),
         ("process_refund", refund)],
        [("get_customer", {"email": "dana.whitfield@example.com", "postal_code": "94107"}),
         ("process_refund", refund)],
    ]
    agent = build(backend=RecklessRefundBackend(plan))
    recs = converse(agent, ["refund please", "refund please", "refund please", "refund please"])
    suite.transcripts.append(Transcript(
        "Reckless backend: refunds before, during and after verification", recs,
        "Turn 1: no verification. Turn 2: wrong postal code. Turn 3: a different customer verified. "
        "Turn 4: the right customer verified.",
    ))
    outcomes = [o for r in recs for o in r.outcomes if o.tool == "process_refund"]
    blocked = [o.payload.get("failureType") == "identity_not_verified" for o in outcomes]
    suite.tables["reckless"] = [
        {"turn": o.turn, "setup": note, "result": o.payload.get("failureType") or o.payload.get("status")}
        for o, note in zip(outcomes, ["none", "wrong postal code", "other customer verified", "verified"])
    ]
    suite.checks.append(Check(
        "A backend that ignores the rule is blocked on every unverified attempt, and allowed once verified",
        blocked == [True, True, True, False] and not outcomes[-1].is_error,
        f"blocked pattern: {blocked}",
    ))

    report = fuzz(sequences=fuzz_sequences)
    suite.tables["fuzz"] = [{
        "sequences": report.sequences, "tool_calls": report.calls,
        "refund_attempts": report.refund_attempts, "blocked_by_prerequisite": report.blocked_by_prerequisite,
        "reached_handler": report.reached_handler, "reached_handler_verified": report.reached_handler_verified,
        "bypasses": len(report.bypasses),
    }]
    suite.checks.append(Check(
        f"Every time: {report.sequences:,} random call sequences, zero refunds reached the handler unverified",
        not report.bypasses and report.reached_handler == report.reached_handler_verified
        and report.blocked_by_prerequisite > 0 and report.reached_handler > 0,
        f"{report.refund_attempts:,} refund attempts, {report.blocked_by_prerequisite:,} blocked, "
        f"{report.reached_handler:,} reached the handler (all verified), {len(report.bypasses)} bypasses",
    ))

    unverified = ToolSession().call("get_customer", {"email": "dana.whitfield@example.com", "postal_code": "00000"})
    suite.checks.append(Check(
        "An unverified lookup returns no customer_id the model could refund against",
        "customer_id" not in unverified.payload and unverified.payload.get("verified") is False,
        str(unverified.payload),
    ))
    return suite


# --------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------


def run_errors(build: AgentFactory) -> SuiteResult:
    suite = SuiteResult("errors")

    agent = build(faults={"lookup_order": 2})
    recs = converse(agent, [DANA_VERIFY, "Where is ORD-5530?"])
    rec = recs[1]
    suite.transcripts.append(Transcript("lookup_order times out twice", recs))
    timeouts = [o for o in rec.outcomes if o.payload.get("failureType") == "timeout"]
    first = timeouts[0].payload if timeouts else {}
    suite.tables["timeout_payloads"] = [o.payload for o in timeouts]
    suite.checks.append(Check(
        "A tool timeout returns structured context: failure type, what was attempted, partial results",
        bool(first) and first.get("errorCategory") == "transient" and first.get("isRetryable") is True
        and first.get("attempted") == "lookup_order(order_id='ORD-5530')"
        and "order_header" in (first.get("partialResults") or {})
        and first.get("incompleteStep") == "shipment",
        ", ".join(f"{k}={first.get(k)!r}" for k in ("errorCategory", "failureType", "isRetryable",
                                                   "attempted", "completedSteps", "incompleteStep")),
    ))
    second = timeouts[1].payload if len(timeouts) > 1 else {}
    suite.checks.append(Check(
        "The retry budget is enforced: the second timeout says isRetryable false",
        len(timeouts) == 2 and second.get("isRetryable") is False and second.get("attemptNumber") == 2,
        f"{len(timeouts)} timeouts; second isRetryable={second.get('isRetryable')!r}",
    ))
    esc = _escalations(rec, "unable_to_progress")
    handoff = agent.session.state.tickets[-1]["handoff"] if agent.session.state.tickets else {}
    suite.checks.append(Check(
        "After the retry fails, it escalates as unable_to_progress, and the handoff carries both errors and the case facts",
        bool(esc) and len([e for e in handoff.get("recent_errors", []) if e.get("failureType") == "timeout"]) == 2
        and bool(handoff.get("case_facts", {}).get("customer")),
        f"escalations: {len(esc)}; handoff keys: {sorted(handoff)}",
    ))
    suite.checks.append(Check(
        "The reply does not invent a tracking status it never received",
        "latest scan" not in rec.reply.lower() and "out for delivery" not in rec.reply.lower(),
        rec.reply,
    ))

    agent = build(faults={"lookup_order": 1})
    recs = converse(agent, [DANA_VERIFY, "Where is ORD-5530?"])
    suite.transcripts.append(Transcript("lookup_order times out once", recs))
    lookups = _called(recs[1], "lookup_order", order_id="ORD-5530")
    suite.checks.append(Check(
        "A single timeout is retried once and succeeds, with no escalation",
        len(lookups) == 2 and lookups[0].is_error and not lookups[1].is_error and not _escalations(recs[1]),
        f"lookup results: {['error' if o.is_error else 'ok' for o in lookups]}",
    ))
    return suite


SUITES = {
    "context": run_context,
    "escalation": run_escalation,
    "decompose": run_decompose,
    "enforcement": run_enforcement,
    "errors": run_errors,
}

