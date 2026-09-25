"""D1: the agentic loop's stop_reason handling, the refund prerequisite, the handoff protocol."""

from __future__ import annotations

import json

import pytest

from resolution_agent.backends.adversarial import PromptIgnoringBackend, ScriptedToolBackend, SequenceBackend
from resolution_agent.backends.backend import ModelTurn
from resolution_agent.evaluation.enforcement import fuzz
from resolution_agent.conversation.prompts import TRUNCATION_NOTICE
from resolution_agent.evaluation.scenarios import converse
from resolution_agent.tools.session import ToolSession
from resolution_agent.evaluation.suites import GOOD_REFUND, VERIFY_ARGS


def _text(text: str, stop: str = "end_turn") -> ModelTurn:
    return ModelTurn(stop, [{"type": "text", "text": text}])


def _run(build, turns):
    backend = SequenceBackend(turns)
    agent = build(backend=backend)
    [rec] = converse(agent, ["Where is ORD-5530?"])
    return agent, backend, rec


def test_stop_reason_end_turn_and_stop_sequence_are_the_reply(build):
    for stop in ("end_turn", "stop_sequence"):
        _, _, rec = _run(build, [_text("Here you go.", stop)])
        assert rec.reply == "Here you go." and not rec.system_handoff


def test_stop_reason_pause_turn_replays_the_content_and_continues(build):
    _, backend, rec = _run(build, [ModelTurn("pause_turn", [{"type": "text", "text": "Checking..."}]),
                                   _text("All sorted.")])
    assert rec.reply == "All sorted." and rec.stop_reasons == ["pause_turn", "end_turn"]
    assert backend.requests[1]["messages"][-1] == {"role": "assistant",
                                                   "content": [{"type": "text", "text": "Checking..."}]}


def test_stop_reason_max_tokens_never_runs_a_truncated_tool_call(build):
    truncated = ModelTurn("max_tokens", [{"type": "tool_use", "id": "t1", "name": "process_refund",
                                          "input": {"customer_id": "C-1001"}}])
    agent, backend, rec = _run(build, [truncated, _text("Could you verify your account first?")])
    assert agent.session.state.invocations["process_refund"] == 0 and rec.outcomes == []
    assert TRUNCATION_NOTICE in json.dumps(backend.requests[1]["messages"][-1])
    assert rec.reply == "Could you verify your account first?"


@pytest.mark.parametrize("turns, stop", [
    ([_text("...", "max_tokens"), _text("...", "max_tokens")], "max_tokens"),
    ([_text("partial answer", "refusal")], "refusal"),
    ([_text("?", "model_context_window_exceeded")], "unexpected_stop_reason:model_context_window_exceeded"),
    ([_text("I'll look.", "tool_use")], "empty_tool_use"),
])
def test_stop_reasons_that_cannot_finish_hand_off_with_a_real_ticket(build, turns, stop):
    agent, _, rec = _run(build, turns)
    assert rec.stop == stop and rec.system_handoff
    assert "HT-" in rec.reply and "partial answer" not in rec.reply
    assert agent.session.ledger[-1].authored_by == "system"


def test_tool_round_cap_hands_off(build):
    agent = build(backend=ScriptedToolBackend([[("lookup_order", {"order_id": "ORD-5530"})] * 20]))
    [rec] = converse(agent, ["Where is ORD-5530?"])
    assert rec.stop == "max_tool_rounds" and rec.system_handoff and "HT-" in rec.reply


def test_gate_blocks_an_unverified_refund_before_the_handler_runs():
    session = ToolSession()
    outcome = session.call("process_refund", GOOD_REFUND)
    assert outcome.payload["errorCategory"] == "prerequisite" and outcome.blocked
    assert session.state.invocations["process_refund"] == 0 and session.ledger == []


def test_gate_runs_before_validation_so_a_malformed_unverified_refund_is_still_prerequisite():
    outcome = ToolSession().call("process_refund", {"customer_id": "C-1001", "amount": "lots"})
    assert outcome.payload["failureType"] == "identity_not_verified"


def test_reckless_backend_is_blocked_on_every_unverified_refund(build):
    plan = [
        [("process_refund", GOOD_REFUND)],
        [("get_customer", {**VERIFY_ARGS, "postal_code": "00000"}), ("process_refund", GOOD_REFUND)],
        [("get_customer", {"email": "priya.raman@example.com", "postal_code": "10001"}),
         ("process_refund", GOOD_REFUND)],
        [("get_customer", VERIFY_ARGS), ("lookup_order", {"order_id": "ORD-5521"}), ("process_refund", GOOD_REFUND)],
    ]
    agent = build(backend=ScriptedToolBackend(plan))
    recs = converse(agent, ["refund"] * 4)
    refunds = [o for r in recs for o in r.outcomes if o.tool == "process_refund"]
    assert [o.payload.get("failureType") == "identity_not_verified" for o in refunds] == [True, True, True, False]
    assert agent.session.state.invocations["process_refund"] == 1


def test_fuzz_no_refund_ever_reaches_the_handler_unverified():
    report = fuzz(sequences=1000)
    assert report.bypasses == []
    assert report.reached_handler == report.reached_handler_verified > 20


def test_handoff_protocol_ticket_carries_customer_root_cause_and_recommended_action(build):
    agent = build()
    converse(agent, ["My email is dana.whitfield@example.com and my postal code is 94107.",
                     "My bank shows two charges for ORD-5530, I was charged twice!"])
    ticket = agent.session.state.tickets[-1]
    record = ticket["handoff_record"]
    assert record["customer_id"] == "C-1001" and record["identity_verified"] is True
    assert "ORD-5530" in record["root_cause"] and "processor" in record["recommended_action"]
    assert ticket["handoff"]["case_facts"]["customer"]["customer_id"] == "C-1001"


def test_explicit_request_is_honoured_even_if_the_model_ignores_the_nudge(build):
    agent = build(backend=PromptIgnoringBackend(stubborn=True))
    [rec] = converse(agent, ["I want to talk to a human about ORD-5530."])
    assert rec.nudged and rec.system_handoff and rec.stop == "explicit_request_not_escalated"
    assert agent.session.state.tickets[-1]["reason_category"] == "customer_request"
    assert agent.session.state.invocations["lookup_order"] == 0
