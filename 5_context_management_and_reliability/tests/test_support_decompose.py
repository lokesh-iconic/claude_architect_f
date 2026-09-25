"""Build step: a message with several concerns gets each one handled, not just the first."""

from __future__ import annotations

from support_agent.backends.mock_agent import split_concerns
from support_agent.evaluation.scenarios import DANA_VERIFY, IDENTITY_AND_TWO_CONCERNS, THREE_CONCERNS, converse


def test_three_concerns_each_get_an_action_and_an_answer(build):
    agent = build()
    rec = converse(agent, [DANA_VERIFY, THREE_CONCERNS])[1]
    calls = [(o.tool, o.input.get("order_id")) for o in rec.outcomes]
    assert ("process_refund", "ORD-5521") in calls
    assert ("lookup_order", "ORD-5530") in calls
    assert ("lookup_order", "ORD-4410") in calls
    sections = [s for s in rec.reply.split("\n\n") if s.strip()]
    assert len(sections) == 3
    assert "ORD-5521" in sections[0] and "RF-" in sections[0]
    assert "ORD-5530" in sections[1]
    assert "ORD-4410" in sections[2] and "2026-07-04" in sections[2]


def test_identity_then_refund_in_one_message_verifies_before_refunding(build):
    agent = build()
    [rec] = converse(agent, [IDENTITY_AND_TWO_CONCERNS])
    tools = rec.tools
    assert tools.index("get_customer") < tools.index("process_refund")
    assert not any(o.is_error for o in rec.outcomes)
    assert "ORD-5530" in rec.reply


def test_splitter_keeps_a_problem_description_with_its_request():
    concerns = split_concerns(THREE_CONCERNS)
    assert [c.intent for c in concerns] == ["refund", "status", "policy_question"]
    assert concerns[0].order_ids == ["ORD-5521"] and "cracked" in concerns[0].text
