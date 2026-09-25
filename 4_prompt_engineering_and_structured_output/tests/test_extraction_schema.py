"""The output contract: strict schema, nullable fields, enum + detail, few-shot, corpus."""

from __future__ import annotations

from invoice_extractor.evaluation.corpus import MOCK_FAULTS
from invoice_extractor.extraction.few_shot import EXAMPLES
from invoice_extractor.extraction.models import Document, Extraction
from invoice_extractor.extraction.prompts import system_prompt
from invoice_extractor.extraction.schema import (
    HEADER_FIELDS,
    LINE_CATEGORIES,
    DOCUMENT_TYPES,
    TOOL_NAME,
    extraction_tool,
    tool_choice_for,
)
from invoice_extractor.extraction.validation import validate


def _walk_objects(schema):
    if schema.get("type") == "object":
        yield schema
        for prop in schema["properties"].values():
            yield from _walk_objects(prop)
    elif schema.get("type") == "array":
        yield from _walk_objects(schema["items"])


def test_tool_is_strict_and_every_object_is_closed_and_fully_required():
    tool = extraction_tool()
    assert tool["strict"] is True
    for obj in _walk_objects(tool["input_schema"]):
        assert obj["additionalProperties"] is False
        assert set(obj["required"]) == set(obj["properties"])


def test_optional_fields_are_nullable_not_omittable():
    props = extraction_tool()["input_schema"]["properties"]
    for name in HEADER_FIELDS:
        value_type = props[name]["properties"]["value"]["type"]
        assert "null" in value_type, name
        assert "null" in props[name]["properties"]["evidence"]["type"]
    line = props["line_items"]["items"]["properties"]
    assert "null" in line["quantity"]["type"] and "null" in line["unit_price"]["type"]
    assert "null" not in line["amount"]["type"]


def test_enums_use_other_plus_free_text_detail():
    props = extraction_tool()["input_schema"]["properties"]
    assert "other" in DOCUMENT_TYPES and "other" in LINE_CATEGORIES
    assert props["document_type"]["enum"] == DOCUMENT_TYPES
    assert "null" in props["document_type_detail"]["type"]
    line = props["line_items"]["items"]["properties"]
    assert line["category"]["enum"] == LINE_CATEGORIES
    assert "null" in line["category_detail"]["type"]


def test_tool_choice_is_forced_except_on_models_that_reject_it():
    assert tool_choice_for("claude-opus-5") == {"type": "tool", "name": TOOL_NAME}
    assert tool_choice_for("claude-opus-5-5") == {"type": "auto"}


def test_few_shot_has_two_to_four_examples_and_all_are_in_the_system_prompt():
    assert 2 <= len(EXAMPLES) <= 4
    prompt = system_prompt()
    for example in EXAMPLES:
        assert example.name in prompt
        assert example.document.strip().splitlines()[0] in prompt


def test_few_shot_examples_pass_the_pipelines_own_validator():
    for example in EXAMPLES:
        extraction = Extraction.from_tool_input(example.extraction)
        retryable = [i for i in validate(extraction, example.document) if i.retryable]
        assert retryable == [], (example.name, [i.message for i in retryable])


def test_few_shot_cover_null_fields_other_category_and_negative_amounts():
    extractions = [e.extraction for e in EXAMPLES]
    assert any(ex[f]["value"] is None for ex in extractions for f in HEADER_FIELDS)
    assert any(li["category"] == "other" and li["category_detail"] for ex in extractions for li in ex["line_items"])
    assert any(ex["total"]["value"] < 0 for ex in extractions)


def test_corpus_has_10_to_15_documents_each_with_ground_truth(corpus):
    docs, truth = corpus
    assert 10 <= len(docs) <= 15
    assert set(docs) == set(truth)
    assert {gt.mock_fault for gt in truth.values()} - {None} <= set(MOCK_FAULTS)


def test_ground_truth_is_grounded_in_its_own_document(corpus):
    """Every labelled evidence quote exists in the source -- else the mock would be flagged as fabricating."""
    docs, truth = corpus
    for doc_id, gt in truth.items():
        extraction = Extraction.from_tool_input(gt.expected)
        grounding = [i for i in validate(extraction, docs[doc_id].text) if i.kind == "grounding"]
        assert grounding == [], (doc_id, [i.message for i in grounding])


def test_grounding_check_flags_a_value_whose_quote_is_not_in_the_source(corpus):
    docs, truth = corpus
    data = truth["inv_002_missing_due_date"].expected
    data = {**data, "due_date": {"value": "2026-03-16", "evidence": "Payment due: 2026-03-16", "confidence": 0.9}}
    issues = validate(Extraction.from_tool_input(data), docs["inv_002_missing_due_date"].text)
    codes = {(i.code, i.field) for i in issues}
    assert ("evidence_not_in_source", "due_date") in codes


def test_other_without_detail_is_a_retryable_format_issue():
    doc = Document("d", "ACME\nINVOICE A-1\nMystery charge 10.00\nTotal USD 10.00\n")
    data = {
        "document_type": "invoice", "document_type_detail": None,
        **{f: {"value": None, "evidence": None, "confidence": 0.9} for f in HEADER_FIELDS},
        "line_items": [{
            "description": "Mystery charge", "quantity": None, "unit_price": None, "amount": 10.0,
            "category": "other", "category_detail": None, "evidence": "Mystery charge 10.00", "confidence": 0.9,
        }],
    }
    issues = validate(Extraction.from_tool_input(data), doc.text)
    other = [i for i in issues if i.code == "other_without_detail"]
    assert other and other[0].retryable and other[0].kind == "format"


def test_european_number_format_is_recognised_as_grounded():
    from invoice_extractor.extraction.validation import amount_in_text

    assert amount_in_text(2165.80, "Gesamtbetrag: 2.165,80 EUR")
    assert amount_in_text(1297.40, "Total USD: 1,297.40")
    assert amount_in_text(-96.80, "Net amount: -96.80")
    assert not amount_in_text(2165.80, "Gesamtbetrag: 2.156,80 EUR")
