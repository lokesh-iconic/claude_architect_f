"""D5: case facts across a 22-turn, three-issue conversation; escalation honoured immediately."""

from __future__ import annotations

import json

import pytest

from resolution_agent.backends.backend import MockBackend
from resolution_agent.conversation.facts import OPEN_TAG
from resolution_agent.conversation.memory import SUMMARY_OPEN
from resolution_agent.evaluation.scenarios import (
    DANA_VERIFY, FACTS_TURN, PROBE_AMOUNT_TURN, PROBE_EMAIL_TURN, PROBE_REFUND_TURN, THREE_ISSUES, converse,
    escalations, facts_drift, refunds_ok,
)


@pytest.fixture
def long_run(build):
    agent = build()
    return agent, converse(agent, THREE_ISSUES)


def test_conversation_is_20_plus_turns_three_issues_and_compacted(long_run):
    agent, records = long_run
    assert len(records) >= 20 and agent.memory.compacted_turns == len(records) - agent.memory.keep_recent
    assert refunds_ok(records[3], order_id="ORD-5521")[0].input["amount"] == 30.0
    assert "$7.50" in records[6].reply
    assert escalations(records[11], "policy_gap")


def test_no_turn_2_fact_drifted_or_was_lost_by_turn_22(long_run):
    _, records = long_run
    rows = facts_drift(records)
    assert len(rows) == len(records) - FACTS_TURN
    assert all(r["customer_unchanged"] and r["order_unchanged"] for r in rows)
    assert {(r["total"], r["placed_on"], r["email"]) for r in rows} == {
        (249.99, "2026-09-02", "dana.whitfield@example.com")}


def test_probes_at_the_end_quote_turn_2_facts_exactly(long_run):
    _, records = long_run
    amount = records[PROBE_AMOUNT_TURN - 1].reply
    assert "$249.99" in amount and "2026-09-02" in amount
    assert "dana.whitfield@example.com" in records[PROBE_EMAIL_TURN - 1].reply
    assert "RF-9001" in records[PROBE_REFUND_TURN - 1].reply and "$30.00" in records[PROBE_REFUND_TURN - 1].reply


def test_probes_are_answered_from_case_facts_not_by_refetching(long_run):
    _, records = long_run
    lookups = [r.turn for r in records if any(o.tool == "lookup_order" and o.input.get("order_id") == "ORD-5521"
                                              for o in r.outcomes)]
    assert lookups == [FACTS_TURN]


def test_the_refund_is_the_only_change_to_the_order_facts(long_run):
    _, records = long_run
    assert (records[-1].facts_seen["orders"]["ORD-5521"]["refunded_amount"]) == 30.0


def test_without_case_facts_the_same_run_loses_facts(build):
    records = converse(build(case_facts=False), THREE_ISSUES)
    assert "dana.whitfield@example.com" not in records[PROBE_EMAIL_TURN - 1].reply
    assert "RF-9001" not in records[PROBE_REFUND_TURN - 1].reply


def test_case_facts_are_a_separate_system_block_in_every_request(build):
    agent = build()
    backend: MockBackend = agent.backend
    converse(agent, THREE_ISSUES)
    for params in backend.calls:
        assert len(params["system"]) == 2 and params["system"][1]["text"].startswith(OPEN_TAG)
        assert OPEN_TAG not in json.dumps(params["messages"])
    assert any(SUMMARY_OPEN in json.dumps(p["messages"]) for p in backend.calls)


def test_explicit_human_request_is_honoured_immediately_without_solving_first(build):
    agent = build()
    [rec] = converse(agent, ["I've had enough, let me speak to a manager about my refund for ORD-5521."])
    assert rec.tools == ["escalate_to_human"] and escalations(rec, "customer_request")
    assert agent.session.state.invocations["lookup_order"] == 0
    assert agent.session.state.invocations["process_refund"] == 0


def test_explicit_human_request_mid_conversation_after_identity(build):
    agent = build()
    recs = converse(agent, [DANA_VERIFY, "Actually, just get me a human please."])
    assert recs[1].tools == ["escalate_to_human"]
    assert agent.session.state.tickets[-1]["handoff_record"]["customer_id"] == "C-1001"


def test_frustration_alone_does_not_escalate(build):
    [rec] = converse(build(), ["This is the third time I'm asking, where is ORD-5530?!"])
    assert not escalations(rec) and "lookup_order" in rec.tools
