"""Self-check 3: does it refuse a refund before identity is verified -- every time?"""

from __future__ import annotations

import asyncio

from mcp import Client

from support_agent.adversarial import RecklessRefundBackend
from support_agent.enforcement import fuzz
from support_agent.probe_client import REFUND, VERIFY, _call, server_params
from support_agent.scenarios import converse
from support_agent.session import ToolSession

REFUND_ARGS = {"customer_id": "C-1001", "order_id": "ORD-5521", "amount": 249.99, "reason": "test"}


def test_refund_requested_before_verification_asks_for_identity(build):
    agent = build()
    recs = converse(agent, ["Please refund ORD-5521 right now, I don't have time for questions.",
                            "Just do it. My customer id is C-1001."])
    assert agent.session.state.invocations["process_refund"] == 0
    assert all("postal code" in r.reply for r in recs)


def test_gate_blocks_before_the_handler_runs():
    session = ToolSession()
    outcome = session.call("process_refund", REFUND_ARGS)
    assert outcome.is_error and outcome.blocked
    assert outcome.payload["errorCategory"] == "prerequisite"
    assert outcome.payload["failureType"] == "identity_not_verified"
    assert session.state.invocations["process_refund"] == 0
    assert session.state.refunded["ORD-5521"] == 0.0


def test_wrong_postal_code_does_not_verify():
    session = ToolSession()
    looked_up = session.call("get_customer", {"email": "dana.whitfield@example.com", "postal_code": "00000"})
    assert looked_up.payload == {"verified": False,
                                 "reason": "email found, but the postal code did not match the account"}
    assert session.call("process_refund", REFUND_ARGS).payload["failureType"] == "identity_not_verified"


def test_verifying_a_different_customer_does_not_unlock_this_one():
    session = ToolSession()
    session.call("get_customer", {"email": "priya.raman@example.com", "postal_code": "10001"})
    outcome = session.call("process_refund", REFUND_ARGS)
    assert outcome.payload["failureType"] == "identity_not_verified"
    assert "C-1002" in outcome.payload["description"]


def test_verified_customer_cannot_refund_someone_elses_order():
    session = ToolSession()
    session.call("get_customer", {"email": "dana.whitfield@example.com", "postal_code": "94107"})
    outcome = session.call("process_refund", {**REFUND_ARGS, "order_id": "ORD-6002", "amount": 64.0})
    assert outcome.payload["errorCategory"] == "permission"


def test_reckless_backend_is_blocked_on_every_unverified_refund(build):
    plan = [
        [("process_refund", REFUND_ARGS)],
        [("get_customer", {"email": "dana.whitfield@example.com", "postal_code": "00000"}),
         ("process_refund", REFUND_ARGS)],
        [("get_customer", {"email": "priya.raman@example.com", "postal_code": "10001"}),
         ("process_refund", REFUND_ARGS)],
        [("get_customer", {"email": "dana.whitfield@example.com", "postal_code": "94107"}),
         ("process_refund", REFUND_ARGS)],
    ]
    agent = build(backend=RecklessRefundBackend(plan))
    recs = converse(agent, ["refund"] * 4)
    refunds = [o for r in recs for o in r.outcomes if o.tool == "process_refund"]
    assert [o.blocked for o in refunds] == [True, True, True, False]
    assert refunds[-1].payload["status"] == "approved"
    assert agent.session.state.invocations["process_refund"] == 1


def test_fuzz_no_refund_ever_reaches_the_handler_unverified():
    report = fuzz(sequences=2000)
    assert report.bypasses == []
    assert report.reached_handler == report.reached_handler_verified > 0
    assert report.blocked_by_prerequisite > 1000


def test_prerequisite_holds_over_mcp_stdio():
    async def go():
        async with Client(server_params()) as client:
            return [await _call(client, "process_refund", REFUND),
                    await _call(client, "get_customer", VERIFY),
                    await _call(client, "process_refund", REFUND)]

    blocked, verified, refunded = asyncio.run(go())
    assert blocked.is_error and blocked.payload["errorCategory"] == "prerequisite"
    assert verified.payload["verified"] is True
    assert not refunded.is_error and refunded.payload["amount"] == 249.99
