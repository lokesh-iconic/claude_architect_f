"""The batch path: custom_id correlation and failed-only resubmission."""

from __future__ import annotations

import pytest

from invoice_extractor.backend import MockBackend
from invoice_extractor.batch import (
    CUSTOM_ID_RE,
    BatchItemResult,
    MockBatchClient,
    run_batch,
    wait_for,
)
from invoice_extractor.corpus import synthesize
from invoice_extractor.errors import ExtractionError
from invoice_extractor.pipeline import build_params


@pytest.fixture(scope="module")
def synthetic():
    return synthesize(120)


def test_batch_set_is_large_and_custom_ids_are_valid(synthetic):
    docs, truth = synthetic
    assert len(docs) >= 100
    assert all(CUSTOM_ID_RE.match(d.doc_id) for d in docs)
    assert len({d.doc_id for d in docs}) == len(docs)


def test_batch_resubmits_only_the_documents_that_failed(synthetic, settings):
    docs, truth = synthetic
    client = MockBatchClient(MockBackend(truth))
    run = run_batch(docs, client, settings, sleep=lambda s: None)

    assert client.submissions[0] == [d.doc_id for d in docs]
    assert len(client.submissions) >= 2
    for prev, submitted in zip(run.rounds, client.submissions[1:]):
        assert set(submitted) == set(prev.resubmit)
        assert set(submitted).isdisjoint(prev.finished)
        assert set(submitted).isdisjoint(prev.rejected)
    first = run.rounds[0]
    assert first.transient_retry and first.validation_retry  # both kinds of failure occurred
    assert len(client.submissions[1]) < len(docs) // 4


def test_rejected_requests_are_not_resubmitted_but_reach_review(synthetic, settings):
    docs, truth = synthetic
    client = MockBatchClient(MockBackend(truth))
    run = run_batch(docs, client, settings, sleep=lambda s: None)
    rejected = run.rounds[0].rejected
    assert rejected
    later = {cid for sub in client.submissions[1:] for cid in sub}
    assert later.isdisjoint(rejected)
    assert all(run.results[cid].status == "review" for cid in rejected)


def test_validation_resubmission_carries_the_feedback_turn(synthetic, settings):
    docs, truth = synthetic
    client = MockBatchClient(MockBackend(truth))
    run = run_batch(docs, client, settings, sleep=lambda s: None)
    cid = run.rounds[0].validation_retry[0]
    params = next(r["params"] for r in client._batches[run.rounds[1].batch_id] if r["custom_id"] == cid)
    roles = [m["role"] for m in params["messages"]]
    assert roles == ["user", "assistant", "user"]
    assert params["messages"][2]["content"][0]["is_error"] is True
    assert run.results[cid].status == "accepted"


class ReversingClient(MockBatchClient):
    """Returns rows in reverse order and swaps nothing else -- positional joins would misattribute."""

    def results(self, batch_id):
        return list(reversed(list(super().results(batch_id))))


def test_results_are_correlated_by_custom_id_not_position(synthetic, settings):
    docs, truth = synthetic
    run = run_batch(docs[:30], ReversingClient(MockBackend(truth)), settings, sleep=lambda s: None)
    for cid, result in run.results.items():
        if result.extraction is not None:
            assert result.extraction.value("invoice_number") == truth[cid].expected["invoice_number"]["value"]


def test_every_batch_request_uses_the_strict_forced_tool(synthetic, settings):
    docs, _ = synthetic
    params = build_params(settings, [{"role": "user", "content": "x"}])
    assert params["tools"][0]["strict"] is True
    assert params["tool_choice"]["type"] == "tool"


class StuckClient:
    def status(self, batch_id):
        return "in_progress"


def test_polling_has_a_deadline_and_fails_with_a_structured_error(settings):
    ticks = iter(range(0, 10_000, 10))
    settings.batch_poll_timeout_s = 30
    with pytest.raises(ExtractionError) as info:
        wait_for(StuckClient(), "msgbatch_x", settings, sleep=lambda s: None, clock=lambda: next(ticks))
    payload = info.value.to_payload()
    assert payload["errorCategory"] == "transient" and payload["isRetryable"] is True
    assert "msgbatch_x" in payload["remediation"]


def test_missing_result_rows_are_treated_as_transient_and_resubmitted(synthetic, settings):
    docs, truth = synthetic

    class DropsOne(MockBatchClient):
        def results(self, batch_id):
            rows = list(super().results(batch_id))
            return rows[1:] if batch_id.endswith("_01") else rows

    client = DropsOne(MockBackend(truth))
    run = run_batch(docs[:20], client, settings, sleep=lambda s: None)
    assert len(run.rounds[0].missing) == 1
    assert run.rounds[0].missing[0] in client.submissions[1]


def test_batch_item_rejection_detection():
    assert BatchItemResult("a", "errored", error_type="invalid_request").request_rejected
    assert BatchItemResult("a", "errored", error_type="invalid_request_error").request_rejected
    assert not BatchItemResult("a", "errored", error_type="api_error").request_rejected
    assert not BatchItemResult("a", "expired").request_rejected
