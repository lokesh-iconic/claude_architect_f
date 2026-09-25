"""D2: tool scope, descriptions, structured errors, and the same guarantees over MCP stdio."""

from __future__ import annotations

import asyncio
import json

from resolution_agent.tools.handlers import Faults
from resolution_agent.evaluation.probe_client import FOUR_TOOLS, mcp_checks, probe
from resolution_agent.evaluation.scenarios import converse
from resolution_agent.tools.schema import DESCRIPTIONS
from resolution_agent.tools.session import ToolSession
from resolution_agent.config.settings import MODULE_DIR
from resolution_agent.evaluation.suites import GOOD_REFUND, SELECTION, VERIFY_ARGS


def test_scope_agent_is_offered_exactly_the_four_tools(build):
    tools = sorted(t["name"] for t in build().build_request([])["tools"])
    assert tools == FOUR_TOOLS


def test_scope_any_other_tool_is_refused_with_a_structured_error():
    outcome = ToolSession().call("update_account_email", {"email": "new@example.com"})
    assert outcome.payload["errorCategory"] == "validation"
    assert outcome.payload["failureType"] == "tool_out_of_scope" and outcome.payload["isRetryable"] is False


def test_scope_project_registers_only_the_support_server():
    config = json.loads((MODULE_DIR / ".mcp.json").read_text(encoding="utf-8"))
    assert list(config["mcpServers"]) == ["support-resolution"]
    assert all(v.startswith("${") for v in config["mcpServers"]["support-resolution"]["env"].values())


def test_descriptions_state_what_each_tool_is_not_for():
    assert "changes nothing" in DESCRIPTIONS["get_customer"]
    assert "never moves money" in DESCRIPTIONS["lookup_order"]
    assert "Not for price adjustments" in DESCRIPTIONS["process_refund"]
    assert "not a way to finish" in DESCRIPTIONS["escalate_to_human"]


def test_selection_near_miss_requests_pick_the_right_tool(build):
    for label, setup, request, must, must_not in SELECTION:
        tools = converse(build(), [*setup, request])[-1].tools
        assert must in tools and must_not not in tools, label


def test_every_error_category_has_error_category_retryable_and_description():
    session = ToolSession(tool_timeout_s=0.3, faults=Faults(timeouts={"lookup_order": 1}))
    session.begin_turn()
    payloads = [session.call("process_refund", GOOD_REFUND).payload]
    session.call("get_customer", VERIFY_ARGS)
    payloads.append(session.call("lookup_order", {"order_id": "ORD-5530"}).payload)
    for oid in ("ORD-6002", "ORD-4410"):
        session.call("lookup_order", {"order_id": oid})
    payloads += [session.call("process_refund", {**GOOD_REFUND, "amount": 999.0}).payload,
                 session.call("process_refund", {**GOOD_REFUND, "order_id": "ORD-6002", "amount": 64.0}).payload,
                 session.call("process_refund", {**GOOD_REFUND, "order_id": "ORD-4410", "amount": 39.0}).payload]
    assert [p["errorCategory"] for p in payloads] == ["prerequisite", "transient", "validation", "permission", "policy"]
    for p in payloads:
        assert isinstance(p["isRetryable"], bool) and p["description"] and p["attempted"]


def test_mcp_stdio_carries_scope_gate_validation_and_timeouts():
    checks, _ = mcp_checks(asyncio.run(probe()))
    failed = [(c.claim, c.detail) for c in checks if not c.passed]
    assert not failed
