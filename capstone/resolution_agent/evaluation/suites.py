"""Per-domain suites: the checks each build step implies, beyond the conversation set.

* `loop`    (D1) -- every stop_reason branch, the refund prerequisite (reckless
                    backend + fuzz), the handoff protocol end to end.
* `tools`   (D2) -- tool scope, description boundaries, selection, structured
                    errors in-process and over MCP stdio.
* `actions` (D4) -- the strict/API schema split, few-shot examples against the
                    validator, validation-retry, and the ledger "no exceptions"
                    invariant across the scenario set and a fuzz.
* `context` (D5) -- the 22-turn conversation's drift table, the ablation,
                    trimming, and where the facts block sits in every request.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from ..tools.actions import SEMANTIC_CHECKS, generic_text
from ..backends.adversarial import ScriptedToolBackend, SequenceBackend
from ..conversation.agent import SupportAgent
from ..backends.backend import MockBackend, ModelTurn
from ..tools.data import CUSTOMERS, ORDERS
from .enforcement import fuzz
from ..tools.errors import SupportToolError
from ..conversation.facts import OPEN_TAG
from ..conversation.few_shot import EXAMPLES
from ..tools.handlers import Faults
from ..conversation.memory import SUMMARY_OPEN
from .probe_client import FOUR_TOOLS, mcp_checks, probe
from ..conversation.prompts import SYSTEM_PROMPT, TRUNCATION_NOTICE
from .results import Check, SuiteResult, Transcript
from .scenarios import (
    DANA_VERIFY, FACTS_TURN, SCENARIOS, THREE_ISSUES, AgentFactory, called, converse, escalations, facts_drift,
    probe_checks,
)
from ..tools.schema import CLIENT_SIDE_KEYWORDS, DESCRIPTIONS, api_tool_specs, tool_specs, validate_input
from ..tools.session import ToolSession
from ..config.settings import MODULE_DIR

VERIFY_ARGS = {"email": "dana.whitfield@example.com", "postal_code": "94107"}
GOOD_REFUND = {"customer_id": "C-1001", "order_id": "ORD-5521", "amount": 249.99, "currency": "USD",
               "refund_type": "full", "reason_code": "damaged_item", "customer_statement": "It arrived cracked.",
               "justification": "ORD-5521 total 249.99, nothing refunded, window open until 2026-10-06."}
BAD_REFUND = {k: v for k, v in GOOD_REFUND.items() if k != "reason_code"} | {"amount": 300.0}
GOOD_HANDOFF = {
    "reason_category": "customer_request", "customer_id": "C-1001", "identity_verified": True,
    "order_ids": ["ORD-5521"], "customer_request": "Customer wants a person to handle ORD-5521.",
    "root_cause": "The customer explicitly asked for a human before any troubleshooting.",
    "recommended_action": "Call the customer about ORD-5521; identity is already verified (C-1001).",
    "actions_taken": ["get_customer: verified"],
}
BAD_HANDOFF = {**GOOD_HANDOFF, "customer_id": None, "identity_verified": False,
               "root_cause": "Please review this case.", "recommended_action": "Please look into this ticket."}


def _text(text: str, stop: str = "end_turn") -> ModelTurn:
    return ModelTurn(stop, [{"type": "text", "text": text}])


def _tool(name: str, args: dict[str, Any], n: int = 1) -> ModelTurn:
    return ModelTurn("tool_use", [{"type": "tool_use", "id": f"toolu_sr_{n}", "name": name, "input": args}])


def capture_scenarios(build: AgentFactory) -> list[SupportAgent]:
    agents: list[SupportAgent] = []

    def capturing(**kw: Any) -> SupportAgent:
        agent = build(**kw)
        agents.append(agent)
        return agent

    for scenario in SCENARIOS:
        scenario.run(capturing)
    return agents


# --------------------------------------------------------------------------
# D1: the loop, the prerequisite, the handoff protocol
# --------------------------------------------------------------------------


def run_loop(build: AgentFactory, fuzz_sequences: int = 2000) -> SuiteResult:
    suite = SuiteResult("loop")
    rows: list[dict[str, Any]] = []

    def stop_case(label: str, turns: list[ModelTurn], expect_handoff: bool, extra=None) -> None:
        backend = SequenceBackend(turns)
        agent = build(backend=backend)
        [rec] = converse(agent, ["Where is ORD-5530?"])
        ok = rec.system_handoff == expect_handoff and ("HT-" in rec.reply) == expect_handoff
        if extra:
            ok = ok and extra(rec, agent, backend)
        rows.append({"case": label, "stop_reasons": rec.stop_reasons, "turn_stop": rec.stop,
                     "handed_off": rec.system_handoff, "ok": ok})
        suite.transcripts.append(Transcript(f"stop_reason: {label}", [rec]))

    truncated_refund = ModelTurn("max_tokens", [{"type": "tool_use", "id": "toolu_trunc", "name": "process_refund",
                                                 "input": {"customer_id": "C-1001"}}])
    stop_case("pause_turn, then end_turn", [ModelTurn("pause_turn", [{"type": "text", "text": "Checking..."}]),
                                            _text("All sorted.")], False,
              lambda r, a, b: r.reply == "All sorted." and b.requests[1]["messages"][-1]["role"] == "assistant")
    stop_case("max_tokens with a truncated tool call, then end_turn", [truncated_refund, _text("Could you verify?")],
              False, lambda r, a, b: a.session.state.invocations["process_refund"] == 0 and not r.outcomes
              and TRUNCATION_NOTICE in json.dumps(b.requests[1]["messages"][-1]))
    stop_case("max_tokens twice", [_text("...", "max_tokens"), _text("...", "max_tokens")], True)
    stop_case("refusal", [_text("partial answer", "refusal")], True, lambda r, a, b: "partial answer" not in r.reply)
    stop_case("stop_sequence", [_text("Here you go.", "stop_sequence")], False, lambda r, a, b: r.reply == "Here you go.")
    stop_case("unrecognised stop_reason", [_text("?", "model_context_window_exceeded")], True)
    stop_case("tool_use with no tool block", [_text("I'll look.", "tool_use")], True)
    stop_case("tool rounds exhausted", [_tool("lookup_order", {"order_id": "ORD-5530"}, n) for n in range(20)], True,
              lambda r, a, b: r.stop == "max_tool_rounds")
    suite.tables["stop_reasons"] = rows
    suite.checks.append(Check(
        "Every stop_reason has its own branch: continue, reply, retry once, or hand off -- never a silent end",
        all(r["ok"] for r in rows), ", ".join(f"{r['case']}: {'ok' if r['ok'] else 'WRONG'}" for r in rows)))

    plan = [
        [("process_refund", GOOD_REFUND)],
        [("get_customer", {**VERIFY_ARGS, "postal_code": "00000"}), ("process_refund", GOOD_REFUND)],
        [("get_customer", {"email": "priya.raman@example.com", "postal_code": "10001"}),
         ("process_refund", GOOD_REFUND)],
        [("get_customer", VERIFY_ARGS), ("lookup_order", {"order_id": "ORD-5521"}), ("process_refund", GOOD_REFUND)],
    ]
    agent = build(backend=ScriptedToolBackend(plan))
    recs = converse(agent, ["refund please"] * 4)
    refunds = [o for r in recs for o in r.outcomes if o.tool == "process_refund"]
    pattern = [o.payload.get("failureType") == "identity_not_verified" for o in refunds]
    suite.transcripts.append(Transcript("Reckless backend: refunds before, during and after verification", recs))
    suite.checks.append(Check(
        "A backend that refunds regardless is blocked unverified, with a wrong postal code, and with another "
        "customer verified; allowed only once the right customer is verified",
        pattern == [True, True, True, False] and not refunds[-1].is_error, f"blocked pattern: {pattern}"))

    report = fuzz(sequences=fuzz_sequences)
    suite.tables["refund_gate_fuzz"] = [{
        "sequences": report.sequences, "tool_calls": report.calls, "refund_attempts": report.refund_attempts,
        "blocked_by_prerequisite": report.blocked_by_prerequisite,
        "rejected_by_validation": report.rejected_by_validation, "reached_handler": report.reached_handler,
        "reached_handler_verified": report.reached_handler_verified, "bypasses": len(report.bypasses)}]
    suite.checks.append(Check(
        f"Every time: {report.sequences:,} random call sequences, no refund reached the handler unverified",
        not report.bypasses and report.reached_handler == report.reached_handler_verified > 0,
        f"{report.refund_attempts:,} refund attempts, {report.blocked_by_prerequisite:,} blocked by the "
        f"prerequisite, {report.reached_handler:,} reached the handler (all verified)"))

    agents = capture_scenarios(build)
    tickets = [t for a in agents for t in a.session.state.tickets]
    bad = [t["id"] for t in tickets if not t["handoff_record"].get("root_cause")
           or generic_text(t["handoff_record"]["root_cause"]) or generic_text(t["handoff_record"]["recommended_action"])
           or "case_facts" not in t["handoff"]]
    wrong_id = [t["id"] for a in agents for t in a.session.state.tickets
                if t["handoff_record"]["customer_id"] != (a.session.facts.customer.customer_id
                                                          if a.session.facts.customer else None)]
    suite.tables["handoffs"] = [
        {"ticket": t["id"], "reason": t["reason_category"], "customer_id": t["handoff_record"]["customer_id"],
         "root_cause": t["handoff_record"]["root_cause"], "recommended_action": t["handoff_record"]["recommended_action"]}
        for t in tickets]
    suite.checks.append(Check(
        "Every handoff in the scenario set names the verified customer (or null), a root cause, a recommended "
        "action, and carries the case facts",
        bool(tickets) and not bad and not wrong_id,
        f"{len(tickets)} tickets; generic or incomplete: {bad or 'none'}; wrong customer_id: {wrong_id or 'none'}"))
    return suite


# --------------------------------------------------------------------------
# D2: tools and MCP
# --------------------------------------------------------------------------

SELECTION = [
    ("verify identity", [], DANA_VERIFY, "get_customer", "process_refund"),
    ("order status", [], "Where is ORD-5530?", "lookup_order", "escalate_to_human"),
    ("refund a defect", [DANA_VERIFY], "ORD-5521 stopped working, please refund it.", "process_refund",
     "escalate_to_human"),
    ("billing question", [DANA_VERIFY], "Why was I charged $89.50 for ORD-5530?", "lookup_order", "process_refund"),
    ("price drop", [DANA_VERIFY], "ORD-5530 went on sale, can I get the difference back?", "escalate_to_human",
     "process_refund"),
    ("duplicate charge", [DANA_VERIFY], "I was charged twice for ORD-5530.", "escalate_to_human", "process_refund"),
    ("account change", [DANA_VERIFY], "Please update the phone number on my account.", "escalate_to_human",
     "process_refund"),
]


def run_tools(build: AgentFactory) -> SuiteResult:
    suite = SuiteResult("tools")

    agent = build()
    offered = sorted(t["name"] for t in agent.build_request([])["tools"])
    out_of_scope = ToolSession().call("update_account_email", {"customer_id": "C-1001", "email": "x@example.com"})
    mcp_config = json.loads((MODULE_DIR / ".mcp.json").read_text(encoding="utf-8"))
    servers = sorted(mcp_config.get("mcpServers", {}))
    suite.checks.append(Check(
        "Scope: the agent is offered exactly the four workflow tools, the project registers one server, and a "
        "call to any other tool is refused with a structured error",
        offered == FOUR_TOOLS and servers == ["support-resolution"]
        and out_of_scope.payload.get("failureType") == "tool_out_of_scope",
        f"offered: {offered}; servers: {servers}; other tool -> {out_of_scope.payload.get('failureType')}"))

    boundaries = {"get_customer": "changes nothing", "lookup_order": "never moves money",
                  "process_refund": "Not for", "escalate_to_human": "not a way to finish"}
    missing = [t for t, phrase in boundaries.items() if phrase not in DESCRIPTIONS[t]]
    suite.checks.append(Check(
        "Each description states what the tool is not for, so neighbouring tools don't overlap",
        not missing, f"missing a boundary: {missing or 'none'}"))

    rows = []
    for label, setup, request, must, must_not in SELECTION:
        a = build()
        recs = converse(a, [*setup, request])
        tools = recs[-1].tools
        rows.append({"request": label, "called": tools, "expected": must, "must_not": must_not,
                     "ok": must in tools and must_not not in tools})
    suite.tables["selection"] = rows
    suite.checks.append(Check(
        "Tool selection across near-miss requests: the right tool, never the tempting wrong one",
        all(r["ok"] for r in rows), ", ".join(f"{r['request']}: {'ok' if r['ok'] else 'WRONG'}" for r in rows)))

    session = ToolSession(tool_timeout_s=0.3, faults=Faults(timeouts={"lookup_order": 1}))
    session.begin_turn()
    samples = {"prerequisite": session.call("process_refund", GOOD_REFUND)}
    session.call("get_customer", VERIFY_ARGS)
    samples["transient"] = session.call("lookup_order", {"order_id": "ORD-5530"})
    session.call("lookup_order", {"order_id": "ORD-6002"})
    session.call("lookup_order", {"order_id": "ORD-4410"})
    samples["validation"] = session.call("process_refund", {**GOOD_REFUND, "amount": 999.0})
    samples["permission"] = session.call("process_refund", {**GOOD_REFUND, "order_id": "ORD-6002", "amount": 64.0})
    samples["policy"] = session.call("process_refund", {**GOOD_REFUND, "order_id": "ORD-4410", "amount": 39.0})
    samples["escalation"] = session.blocked("lookup_order", {"order_id": "ORD-5530"}, SupportToolError(
        "the customer explicitly asked for a human", category="escalation", failure_type="escalation_required",
        attempted="lookup_order(order_id='ORD-5530')"))
    rows = [{"category": k, "errorCategory": o.payload.get("errorCategory"), "isRetryable": o.payload.get("isRetryable"),
             "failureType": o.payload.get("failureType"), "description": o.payload.get("description")}
            for k, o in samples.items()]
    suite.tables["error_payloads"] = rows
    suite.checks.append(Check(
        "Every failure category comes back with errorCategory, a boolean isRetryable and a human-readable description",
        all(r["errorCategory"] == r["category"] and isinstance(r["isRetryable"], bool) and r["description"]
            for r in rows),
        ", ".join(f"{r['category']}: retryable={r['isRetryable']}" for r in rows)))

    checks, tables = mcp_checks(asyncio.run(probe()))
    suite.checks.extend(checks)
    suite.tables.update(tables)
    return suite


# --------------------------------------------------------------------------
# D4: structured actions
# --------------------------------------------------------------------------


def _keys(node: Any) -> set[str]:
    if isinstance(node, dict):
        return set(node) | {k for v in node.values() for k in _keys(v)}
    if isinstance(node, list):
        return {k for v in node for k in _keys(v)}
    return set()


def _objects(node: Any) -> list[dict[str, Any]]:
    if isinstance(node, dict):
        own = [node] if node.get("type") == "object" else []
        return own + [o for v in node.values() for o in _objects(v)]
    if isinstance(node, list):
        return [o for v in node for o in _objects(v)]
    return []


def run_actions(build: AgentFactory, fuzz_sequences: int = 2000) -> SuiteResult:
    suite = SuiteResult("actions")

    api = api_tool_specs()
    leaked = sorted(_keys([s["input_schema"] for s in api]) & CLIENT_SIDE_KEYWORDS)
    kept = sorted(_keys([s["input_schema"] for s in tool_specs()]) & CLIENT_SIDE_KEYWORDS)
    suite.checks.append(Check(
        "Every tool is sent strict with additionalProperties false; constraints strict mode can't carry are "
        "enforced client-side instead",
        all(s["strict"] for s in api) and all(o.get("additionalProperties") is False
                                              for s in api for o in _objects(s["input_schema"]))
        and not leaked and bool(kept),
        f"sent to the API: {leaked or 'no'} client-side keywords; enforced locally: {kept}"))

    rows = []
    for ex in EXAMPLES:
        issues = [] if ex.tool is None else validate_input(ex.tool, ex.action) + SEMANTIC_CHECKS[ex.tool](ex.action, ex.facts)
        fictional = not (set(ex.facts.orders) & set(ORDERS)) and ex.facts.customer.customer_id not in CUSTOMERS
        rows.append({"example": ex.name, "action": ex.tool or "clarify", "issues": [i.code for i in issues],
                     "fictional_ids": fictional, "in_prompt": ex.customer in SYSTEM_PROMPT})
    suite.tables["few_shot"] = rows
    suite.checks.append(Check(
        "The three few-shot examples (defect refund, uncorroborated duplicate charge, two matching orders) pass "
        "the agent's own validator, use fictional ids, and are in the system prompt",
        len(rows) == 3 and all(not r["issues"] and r["fictional_ids"] and r["in_prompt"] for r in rows),
        "; ".join(f"{r['example']}: {r['action']}, issues={r['issues']}" for r in rows)))

    agent = build(backend=ScriptedToolBackend([[("get_customer", VERIFY_ARGS), ("lookup_order", {"order_id": "ORD-5521"}),
                                                ("process_refund", BAD_REFUND), ("process_refund", GOOD_REFUND)]]))
    [rec] = converse(agent, ["It arrived cracked, refund ORD-5521."])
    attempts = [o for o in rec.outcomes if o.tool == "process_refund"]
    first = attempts[0].payload if attempts else {}
    codes = sorted(i["code"] for i in first.get("issues", []))
    entry = agent.session.ledger[-1] if agent.session.ledger else None
    suite.transcripts.append(Transcript("Validation-retry: malformed refund, then corrected", [rec]))
    suite.checks.append(Check(
        "Validation-retry: a malformed refund is rejected before the handler with each issue listed, and the "
        "corrected record then executes",
        first.get("failureType") == "invalid_action_record" and first.get("isRetryable") is True
        and codes == ["exceeds_refundable", "missing"] and not attempts[1].is_error
        and agent.session.state.invocations["process_refund"] == 1,
        f"first attempt issues: {codes}; handler runs: {agent.session.state.invocations['process_refund']}"))
    suite.checks.append(Check(
        "The ledger record keeps the rejected attempt alongside the record that ran",
        entry is not None and entry.validation_attempts == 2 and entry.status == "executed"
        and len(entry.rejected_attempts) == 1,
        f"{entry.action_id}: attempts={entry.validation_attempts}, status={entry.status}" if entry else "no record"))

    session = ToolSession()
    session.begin_turn()
    session.call("get_customer", VERIFY_ARGS)
    session.call("lookup_order", {"order_id": "ORD-5521"})
    same = [session.call("process_refund", BAD_REFUND).payload for _ in range(2)]
    session.begin_turn()
    variants = [{**GOOD_REFUND, "amount": 300.0}, {**GOOD_REFUND, "currency": "EUR"},
                {**GOOD_REFUND, "refund_type": "partial"}]
    budget = [session.call("process_refund", v).payload for v in variants]
    suite.checks.append(Check(
        "The retry budget ends the loop: resubmitting the same issues stops at once; three different invalid "
        "attempts stop at the third",
        same[0]["isRetryable"] and not same[1]["isRetryable"] and same[1].get("sameIssuesAsLastAttempt")
        and [b["isRetryable"] for b in budget] == [True, True, False]
        and session.state.invocations["process_refund"] == 0,
        f"same issues: {[p['isRetryable'] for p in same]}; distinct: {[b['isRetryable'] for b in budget]}"))

    agent = build(backend=ScriptedToolBackend([[("get_customer", VERIFY_ARGS), ("escalate_to_human", BAD_HANDOFF),
                                                ("escalate_to_human", GOOD_HANDOFF)]]))
    [rec] = converse(agent, ["My ORD-5521 problem needs your team to sort it out."])
    tries = [o for o in rec.outcomes if o.tool == "escalate_to_human"]
    codes = sorted({i["code"] for i in tries[0].payload.get("issues", [])}) if tries else []
    suite.checks.append(Check(
        "A handoff with the wrong customer_id and 'please review' is rejected and corrected before a ticket exists",
        codes == ["contradicts_case_facts", "generic_text", "not_the_verified_customer"]
        and not tries[1].is_error and len(agent.session.state.tickets) == 1,
        f"rejection codes: {codes}; tickets: {len(agent.session.state.tickets)}"))

    agent = build(backend=ScriptedToolBackend([[("escalate_to_human", BAD_HANDOFF)] * 3]))
    [rec] = converse(agent, ["Please just get me a human."])
    ledger = agent.session.ledger
    suite.checks.append(Check(
        "If the model can't produce a valid handoff within the budget, the program files one through the same "
        "validator",
        rec.system_handoff and len(ledger) == 1 and ledger[0].authored_by == "system"
        and not validate_input("escalate_to_human", ledger[0].record) and "HT-" in rec.reply,
        f"stop: {rec.stop}; ledger: {[(r.action_id, r.authored_by, r.status) for r in ledger]}"))

    agents = capture_scenarios(build)
    runs = sum(a.session.state.invocations[t] for a in agents for t in ("process_refund", "escalate_to_human"))
    records = [r for a in agents for r in a.session.ledger]
    # Case-fact checks ran at admission; facts move on afterwards (a refund lowers what is refundable).
    invalid = [r.action_id for r in records if validate_input(r.tool, r.record)]
    suite.tables["ledger_by_tool"] = [
        {"tool": tool, "authored_by": who, "status": status,
         "records": sum(1 for r in records if (r.tool, r.authored_by, r.status.split(":")[0]) == (tool, who, status))}
        for tool, who, status in sorted({(r.tool, r.authored_by, r.status.split(":")[0]) for r in records})]
    suite.checks.append(Check(
        f"No exceptions across the {len(SCENARIOS)} scenarios: every refund and handoff that reached a handler has "
        "a validated ledger record",
        runs == len(records) > 0 and not invalid,
        f"{runs} action handler runs, {len(records)} ledger records, {len(invalid)} invalid"))

    report = fuzz(sequences=fuzz_sequences)
    suite.tables["ledger_fuzz"] = [{
        "sequences": report.sequences, "action_handler_runs": report.action_handler_runs,
        "ledger_records": report.ledger_records, "invalid_ledger_records": report.invalid_ledger_records,
        "rejected_by_validation": report.rejected_by_validation, "mismatches": len(report.bypasses)}]
    suite.checks.append(Check(
        f"No exceptions under fuzz: {report.sequences:,} random sequences with malformed records mixed in",
        not report.bypasses and report.action_handler_runs == report.ledger_records > 0
        and report.invalid_ledger_records == 0 and report.rejected_by_validation > 0,
        f"{report.action_handler_runs:,} handler runs = {report.ledger_records:,} ledger records; "
        f"{report.rejected_by_validation:,} malformed refunds rejected before running"))
    return suite


# --------------------------------------------------------------------------
# D5: context across a long, multi-issue conversation
# --------------------------------------------------------------------------


def run_context(build: AgentFactory) -> SuiteResult:
    suite = SuiteResult("context")

    agent = build()
    records = converse(agent, THREE_ISSUES)
    suite.transcripts.append(Transcript("22 turns, three issues, case facts ON", records))
    drift = facts_drift(records)
    suite.tables["drift"] = drift
    suite.checks.append(Check(
        f"{len(records)} turns with {agent.memory.compacted_turns} compacted into the summary",
        len(records) >= 20 and agent.memory.compacted_turns == len(records) - agent.memory.keep_recent,
        f"{len(agent.memory.exchanges)} turns kept verbatim"))
    suite.checks.append(Check(
        f"Every request from turn {FACTS_TURN + 1} to {len(records)} carried the turn-{FACTS_TURN} customer and "
        "order facts unchanged",
        all(r["customer_unchanged"] and r["order_unchanged"] for r in drift), f"{len(drift)} requests compared"))
    refund_facts = (records[-1].facts_seen or {}).get("orders", {}).get("ORD-5521", {})
    suite.checks.append(Check(
        "The one legitimate change -- the turn-4 refund -- is reflected exactly (refunded_amount 30.0)",
        refund_facts.get("refunded_amount") == 30.0, f"refunded_amount={refund_facts.get('refunded_amount')!r}"))
    on = probe_checks(records, "facts on")
    suite.checks.extend(on)

    ablation = build(case_facts=False)
    ab_records = converse(ablation, THREE_ISSUES)
    suite.transcripts.append(Transcript(
        "Same conversation, case facts OFF (ablation)", ab_records,
        "Same summarizer, no case-facts block. This is the control; it is expected to fail the probes."))
    off = probe_checks(ab_records, "facts off")
    suite.tables["ablation"] = [{"probe": c.claim.split("] ", 1)[1], "facts_on": a.passed, "facts_off": c.passed,
                                 "facts_off_reply": c.detail} for c, a in zip(off, on)]
    suite.checks.append(Check(
        "Ablation: without the case-facts block, the same run loses or distorts at least one fact",
        not all(c.passed for c in off), f"{sum(not c.passed for c in off)}/{len(off)} probes failed with facts off"))

    backend: MockBackend = agent.backend  # type: ignore[assignment]
    calls = getattr(backend, "calls", [])
    placed = all(len(p["system"]) == 2 and p["system"][1]["text"].startswith(OPEN_TAG)
                 and OPEN_TAG not in json.dumps(p["messages"]) for p in calls)
    suite.checks.append(Check(
        "The facts block is its own system block after the cached prompt, never inside the summarized history",
        (placed and any(SUMMARY_OPEN in json.dumps(p["messages"]) for p in calls)) if calls else True,
        f"{len(calls)} requests inspected" if calls else "live mode: request log not kept"))

    ok = [o for o in agent.session.log if not o.is_error]
    raw, kept = sum(o.raw_chars for o in ok), sum(o.trimmed_chars for o in ok)
    suite.checks.append(Check(
        "Tool outputs are trimmed before they enter context", raw > 0 and kept < raw * 0.25,
        f"{raw:,} raw chars -> {kept:,} in context ({100 * kept / max(raw, 1):.1f}% kept)"))
    suite.tables["request_size"] = [{"turn": r.turn, "request_chars": r.request_chars,
                                     "summary": r.summary_in_request} for r in records]
    escalated = [r.turn for r in records if escalations(r)]
    suite.notes.append(f"Handoffs during the conversation: turn(s) {escalated}; refunds: "
                       f"{[r.turn for r in records if called(r, 'process_refund')]}.")
    return suite
