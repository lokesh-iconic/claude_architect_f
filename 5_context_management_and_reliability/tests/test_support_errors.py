"""Build step: a tool timeout mid-conversation returns structured, recoverable context."""

from __future__ import annotations

import asyncio

from mcp import Client

from support_agent.tools import handlers
from support_agent.tools.handlers import Faults
from support_agent.evaluation.probe_client import _call, server_params
from support_agent.evaluation.scenarios import DANA_VERIFY, converse
from support_agent.tools.session import ToolSession


def _timeouts(record):
    return [o for o in record.outcomes if o.payload.get("failureType") == "timeout"]


def test_timeout_payload_carries_failure_type_attempt_and_partial_results(build):
    agent = build(faults={"lookup_order": 2})
    rec = converse(agent, [DANA_VERIFY, "Where is ORD-5530?"])[1]
    first = _timeouts(rec)[0].payload
    assert first["errorCategory"] == "transient" and first["failureType"] == "timeout"
    assert first["isRetryable"] is True
    assert first["attempted"] == "lookup_order(order_id='ORD-5530')"
    assert first["partialResults"]["order_header"]["total"] == 89.5
    assert first["completedSteps"] == ["order_header"] and first["incompleteStep"] == "shipment"
    assert first["remediation"]


def test_retry_budget_flips_retryable_on_the_second_timeout(build):
    agent = build(faults={"lookup_order": 2})
    rec = converse(agent, [DANA_VERIFY, "Where is ORD-5530?"])[1]
    second = _timeouts(rec)[1].payload
    assert second["isRetryable"] is False and second["attemptNumber"] == 2
    assert "escalate" in second["remediation"]


def test_exhausted_retry_escalates_with_errors_and_facts_in_the_handoff(build):
    agent = build(faults={"lookup_order": 2})
    rec = converse(agent, [DANA_VERIFY, "Where is ORD-5530?"])[1]
    assert any(o.tool == "escalate_to_human" and o.input["reason_category"] == "unable_to_progress"
               for o in rec.outcomes)
    handoff = agent.session.state.tickets[-1]["handoff"]
    assert [e["failureType"] for e in handoff["recent_errors"]] == ["timeout", "timeout"]
    assert handoff["case_facts"]["customer"]["customer_id"] == "C-1001"


def test_reply_after_timeouts_uses_partials_and_invents_nothing(build):
    agent = build(faults={"lookup_order": 2})
    reply = converse(agent, [DANA_VERIFY, "Where is ORD-5530?"])[1].reply.lower()
    assert "timed out" in reply and "in_transit" in reply
    assert "latest scan" not in reply and "out for delivery" not in reply


def test_single_timeout_is_retried_and_recovers(build):
    agent = build(faults={"lookup_order": 1})
    rec = converse(agent, [DANA_VERIFY, "Where is ORD-5530?"])[1]
    lookups = [o for o in rec.outcomes if o.tool == "lookup_order"]
    assert [o.is_error for o in lookups] == [True, False]
    assert "escalate_to_human" not in rec.tools


def test_timed_out_refund_does_not_move_money():
    session = ToolSession(tool_timeout_s=0.2, faults=Faults(timeouts={"process_refund": 1}))
    session.call("get_customer", {"email": "dana.whitfield@example.com", "postal_code": "94107"})
    outcome = session.call("process_refund", {"customer_id": "C-1001", "order_id": "ORD-5521",
                                              "amount": 249.99, "reason": "t"})
    assert outcome.payload["failureType"] == "timeout"
    assert session.state.refunded["ORD-5521"] == 0.0 and session.state.refunds == []


def test_a_handler_bug_still_comes_back_structured(monkeypatch):
    def broken(*_):
        raise KeyError("boom")

    monkeypatch.setitem(handlers.HANDLERS, "lookup_order", broken)
    outcome = ToolSession().call("lookup_order", {"order_id": "ORD-5521"})
    assert outcome.payload["errorCategory"] == "internal"
    assert outcome.payload["attempted"] == "lookup_order(order_id='ORD-5521')"


def test_timeout_is_structured_over_mcp_stdio():
    env = {"SUPPORT_SIMULATE_TIMEOUT": "lookup_order", "SUPPORT_SIMULATE_TIMES": "1",
           "SUPPORT_TOOL_TIMEOUT": "0.5"}

    async def go():
        async with Client(server_params(env)) as client:
            return [await _call(client, "lookup_order", {"order_id": "ORD-5530"}) for _ in range(2)]

    failed, recovered = asyncio.run(go())
    assert failed.is_error
    assert failed.payload["failureType"] == "timeout"
    assert "order_header" in failed.payload["partialResults"]
    assert not recovered.is_error and recovered.payload["status"] == "in_transit"
