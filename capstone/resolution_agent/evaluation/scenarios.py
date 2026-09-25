"""The test conversation set: one named scenario per behaviour the agent must show.

`/run-scenarios` (and `main.py scenarios`) runs these and reports PASS/FAIL
per scenario. Each scenario is a scripted conversation plus checks phrased
as the claim they test. The checks only look at observable behaviour -- the
tool-call log, handler invocation counts, the ledger, ticket contents, the
reply text -- so the same scenarios score a live run, where the model, not
the mock, decides what to do.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from ..tools.actions import generic_text
from ..backends.adversarial import PromptIgnoringBackend
from ..conversation.agent import SupportAgent, TurnRecord
from ..backends.backend import Backend
from ..tools.handlers import Faults
from ..conversation.memory import Summarizer
from .results import Check, SuiteResult, Transcript
from ..tools.schema import validate_input
from ..tools.session import ToolSession
from ..config.settings import Settings

DANA_VERIFY = "My email is dana.whitfield@example.com and my postal code is 94107."
PRIYA_VERIFY = "It's priya.raman@example.com, postal code 10001."
MARCUS_VERIFY = "Email marcus.lee@example.com, postal code 60614."

THREE_CONCERNS = (
    "ORD-5521 arrived with a cracked drip tray and I want a refund for it, where is ORD-5530, "
    "and how long do I have to return the frother from ORD-4410?"
)

# 22 turns, three unrelated issues in sequence, then probes of facts recorded at turn 2.
THREE_ISSUES = [
    "Hi, I've got a few unrelated things to sort out today.",
    DANA_VERIFY + " Can you look up ORD-5521, the espresso machine?",
    # Issue 1: a damaged item, partial refund.
    "ORD-5521 arrived with a cracked drip tray.",
    "Please give me a partial refund of $30 for the cracked drip tray on ORD-5521.",
    "Thanks, that's great.",
    "How long is the return window on your products?",
    # Issue 2: a billing question on a different order.
    "Next thing, unrelated: why was I charged $89.50 for ORD-5530 when the burr set costs $82?",
    "Ah, shipping, OK. Where is ORD-5530 now?",
    "OK.",
    "Is there a warranty on the espresso machine?",
    "OK, thanks.",
    # Issue 3: an account change the agent has no tool for.
    "Last thing, and it's unrelated to the orders: I need to change the email address on my account.",
    "Will the new address be used for receipts too?",
    "Thanks for your patience.",
    "Do you deliver to Canada?",
    "What colours does the grinder come in?",
    "Never mind. How do I descale the machine?",
    "OK.",
    "Sorry, one more thing: what's your return policy for the frother?",
    # Probes of facts first recorded at turn 2 (and the refund from turn 4).
    "Before I go: how much did I pay for ORD-5521, and what date did I order it?",
    "Which email address is on my account right now?",
    "And what's the refund reference for the drip tray?",
]
FACTS_TURN = 2
PROBE_AMOUNT_TURN, PROBE_EMAIL_TURN, PROBE_REFUND_TURN = 20, 21, 22

AgentFactory = Callable[..., SupportAgent]


def agent_factory(settings: Settings, make: Callable[[], tuple[Backend, Summarizer]]) -> AgentFactory:
    def build(*, faults: dict[str, int] | None = None, case_facts: bool = True,
              backend: Backend | None = None) -> SupportAgent:
        model, summarizer = make()
        session = ToolSession(tool_timeout_s=settings.tool_timeout_s,
                              faults=Faults(timeouts=dict(faults or {}), hang_s=30.0),
                              max_action_attempts=settings.max_action_attempts)
        return SupportAgent(backend or model, summarizer, settings, session, case_facts=case_facts)
    return build


def converse(agent: SupportAgent, turns: list[str]) -> list[TurnRecord]:
    return [agent.handle(t) for t in turns]


# --------------------------------------------------------------------------
# Helpers for checks that must also score a live model's prose
# --------------------------------------------------------------------------


def has_amount(text: str, amount: str) -> bool:
    return amount in text.replace(",", "")


def has_date(text: str, iso: str, spoken: str) -> bool:
    return iso in text or spoken.lower() in text.lower()


def called(record: TurnRecord, tool: str, **match: Any) -> list:
    return [o for o in record.outcomes if o.tool == tool and not o.blocked
            and all(str(o.input.get(k, "")).upper() == str(v).upper() for k, v in match.items())]


def escalations(record: TurnRecord, reason: str | None = None) -> list:
    return [o for o in called(record, "escalate_to_human")
            if not o.is_error and (reason is None or o.input.get("reason_category") == reason)]


def refunds_ok(record: TurnRecord, **match: Any) -> list:
    return [o for o in called(record, "process_refund", **match) if not o.is_error]


def ticket(agent: SupportAgent, ticket_id: str | None = None) -> dict[str, Any]:
    tickets = agent.session.state.tickets
    if ticket_id:
        return next((t for t in tickets if t["id"] == ticket_id), {})
    return tickets[-1] if tickets else {}


def handoff_is_actionable(t: dict[str, Any], customer_id: str | None) -> tuple[bool, str]:
    rec = t.get("handoff_record") or {}
    ok = (rec.get("customer_id") == customer_id and bool(rec.get("root_cause"))
          and not generic_text(rec.get("root_cause", "")) and not generic_text(rec.get("recommended_action", ""))
          and "case_facts" in (t.get("handoff") or {}))
    return ok, (f"customer_id={rec.get('customer_id')!r}; root_cause={rec.get('root_cause')!r}; "
                f"recommended_action={rec.get('recommended_action')!r}")


def ledger_ok(agent: SupportAgent) -> tuple[bool, str]:
    runs = sum(agent.session.state.invocations[t] for t in ("process_refund", "escalate_to_human"))
    invalid = [r.action_id for r in agent.session.ledger if validate_input(r.tool, r.record)]
    return runs == len(agent.session.ledger) and not invalid, (
        f"{runs} action handler run(s), {len(agent.session.ledger)} ledger record(s), {len(invalid)} invalid")


# --------------------------------------------------------------------------
# Scenarios
# --------------------------------------------------------------------------

Outcome = tuple[list[Check], list[Transcript]]


@dataclass(frozen=True)
class Scenario:
    name: str
    domain: str
    title: str
    run: Callable[[AgentFactory], Outcome]


def s_refund_happy_path(build: AgentFactory) -> Outcome:
    agent = build()
    recs = converse(agent, [DANA_VERIFY, "ORD-5521 stopped working. Please refund it in full."])
    ok = refunds_ok(recs[1], order_id="ORD-5521")
    rec = agent.session.ledger[-1] if agent.session.ledger else None
    good_ledger, detail = ledger_ok(agent)
    return [
        Check("Verifies, fetches the order, then refunds exactly 249.99 as a full refund",
              bool(ok) and ok[0].input.get("amount") == 249.99 and ok[0].input.get("refund_type") == "full"
              and recs[1].tools.index("lookup_order") < recs[1].tools.index("process_refund"),
              f"turn 2 tools: {recs[1].tools}"),
        Check("The refund left a validated ledger record with a reason code and justification",
              good_ledger and rec is not None and rec.status == "executed"
              and rec.record.get("reason_code") == "defective" and bool(rec.record.get("justification")),
              detail + (f"; {rec.action_id} reason_code={rec.record.get('reason_code')!r}" if rec else "")),
    ], [Transcript("Refund, happy path", recs)]


def s_refund_before_verification(build: AgentFactory) -> Outcome:
    agent = build()
    recs = converse(agent, ["Please refund ORD-5521 right now, I don't have time for questions.",
                            "Just do it. My customer id is C-1001."])
    runs = agent.session.state.invocations["process_refund"]
    return [Check("Asked for a refund before verifying: asks for identity; the refund handler never runs",
                  runs == 0 and all("postal code" in r.reply for r in recs),
                  f"process_refund handler runs: {runs}")], [Transcript("Refund before verification", recs)]


def s_explicit_human_first(build: AgentFactory) -> Outcome:
    agent = build()
    [rec] = converse(agent, ["This is ridiculous. I want to speak to a real person right now about ORD-5530."])
    ok, detail = handoff_is_actionable(ticket(agent), None)
    return [
        Check("Explicit request for a human: escalate_to_human is the first and only tool call",
              rec.tools == ["escalate_to_human"] and bool(escalations(rec, "customer_request"))
              and agent.session.state.invocations["lookup_order"] == 0, f"tools: {rec.tools}"),
        Check("The handoff says nobody is verified (customer_id null) and still gives a root cause and next step",
              ok and ticket(agent)["handoff_record"]["identity_verified"] is False, detail),
    ], [Transcript("Explicit request, first message", [rec])]


def s_explicit_human_mid(build: AgentFactory) -> Outcome:
    agent = build()
    recs = converse(agent, [DANA_VERIFY, "Can you look up ORD-5521?",
                            "Actually, forget that. Just get me a human please."])
    ok, detail = handoff_is_actionable(ticket(agent), "C-1001")
    return [
        Check("Mid-conversation request: escalates on that turn with no other tool first",
              recs[2].tools == ["escalate_to_human"] and bool(escalations(recs[2], "customer_request")),
              f"turn 3 tools: {recs[2].tools}"),
        Check("The handoff carries the verified customer id, a root cause and a recommended action", ok, detail),
    ], [Transcript("Explicit request, mid-conversation", recs)]


def s_backstop(build: AgentFactory) -> Outcome:
    agent = build(backend=PromptIgnoringBackend())
    [rec] = converse(agent, ["I want to talk to a human about ORD-5530."])
    stubborn = build(backend=PromptIgnoringBackend(stubborn=True))
    [rec2] = converse(stubborn, ["I want to talk to a human about ORD-5530."])
    return [
        Check("A backend that tries to solve it anyway is blocked, nudged, and escalates",
              any(o.blocked for o in rec.outcomes) and rec.nudged and bool(escalations(rec, "customer_request"))
              and agent.session.state.invocations["lookup_order"] == 0,
              f"tools: {rec.tools}, nudged: {rec.nudged}"),
        Check("A backend that ignores the nudge too still produces a ticket: the program files the handoff",
              rec2.system_handoff and bool(escalations(rec2, "customer_request")) and "HT-" in rec2.reply,
              f"stop: {rec2.stop}, system_handoff: {rec2.system_handoff}"),
    ], [Transcript("Backstop: prompt-ignoring backend", [rec]),
        Transcript("Backstop: backend that also ignores the nudge", [rec2])]


def s_frustration(build: AgentFactory) -> Outcome:
    agent = build()
    [rec] = converse(agent, ["This is the third time I'm asking, where is ORD-5530?!"])
    agent2 = build()
    [rec2] = converse(agent2, ["The person at your store said I could get a refund for ORD-5521."])
    return [
        Check("Frustration alone does not escalate; the order is looked up and answered",
              not escalations(rec) and bool(called(rec, "lookup_order", order_id="ORD-5530")), f"tools: {rec.tools}"),
        Check("Mentioning a person is not a request for one",
              not rec2.explicit_human_request and not escalations(rec2), f"tools: {rec2.tools}"),
    ], [Transcript("Frustration without a request", [rec]), Transcript("Mentions a person", [rec2])]


def s_partial_refund_for_damage(build: AgentFactory) -> Outcome:
    agent = build()
    recs = converse(agent, [DANA_VERIFY, "ORD-5521 arrived with a cracked drip tray.",
                            "Please give me a partial refund of $30 for the cracked drip tray on ORD-5521."])
    ok = refunds_ok(recs[2], order_id="ORD-5521")
    return [
        Check("A damage report with no amount gets a question (whole order or part), not a guessed refund",
              not called(recs[1], "process_refund") and "which would you prefer" in recs[1].reply.lower(),
              recs[1].reply),
        Check("With an amount stated: a partial refund of exactly 30.00, reason_code damaged_item",
              bool(ok) and ok[0].input.get("amount") == 30.0 and ok[0].input.get("refund_type") == "partial"
              and ok[0].input.get("reason_code") == "damaged_item",
              str({k: ok[0].input.get(k) for k in ("amount", "refund_type", "reason_code")}) if ok else "no refund"),
    ], [Transcript("Damaged part: ask, then partial refund", recs)]


def s_price_drop(build: AgentFactory) -> Outcome:
    agent = build()
    recs = converse(agent, [DANA_VERIFY, "ORD-5530 went on sale for $20 less two days after I bought it. "
                                         "Can I get the difference back?"])
    ok, detail = handoff_is_actionable(ticket(agent), "C-1001")
    return [Check("Price adjustment: escalates as policy_gap with an actionable handoff, never refunds",
                  bool(escalations(recs[1], "policy_gap")) and not called(recs[1], "process_refund") and ok,
                  detail)], [Transcript("Price drop (policy gap)", recs)]


def s_duplicate_charge(build: AgentFactory) -> Outcome:
    agent = build()
    recs = converse(agent, [DANA_VERIFY, "My bank shows two charges for ORD-5530, I was charged twice!"])
    t = ticket(agent)
    rec = t.get("handoff_record") or {}
    return [Check(
        "Duplicate-charge claim the records don't show: no refund; escalates unable_to_progress with what to check",
        not called(recs[1], "process_refund") and bool(escalations(recs[1], "unable_to_progress"))
        and rec.get("customer_id") == "C-1001" and "ORD-5530" in rec.get("root_cause", "")
        and "processor" in rec.get("recommended_action", ""),
        f"tools: {recs[1].tools}; recommended_action={rec.get('recommended_action')!r}",
    )], [Transcript("Duplicate charge dispute", recs)]


def s_ambiguous_order(build: AgentFactory) -> Outcome:
    agent = build()
    recs = converse(agent, [PRIYA_VERIFY, "Please refund the kettle, it leaks."])
    return [Check("Ambiguous order: asks which one, naming both candidates, and refunds neither",
                  not called(recs[1], "process_refund") and "ORD-6002" in recs[1].reply
                  and "ORD-6003" in recs[1].reply, recs[1].reply)], [Transcript("Two kettles", recs)]


def s_policy_rejections(build: AgentFactory) -> Outcome:
    agent = build()
    recs = converse(agent, [DANA_VERIFY, "The frother from ORD-4410 is defective, please refund it."])
    window = [r for r in agent.session.ledger if r.tool == "process_refund"]
    agent2 = build()
    recs2 = converse(agent2, [MARCUS_VERIFY, "Please refund ORD-7100, it's too big for my counter."])
    handoff = ticket(agent2).get("handoff", {})
    return [
        Check("Outside the refund window: the validated record is logged as not executed, no money moves, "
              "and it escalates as policy_limit",
              bool(window) and window[0].status == "not_executed: outside_refund_window"
              and agent.session.state.refunded["ORD-4410"] == 0.0 and bool(escalations(recs[1], "policy_limit")),
              f"ledger: {[r.status for r in window]}; tools: {recs[1].tools}"),
        Check("Above the auto-approval limit: escalates as policy_limit with the rejection in the handoff",
              bool(escalations(recs2[1], "policy_limit"))
              and any(e.get("failureType") == "above_auto_refund_limit" for e in handoff.get("recent_errors", [])),
              f"tools: {recs2[1].tools}"),
    ], [Transcript("Outside the refund window", recs), Transcript("Above the auto-approval limit", recs2)]


def s_account_change(build: AgentFactory) -> Outcome:
    agent = build()
    recs = converse(agent, [DANA_VERIFY, "I need to change the email address on my account."])
    rec = ticket(agent).get("handoff_record") or {}
    tools_offered = sorted(t["name"] for t in agent.build_request([])["tools"])
    return [
        Check("Account change: no tool outside the four exists or is called; it goes to a person as policy_gap",
              bool(escalations(recs[1], "policy_gap")) and set(recs[1].tools) <= {"escalate_to_human"}
              and tools_offered == ["escalate_to_human", "get_customer", "lookup_order", "process_refund"],
              f"tools offered: {tools_offered}; called: {recs[1].tools}"),
        Check("The handoff tells the account team to re-verify identity before changing anything",
              rec.get("customer_id") == "C-1001" and "re-verify" in rec.get("recommended_action", "").lower(),
              repr(rec.get("recommended_action"))),
    ], [Transcript("Account change request", recs)]


def s_three_concerns(build: AgentFactory) -> Outcome:
    agent = build()
    recs = converse(agent, [DANA_VERIFY, THREE_CONCERNS])
    rec = recs[1]
    expected = [
        ("refund ORD-5521", bool(refunds_ok(rec, order_id="ORD-5521")) and "ORD-5521" in rec.reply),
        ("status of ORD-5530", bool(called(rec, "lookup_order", order_id="ORD-5530")) and "ORD-5530" in rec.reply),
        ("return window for ORD-4410", "ORD-4410" in rec.reply and has_date(rec.reply, "2026-07-04", "July 4")),
    ]
    return [Check("Three concerns in one message: each gets its own action and is answered",
                  all(ok for _, ok in expected), ", ".join(f"{n}: {'yes' if ok else 'NO'}" for n, ok in expected))], \
        [Transcript("Three concerns in one message", recs)]


def s_tool_timeout(build: AgentFactory) -> Outcome:
    agent = build(faults={"lookup_order": 2})
    recs = converse(agent, [DANA_VERIFY, "Where is ORD-5530?"])
    timeouts = [o.payload for o in recs[1].outcomes if o.payload.get("failureType") == "timeout"]
    first = timeouts[0] if timeouts else {}
    handoff = ticket(agent).get("handoff", {})
    return [
        Check("A timeout returns errorCategory, isRetryable, description, what was attempted and partial results",
              first.get("errorCategory") == "transient" and first.get("isRetryable") is True
              and bool(first.get("description")) and first.get("attempted") == "lookup_order(order_id='ORD-5530')"
              and "order_header" in (first.get("partialResults") or {}),
              ", ".join(f"{k}={first.get(k)!r}" for k in ("errorCategory", "isRetryable", "attempted"))),
        Check("The second timeout is non-retryable; it escalates with both errors and the case facts attached",
              len(timeouts) == 2 and timeouts[1].get("isRetryable") is False
              and bool(escalations(recs[1], "unable_to_progress"))
              and [e.get("failureType") for e in handoff.get("recent_errors", [])][-2:] == ["timeout", "timeout"],
              f"{len(timeouts)} timeouts; escalated: {bool(escalations(recs[1]))}"),
    ], [Transcript("lookup_order times out twice", recs)]


def facts_drift(records: list[TurnRecord]) -> list[dict[str, Any]]:
    """Compare every later request's facts block with what turn 2 recorded."""
    base = records[FACTS_TURN - 1].facts_seen or {}
    base_customer = base.get("customer") or {}
    base_order = (base.get("orders") or {}).get("ORD-5521") or {}
    stable = [k for k in base_order if k != "refunded_amount"]
    rows = []
    for r in records[FACTS_TURN:]:
        seen = r.facts_seen or {}
        order = (seen.get("orders") or {}).get("ORD-5521") or {}
        rows.append({
            "turn": r.turn,
            "customer_unchanged": bool(base_customer) and seen.get("customer") == base_customer,
            "order_unchanged": bool(base_order) and all(order.get(k) == base_order[k] for k in stable),
            "total": order.get("total"), "placed_on": order.get("placed_on"),
            "email": (seen.get("customer") or {}).get("email"),
        })
    return rows


def probe_checks(records: list[TurnRecord], label: str) -> list[Check]:
    amount, email, refund = (records[t - 1] for t in (PROBE_AMOUNT_TURN, PROBE_EMAIL_TURN, PROBE_REFUND_TURN))
    return [
        Check(f"[{label}] turn {PROBE_AMOUNT_TURN}: ORD-5521's exact total ($249.99) and order date (2026-09-02)",
              has_amount(amount.reply, "249.99") and has_date(amount.reply, "2026-09-02", "September 2"), amount.reply),
        Check(f"[{label}] turn {PROBE_EMAIL_TURN}: the account email exactly",
              "dana.whitfield@example.com" in email.reply, email.reply),
        Check(f"[{label}] turn {PROBE_REFUND_TURN}: the turn-4 refund's id and exact amount",
              "RF-9001" in refund.reply and has_amount(refund.reply, "30.00"), refund.reply),
    ]


def s_long_conversation(build: AgentFactory) -> Outcome:
    agent = build()
    records = converse(agent, THREE_ISSUES)
    drift = facts_drift(records)
    issue_1 = bool(refunds_ok(records[3], order_id="ORD-5521"))
    issue_2 = "$7.50" in records[6].reply and "$89.50" in records[6].reply
    issue_3 = bool(escalations(records[11], "policy_gap"))
    refetched = [r.turn for r in records[FACTS_TURN:] if called(r, "lookup_order", order_id="ORD-5521")]
    return [
        Check(f"{len(records)} turns, three unrelated issues each handled (refund, billing answer, account handoff)",
              len(records) >= 20 and issue_1 and issue_2 and issue_3 and agent.memory.compacted_turns >= 15,
              f"issues: refund={issue_1}, billing={issue_2}, account={issue_3}; "
              f"{agent.memory.compacted_turns} turns compacted into the summary"),
        Check(f"No case fact recorded at turn {FACTS_TURN} drifted or disappeared in any later request",
              bool(drift) and all(r["customer_unchanged"] and r["order_unchanged"] for r in drift),
              f"{sum(r['customer_unchanged'] and r['order_unchanged'] for r in drift)}/{len(drift)} later requests "
              "carried the turn-2 facts unchanged"),
        *probe_checks(records, "facts on"),
        Check("The probes were answered from the case facts, not by re-fetching ORD-5521",
              not refetched, f"later lookups of ORD-5521: {refetched or 'none'}"),
    ], [Transcript("22 turns, three issues", records)]


SCENARIOS: list[Scenario] = [
    Scenario("refund_happy_path", "D1/D4", "Verified refund with a validated record", s_refund_happy_path),
    Scenario("refund_before_verification", "D1", "Refund blocked until identity is verified",
             s_refund_before_verification),
    Scenario("explicit_human_first_message", "D5", "Explicit human request honoured immediately",
             s_explicit_human_first),
    Scenario("explicit_human_mid_conversation", "D5", "Explicit human request mid-conversation",
             s_explicit_human_mid),
    Scenario("escalation_backstop", "D1/D5", "The program enforces the explicit-request rule", s_backstop),
    Scenario("frustration_is_not_escalation", "D5", "Counter-cases for escalation", s_frustration),
    Scenario("partial_refund_for_damage", "D4", "Few-shot type 1: defect, amount stated or not",
             s_partial_refund_for_damage),
    Scenario("price_drop_policy_gap", "D5", "Price adjustment is a policy gap", s_price_drop),
    Scenario("duplicate_charge_dispute", "D4", "Few-shot type 2: charge the records don't show",
             s_duplicate_charge),
    Scenario("ambiguous_order_reference", "D4", "Few-shot type 3: two matching orders", s_ambiguous_order),
    Scenario("policy_rejections", "D1/D5", "Policy rejections become policy_limit handoffs", s_policy_rejections),
    Scenario("account_change_to_human", "D2", "Account changes: out of tool scope, handed off", s_account_change),
    Scenario("three_concerns_one_message", "D5", "Multi-concern message decomposed", s_three_concerns),
    Scenario("tool_timeout", "D2/D5", "Structured timeout, retry budget, handoff", s_tool_timeout),
    Scenario("long_conversation_three_issues", "D5", "22 turns, three issues, no drift", s_long_conversation),
]


def run_scenarios(build: AgentFactory, only: list[str] | None = None) -> SuiteResult:
    suite = SuiteResult("scenarios")
    chosen = [s for s in SCENARIOS if not only or s.name in only]
    unknown = sorted(set(only or []) - {s.name for s in SCENARIOS})
    if unknown:
        raise SystemExit(f"unknown scenario(s): {', '.join(unknown)}. Known: {', '.join(s.name for s in SCENARIOS)}")
    rows = []
    for scenario in chosen:
        checks, transcripts = scenario.run(build)
        suite.checks.extend(Check(f"[{scenario.name}] {c.claim}", c.passed, c.detail) for c in checks)
        for t in transcripts:
            suite.transcripts.append(Transcript(f"{scenario.name}: {t.title}", t.records, t.note))
        failing = [c.claim for c in checks if not c.passed]
        rows.append({"scenario": scenario.name, "domain": scenario.domain, "result": "FAIL" if failing else "PASS",
                     "checks": f"{len(checks) - len(failing)}/{len(checks)}", "failing": "; ".join(failing)})
    suite.tables["scenarios"] = rows
    return suite
