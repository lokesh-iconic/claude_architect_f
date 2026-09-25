"""Self-check 1: after 15+ turns, are the order amount and customer details still exact?"""

from __future__ import annotations

import threading

import pytest

from support_agent import trimming
from support_agent.backend import MockBackend
from support_agent.facts import OPEN_TAG
from support_agent.handlers import Faults, Progress, UpstreamState, get_customer, lookup_order
from support_agent.memory import SUMMARY_OPEN, MockSummarizer, paraphrase
from support_agent.scenarios import (
    LONG_CONVERSATION, PROBE_AMOUNT_TURN, PROBE_EMAIL_TURN, PROBE_REFUND_TURN, converse,
)


@pytest.fixture
def long_run(build):
    agent = build()
    return agent, converse(agent, LONG_CONVERSATION)


def test_long_conversation_is_really_long_and_really_compacted(long_run):
    agent, records = long_run
    assert len(records) >= 15
    assert agent.memory.compacted_turns == len(records) - agent.memory.keep_recent
    # The turn that looked up ORD-5521 is long gone from the verbatim window.
    assert all(ex.turn > 3 for ex in agent.memory.exchanges)


def test_case_facts_keep_the_exact_amount_and_date_after_compaction(long_run):
    _, records = long_run
    reply = records[PROBE_AMOUNT_TURN - 1].reply
    assert "$249.99" in reply and "2026-09-02" in reply


def test_case_facts_keep_the_customer_email_after_compaction(long_run):
    _, records = long_run
    assert "dana.whitfield@example.com" in records[PROBE_EMAIL_TURN - 1].reply


def test_refund_at_turn_18_uses_the_exact_amount_without_reverifying(long_run):
    agent, records = long_run
    rec = records[PROBE_REFUND_TURN - 1]
    refunds = [o for o in rec.outcomes if o.tool == "process_refund" and not o.is_error]
    assert [o.payload["amount"] for o in refunds] == [249.99]
    assert "get_customer" not in rec.tools


def test_probes_are_answered_from_case_facts_not_by_refetching(long_run):
    _, records = long_run
    lookups = [r.turn for r in records if any(
        o.tool == "lookup_order" and o.input.get("order_id") == "ORD-5521" for o in r.outcomes)]
    assert lookups == [3]


def test_case_facts_are_a_separate_system_block_in_every_request(build):
    agent = build()
    backend: MockBackend = agent.backend
    converse(agent, LONG_CONVERSATION)
    rendered = OPEN_TAG + "\nAuthoritative facts"
    for params in backend.calls:
        system_texts = [b["text"] for b in params["system"]]
        assert len(system_texts) == 2 and system_texts[1].startswith(rendered)
        assert rendered not in system_texts[0]
        # ...and never inside the (summarized) message history.
        assert rendered not in str(params["messages"])
    assert any(SUMMARY_OPEN in str(p["messages"]) for p in backend.calls)


def test_without_case_facts_the_same_run_drifts(build):
    """The ablation: identical conversation, no facts block -> the summary's paraphrase leaks through."""
    agent = build(case_facts=False)
    records = converse(agent, LONG_CONVERSATION)
    amount_reply = records[PROBE_AMOUNT_TURN - 1].reply
    assert "249.99" not in amount_reply and "about $250" in amount_reply
    assert "dana.whitfield@example.com" not in records[PROBE_EMAIL_TURN - 1].reply
    assert "process_refund" not in records[PROBE_REFUND_TURN - 1].tools


def test_the_summarizer_is_lossy_the_way_paraphrase_is():
    text = "ORD-5521 came to $249.99, placed 2026-09-02; email dana.whitfield@example.com."
    out = paraphrase(text)
    assert "249.99" not in out and "about $250" in out
    assert "2026-09-02" not in out and "September 2026" in out
    assert "@" not in out


def test_mock_is_stateless_it_only_knows_what_the_request_carries(build):
    agent = build()
    converse(agent, LONG_CONVERSATION[:PROBE_AMOUNT_TURN - 1])
    params = agent.build_request([{"role": "user", "content": LONG_CONVERSATION[PROBE_AMOUNT_TURN - 1]}])
    fresh = MockBackend()
    assert "249.99" in fresh.create(params).text
    params["system"] = params["system"][:1]
    assert "249.99" not in fresh.create(params).text


def test_tool_outputs_are_trimmed_before_entering_context(long_run):
    agent, _ = long_run
    ok = [o for o in agent.session.log if not o.is_error]
    raw = sum(o.raw_chars for o in ok)
    kept = sum(o.trimmed_chars for o in ok)
    assert kept < raw * 0.25
    for o in ok:
        # What goes back to the model as the tool_result is exactly the trimmed payload.
        assert len(o.content) == o.trimmed_chars < o.raw_chars


def test_every_case_fact_field_survives_trimming():
    raw = lookup_order({"order_id": "ORD-5521"}, UpstreamState(), Faults(), Progress(), threading.Event())
    trimmed = trimming.trim("lookup_order", raw)
    for field in ("order_id", "status", "placed_on", "delivered_on", "total", "currency",
                  "refunded_amount", "refundable_until", "items"):
        assert field in trimmed
    assert trimmed["total"] == 249.99
    for noisy in ("audit_log", "fraud_signals", "payment", "shipment"):
        assert noisy not in trimmed


def test_trimming_drops_contact_and_device_data_from_customer_lookups():
    raw = get_customer({"email": "dana.whitfield@example.com", "postal_code": "94107"},
                       UpstreamState(), Faults(), Progress(), threading.Event())
    trimmed = trimming.trim("get_customer", raw)
    assert trimmed["verified"] is True
    assert "phone" not in str(trimmed) and "devices" not in trimmed and "risk" not in trimmed


def test_summary_stays_bounded():
    summarizer = MockSummarizer()
    from support_agent.memory import Exchange

    summary = ""
    for n in range(40):
        summary = summarizer.summarize(summary, Exchange(n, f"question {n}", [], f"answer {n}"))
    assert summary.count("- Turn") == 12
    assert summary.startswith("(28 earlier turn(s) omitted)")
