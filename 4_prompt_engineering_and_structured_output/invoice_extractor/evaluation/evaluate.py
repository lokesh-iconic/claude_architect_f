"""Score extractions against ground truth, with fabrication counted separately.

A "fabricated" field is one the ground truth says is absent (null) but the
extraction filled in. It is tracked apart from ordinary wrong values because
it is the failure the brief's first self-check asks about -- and the one a
downstream system is least likely to catch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .corpus import GroundTruth
from ..extraction.models import DocumentResult
from ..extraction.schema import HEADER_FIELDS


def _same(got: Any, want: Any) -> bool:
    if got is None or want is None:
        return got is None and want is None
    if isinstance(want, (int, float)) and not isinstance(want, bool):
        try:
            return abs(float(got) - float(want)) < 0.01
        except (TypeError, ValueError):
            return False
    return str(got).strip().casefold() == str(want).strip().casefold()


def classify(got: Any, want: Any) -> str:
    if _same(got, want):
        return "correct_null" if want is None else "correct"
    if want is None:
        return "fabricated"
    if got is None:
        return "missed"
    return "wrong"


@dataclass
class EvalSummary:
    documents: int = 0
    fields_checked: int = 0
    counts: dict[str, int] = field(default_factory=lambda: {
        "correct": 0, "correct_null": 0, "fabricated": 0, "missed": 0, "wrong": 0,
    })
    absent_fields: int = 0
    fabricated_first_attempt: list[str] = field(default_factory=list)
    fabricated_final: list[str] = field(default_factory=list)
    other_errors: list[str] = field(default_factory=list)
    line_item_count_mismatches: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "documents": self.documents,
            "fieldsChecked": self.fields_checked,
            "counts": self.counts,
            "absentFieldsInGroundTruth": self.absent_fields,
            "fabricatedOnFirstAttempt": self.fabricated_first_attempt,
            "fabricatedInFinalOutput": self.fabricated_final,
            "otherErrors": self.other_errors,
            "lineItemCountMismatches": self.line_item_count_mismatches,
        }


def evaluate(results: list[DocumentResult], truth: dict[str, GroundTruth]) -> EvalSummary:
    summary = EvalSummary()
    for result in results:
        gt = truth.get(result.doc_id)
        if gt is None:
            continue
        summary.documents += 1
        first = result.attempts[0].extraction if result.attempts else None
        for name in HEADER_FIELDS:
            want = gt.expected[name]["value"]
            if first is not None and classify(first[name]["value"], want) == "fabricated":
                summary.fabricated_first_attempt.append(f"{result.doc_id}.{name}")
            if result.extraction is None:
                continue
            outcome = classify(result.extraction.value(name), want)
            summary.fields_checked += 1
            summary.absent_fields += want is None
            summary.counts[outcome] += 1
            if outcome == "fabricated":
                summary.fabricated_final.append(f"{result.doc_id}.{name}")
            elif outcome in ("missed", "wrong"):
                summary.other_errors.append(f"{result.doc_id}.{name}: {outcome}")
        if result.extraction is not None and len(result.extraction.line_items) != len(gt.expected["line_items"]):
            summary.line_item_count_mismatches.append(result.doc_id)
    return summary
