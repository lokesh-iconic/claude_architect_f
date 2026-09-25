"""The validation-retry loop and confidence routing, against the mock backend."""

from __future__ import annotations

import copy

from invoice_extractor.backends.backend import MockBackend, ModelTurn
from invoice_extractor.evaluation.corpus import GroundTruth
from invoice_extractor.evaluation.evaluate import evaluate
from invoice_extractor.extraction.pipeline import PERSISTED, extract_all, extract_document
from invoice_extractor.extraction.schema import TOOL_NAME


def _tool_results(message):
    content = message["content"]
    return [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"] if isinstance(content, list) else []


def test_missing_field_returns_null_not_a_fabricated_value(corpus, settings):
    docs, truth = corpus
    result = extract_document(docs["inv_002_missing_due_date"], MockBackend(truth), settings)
    first = result.attempts[0]
    assert first.extraction["due_date"]["value"] is not None  # the scripted fabrication
    assert "evidence_not_in_source" in {i.code for i in first.issues}
    assert result.extraction.value("due_date") is None
    assert result.status == "accepted"


def test_no_fabricated_fields_survive_the_pipeline_on_the_corpus(corpus, settings):
    docs, truth = corpus
    results = extract_all(list(docs.values()), MockBackend(truth), settings)
    summary = evaluate(results, truth)
    assert summary.fabricated_first_attempt  # the mock does fabricate once...
    assert summary.fabricated_final == []  # ...and the grounding check removes it
    assert summary.counts["correct_null"] == summary.absent_fields


def test_retry_sends_document_failed_extraction_and_specific_error(corpus, settings):
    docs, truth = corpus
    backend = MockBackend(truth)
    result = extract_document(docs["inv_005_five_line_freight"], backend, settings)
    assert result.attempt_count == 2 and result.status == "accepted"

    retry = backend.calls[1]["messages"]
    assert retry[0]["role"] == "user" and docs["inv_005_five_line_freight"].text in retry[0]["content"]
    assert retry[1]["role"] == "assistant"
    failed = next(b for b in retry[1]["content"] if b["type"] == "tool_use")
    assert failed["name"] == TOOL_NAME and len(failed["input"]["line_items"]) == 4
    [tool_result] = _tool_results(retry[2])
    assert tool_result["tool_use_id"] == failed["id"] and tool_result["is_error"] is True
    assert "line_items_sum_mismatch" in tool_result["content"]
    assert "difference +265.00" in tool_result["content"]  # the dropped "Port handling" line


def test_retry_without_the_specific_error_does_not_fix_the_mock(corpus, settings):
    """Guards the mock itself: a bare re-ask must not be rewarded, or the retry test proves nothing."""
    docs, truth = corpus
    backend = MockBackend(truth)
    first = backend.respond({"messages": [{"role": "user", "content": '<document id="inv_005_five_line_freight">'}],
                             "tools": [{"name": TOOL_NAME}]})
    bare = [
        {"role": "user", "content": '<document id="inv_005_five_line_freight">'},
        {"role": "assistant", "content": first.content},
        {"role": "user", "content": "try again"},
    ]
    again = backend.respond({"messages": bare, "tools": [{"name": TOOL_NAME}]})
    assert len(again.tool_input["line_items"]) == 4


def test_absent_required_field_is_routed_to_review_without_retrying(corpus, settings):
    docs, truth = corpus
    backend = MockBackend(truth)
    result = extract_document(docs["inv_006_receipt_no_number"], backend, settings)
    assert result.attempt_count == 1
    assert len(backend.calls) == 1
    assert result.status == "review"
    assert "absent_from_source: invoice_number" in result.review_reasons


def test_absent_issues_are_withheld_from_retry_feedback(corpus, settings):
    """Telling the model a required field is missing would invite it to invent one."""
    docs, truth = corpus
    gt = truth["inv_006_receipt_no_number"]
    faulty = GroundTruth(gt.doc_id, gt.expected, "fabricate_due_date", [])
    backend = MockBackend({gt.doc_id: faulty})
    extract_document(docs[gt.doc_id], backend, settings)
    feedback = _tool_results(backend.calls[1]["messages"][-1])[0]["content"]
    assert "evidence_not_in_source" in feedback
    assert "absent_from_source" not in feedback


def test_source_inconsistency_stops_after_one_unchanged_retry(corpus, settings):
    docs, truth = corpus
    settings.max_attempts = 5
    result = extract_document(docs["inv_007_source_inconsistent"], MockBackend(truth), settings)
    assert result.attempt_count == 2  # not 5: the repeat proves retrying won't help
    assert result.status == "review"
    assert any(r.startswith(PERSISTED) for r in result.review_reasons)


def test_fixable_format_error_is_corrected_by_retry(corpus, settings):
    docs, truth = corpus
    result = extract_document(docs["inv_008_statement_other_category"], MockBackend(truth), settings)
    assert [sorted({i.code for i in a.issues}) for a in result.attempts] == [["other_without_detail"], []]
    assert result.status == "accepted"


def test_low_confidence_field_routes_to_review_and_threshold_is_respected(corpus, settings):
    docs, truth = corpus
    result = extract_document(docs["inv_009_ocr_noisy"], MockBackend(truth), settings)
    assert result.status == "review"
    assert {f for f, _ in result.low_confidence} == {"vendor_name", "invoice_date"}

    settings.confidence_threshold = 0.5
    relaxed = extract_document(docs["inv_009_ocr_noisy"], MockBackend(truth), settings)
    assert relaxed.status == "accepted"


def test_corpus_routing_outcomes(corpus, settings):
    docs, truth = corpus
    results = {r.doc_id: r for r in extract_all(list(docs.values()), MockBackend(truth), settings)}
    review = {d for d, r in results.items() if r.status == "review"}
    assert review == {
        "inv_006_receipt_no_number", "inv_007_source_inconsistent",
        "inv_009_ocr_noisy", "inv_012_email_body",
    }


class NoToolThenGood:
    name = "scripted"

    def __init__(self, good: dict):
        self.good = good
        self.calls = 0

    def create(self, params):
        self.calls += 1
        if self.calls == 1:
            return ModelTurn("end_turn", None, None, [{"type": "text", "text": "Here is the data..."}])
        return ModelTurn("tool_use", "t2", copy.deepcopy(self.good),
                         [{"type": "tool_use", "id": "t2", "name": TOOL_NAME, "input": self.good}])


def test_a_turn_without_the_tool_call_is_retried(corpus, settings):
    docs, truth = corpus
    backend = NoToolThenGood(truth["inv_001_standard"].expected)
    result = extract_document(docs["inv_001_standard"], backend, settings)
    assert backend.calls == 2
    assert result.attempts[0].issues[0].code == "no_tool_call"
    assert result.status == "accepted"
