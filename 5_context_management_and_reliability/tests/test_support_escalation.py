"""Self-check 2: does it escalate immediately on an explicit request, without trying to solve it first?

Plus the other escalation criteria and the ambiguity pattern.
"""

from __future__ import annotations

import pytest

from support_agent.adversarial import PromptIgnoringBackend
from support_agent.escalation import explicit_human_request
from support_agent.scenarios import DANA_VERIFY, MARCUS_VERIFY, PRIYA_VERIFY, converse


def _escalated(record, reason):
    return [o for o in record.outcomes if o.tool == "escalate_to_human" and not o.is_error
            and o.input.get("reason_category") == reason]


def test_explicit_human_request_escalates_first_with_no_other_tool(build):
    agent = build()
    [rec] = converse(agent, ["This is ridiculous. I want to speak to a real person right now about ORD-5530."])
    assert rec.tools == ["escalate_to_human"]
    assert _escalated(rec, "customer_request")
    assert agent.session.state.invocations["lookup_order"] == 0
    assert "HT-" in rec.reply


def test_explicit_human_request_mid_conversation_escalates_on_that_turn(build):
    agent = build()
    recs = converse(agent, [DANA_VERIFY, "Can you look up ORD-5521?",
                            "Actually, forget that. Just get me a human please."])
    assert recs[2].tools == ["escalate_to_human"]


def test_escalation_backstop_blocks_other_tools_and_nudges(build):
    agent = build(backend=PromptIgnoringBackend())
    [rec] = converse(agent, ["I want to talk to a human about ORD-5530."])
    blocked = [o for o in rec.outcomes if o.blocked]
    assert [o.tool for o in blocked] == ["lookup_order"]
    assert blocked[0].payload["errorCategory"] == "escalation"
    assert agent.session.state.invocations["lookup_order"] == 0
    assert rec.nudged and _escalated(rec, "customer_request")


def test_frustration_alone_does_not_escalate(build):
    agent = build()
    [rec] = converse(agent, ["This is the third time I'm asking, where is ORD-5530?!"])
    assert "escalate_to_human" not in rec.tools and "lookup_order" in rec.tools


def test_mentioning_a_person_is_not_a_request_for_one(build):
    agent = build()
    [rec] = converse(agent, ["The person at your store said I could get a refund for ORD-5521."])
    assert not rec.explicit_human_request and "escalate_to_human" not in rec.tools


def test_policy_gap_escalates_and_never_refunds(build):
    agent = build()
    recs = converse(agent, [DANA_VERIFY, "ORD-5530 went on sale for $20 less two days after I bought it. "
                                         "Can I get the difference back?"])
    assert _escalated(recs[1], "policy_gap")
    assert "process_refund" not in recs[1].tools


def test_policy_rejection_escalates_as_policy_limit_with_the_error_in_the_handoff(build):
    agent = build()
    recs = converse(agent, [MARCUS_VERIFY, "Please refund ORD-7100, it's too big for my counter."])
    refund = next(o for o in recs[1].outcomes if o.tool == "process_refund")
    assert refund.payload["failureType"] == "above_auto_refund_limit"
    assert _escalated(recs[1], "policy_limit")
    handoff = agent.session.state.tickets[-1]["handoff"]
    assert any(e.get("failureType") == "above_auto_refund_limit" for e in handoff["recent_errors"])


def test_ambiguous_order_gets_a_clarifying_question_not_a_guess(build):
    agent = build()
    recs = converse(agent, [PRIYA_VERIFY, "Please refund the kettle, it leaks."])
    assert "process_refund" not in recs[1].tools
    assert "ORD-6002" in recs[1].reply and "ORD-6003" in recs[1].reply


@pytest.mark.parametrize("text", [
    "I want to speak to a real person.",
    "Can I talk to a human?",
    "Get me a manager.",
    "Transfer me to a representative please",
    "I'd like a human, please.",
    "just get me a human please",
])
def test_detector_fires_on_explicit_requests(text):
    assert explicit_human_request(text)


@pytest.mark.parametrize("text", [
    "The person at your store said I could get a refund.",
    "This is the third time I'm asking!",
    "I don't want to talk to a human, just fix it.",
    "My manager bought this for the office.",
    "Are you a real person?",
])
def test_detector_ignores_non_requests(text):
    assert not explicit_human_request(text)
