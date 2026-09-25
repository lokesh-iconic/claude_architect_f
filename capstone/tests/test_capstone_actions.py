"""D4: schema-validated actions, few-shot examples, validation-retry, and the ledger."""

from __future__ import annotations

import pytest

from resolution_agent.tools.actions import SEMANTIC_CHECKS, generic_text
from resolution_agent.backends.adversarial import ScriptedToolBackend
from resolution_agent.tools.data import CUSTOMERS, ORDERS
from resolution_agent.evaluation.enforcement import fuzz
from resolution_agent.conversation.few_shot import EXAMPLES
from resolution_agent.conversation.prompts import SYSTEM_PROMPT
from resolution_agent.evaluation.scenarios import SCENARIOS, converse
from resolution_agent.tools.schema import CLIENT_SIDE_KEYWORDS, api_tool_specs, tool_specs, validate, validate_input
from resolution_agent.tools.session import ToolSession
from resolution_agent.evaluation.suites import BAD_HANDOFF, BAD_REFUND, GOOD_HANDOFF, GOOD_REFUND, VERIFY_ARGS, capture_scenarios


def _verified_session() -> ToolSession:
    session = ToolSession()
    session.begin_turn()
    session.call("get_customer", VERIFY_ARGS)
    session.call("lookup_order", {"order_id": "ORD-5521"})
    return session


def test_api_schemas_are_strict_and_carry_no_client_side_keywords():
    for spec in api_tool_specs():
        assert spec["strict"] is True
        assert CLIENT_SIDE_KEYWORDS.isdisjoint(str(spec["input_schema"]).replace("'", " ").split())
    assert "exclusiveMinimum" in str(tool_specs())


def test_validator_covers_the_keywords_the_schemas_use():
    schema = {"type": "object", "properties": {"a": {"type": ["string", "null"], "pattern": r"^C-\d{4}$"},
                                                "n": {"type": "number", "exclusiveMinimum": 0},
                                                "xs": {"type": "array", "items": {"type": "string", "minLength": 2}}},
              "required": ["a", "n", "xs"], "additionalProperties": False}
    assert validate(schema, {"a": None, "n": 1, "xs": ["ok"]}) == []
    codes = sorted(i.code for i in validate(schema, {"a": "C-1", "n": 0, "xs": ["x", 3], "z": 1}))
    assert codes == ["bad_format", "out_of_range", "too_short", "unexpected_field", "wrong_type"]


@pytest.mark.parametrize("example", [e for e in EXAMPLES if e.tool], ids=lambda e: e.name)
def test_few_shot_actions_pass_the_agents_own_validator(example):
    assert validate_input(example.tool, example.action) == []
    assert SEMANTIC_CHECKS[example.tool](example.action, example.facts) == []


def test_few_shot_examples_are_three_fictional_and_in_the_prompt():
    assert [e.name for e in EXAMPLES] == ["partial_refund_for_defect", "duplicate_charge_not_in_records",
                                          "ambiguous_order_reference"]
    for e in EXAMPLES:
        assert not set(e.facts.orders) & set(ORDERS) and e.facts.customer.customer_id not in CUSTOMERS
        assert e.customer in SYSTEM_PROMPT and e.reasoning in SYSTEM_PROMPT


def test_malformed_refund_is_rejected_before_the_handler_with_every_issue_listed():
    session = _verified_session()
    outcome = session.call("process_refund", BAD_REFUND)
    assert outcome.payload["failureType"] == "invalid_action_record" and outcome.payload["isRetryable"] is True
    assert sorted(i["code"] for i in outcome.payload["issues"]) == ["exceeds_refundable", "missing"]
    assert session.state.invocations["process_refund"] == 0 and session.ledger == []


def test_validation_retry_corrected_record_executes_and_ledger_keeps_the_rejection(build):
    agent = build(backend=ScriptedToolBackend([[("get_customer", VERIFY_ARGS), ("lookup_order", {"order_id": "ORD-5521"}),
                                                ("process_refund", BAD_REFUND), ("process_refund", GOOD_REFUND)]]))
    converse(agent, ["It arrived cracked, refund ORD-5521."])
    [entry] = agent.session.ledger
    assert agent.session.state.invocations["process_refund"] == 1
    assert entry.status == "executed" and entry.validation_attempts == 2 and entry.result_id == "RF-9001"
    assert {i["code"] for i in entry.rejected_attempts[0]} == {"missing", "exceeds_refundable"}


def test_refund_type_must_match_the_amount():
    session = _verified_session()
    partial_as_full = session.call("process_refund", {**GOOD_REFUND, "amount": 30.0})
    assert partial_as_full.payload["issues"][0]["code"] == "type_amount_mismatch"


def test_refund_must_be_grounded_in_a_fetched_order():
    session = ToolSession()
    session.begin_turn()
    session.call("get_customer", VERIFY_ARGS)
    outcome = session.call("process_refund", GOOD_REFUND)
    assert outcome.payload["issues"][0]["code"] == "not_in_case_facts"


def test_retry_budget_stops_on_repeated_issues_and_after_three_attempts():
    session = _verified_session()
    same = [session.call("process_refund", BAD_REFUND).payload for _ in range(2)]
    assert same[0]["isRetryable"] and not same[1]["isRetryable"] and same[1]["sameIssuesAsLastAttempt"]
    session.begin_turn()
    variants = [{**GOOD_REFUND, "amount": 300.0}, {**GOOD_REFUND, "currency": "EUR"},
                {**GOOD_REFUND, "refund_type": "partial"}]
    assert [session.call("process_refund", v).payload["isRetryable"] for v in variants] == [True, True, False]


def test_handoff_with_wrong_customer_and_boilerplate_is_rejected():
    session = ToolSession()
    session.begin_turn()
    session.call("get_customer", VERIFY_ARGS)
    outcome = session.call("escalate_to_human", BAD_HANDOFF)
    assert {i["code"] for i in outcome.payload["issues"]} == {
        "not_the_verified_customer", "contradicts_case_facts", "generic_text"}
    assert session.state.tickets == []
    assert not session.call("escalate_to_human", GOOD_HANDOFF).is_error


@pytest.mark.parametrize("text, generic", [
    ("please review", True), ("Please look into this ticket.", True), ("n/a", True), ("check", True),
    ("Check the processor for a second capture on ORD-5530.", False),
])
def test_generic_text_detector(text, generic):
    assert generic_text(text) is generic


def test_exhausted_handoff_budget_is_filed_by_the_system(build):
    agent = build(backend=ScriptedToolBackend([[("escalate_to_human", BAD_HANDOFF)] * 3]))
    [rec] = converse(agent, ["Please just get me a human."])
    [entry] = agent.session.ledger
    assert rec.system_handoff and entry.authored_by == "system" and entry.status == "executed"
    assert validate_input("escalate_to_human", entry.record) == []


def test_no_exceptions_every_action_in_the_scenario_set_has_a_valid_ledger_record(build):
    agents = capture_scenarios(build)
    runs = sum(a.session.state.invocations[t] for a in agents for t in ("process_refund", "escalate_to_human"))
    records = [r for a in agents for r in a.session.ledger]
    assert runs == len(records) >= len(SCENARIOS)
    assert all(validate_input(r.tool, r.record) == [] for r in records)


def test_no_exceptions_under_fuzz():
    report = fuzz(sequences=1000)
    assert report.bypasses == [] and report.invalid_ledger_records == 0
    assert report.action_handler_runs == report.ledger_records > 0 and report.rejected_by_validation > 0
