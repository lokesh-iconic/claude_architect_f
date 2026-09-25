"""Semantic validation of an extraction against its source document.

The schema guarantees shape; this module checks meaning. Each issue is
classified up front as retryable or not, because the two need opposite
handling: a formatting slip or a dropped line is worth sending back with the
specific error, while a field the document never states will come back null
however many times you ask -- that goes to a human instead.
"""

from __future__ import annotations

import re
from datetime import date

from .errors import ValidationIssue
from .models import Extraction
from .schema import DOCUMENT_TYPES, LINE_CATEGORIES, REQUIRED_FOR_POSTING

TOLERANCE = 0.015
DATE_FIELDS = ("invoice_date", "due_date")
NUMERIC_FIELDS = ("subtotal", "tax", "total")
# Values that must be copied character-for-character, so they must appear in their quote.
IDENTIFIER_FIELDS = ("invoice_number", "purchase_order")


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def _parse_number(token: str, decimal: str) -> float | None:
    thousands = "," if decimal == "." else "."
    cleaned = token.replace(thousands, "").replace(decimal, ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def amount_in_text(value: float, text: str) -> bool:
    """True if `value` is printed in `text`, in either 1,234.56 or 1.234,56 notation."""
    target = abs(float(value))
    for token in re.findall(r"\d[\d.,]*\d|\d", text):
        for decimal in (".", ","):
            parsed = _parse_number(token, decimal)
            if parsed is not None and abs(parsed - target) < 0.005:
                return True
    return False


def _issue(code, kind, field, message, retryable, remediation="", signature=()) -> ValidationIssue:
    return ValidationIssue(code, kind, field, message, retryable, remediation, tuple(signature))


def check_format(ex: Extraction) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if ex.document_type not in DOCUMENT_TYPES:
        issues.append(_issue("bad_enum", "format", "document_type",
                             f"{ex.document_type!r} is not one of {DOCUMENT_TYPES}", True))
    if ex.document_type == "other" and not ex.document_type_detail:
        issues.append(_issue(
            "other_without_detail", "format", "document_type_detail",
            "document_type is 'other' but document_type_detail is empty", True,
            "Give a short label for what the document calls itself.",
        ))
    for name in DATE_FIELDS:
        v = ex.value(name)
        if v is None:
            continue
        try:
            date.fromisoformat(str(v))
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(v)):
                raise ValueError
        except ValueError:
            issues.append(_issue("bad_date_format", "format", name,
                                 f"{v!r} is not a YYYY-MM-DD date", True,
                                 "Convert the printed date to YYYY-MM-DD.", [v]))
    cur = ex.value("currency")
    if cur is not None and not re.fullmatch(r"[A-Z]{3}", str(cur)):
        issues.append(_issue("bad_currency", "format", "currency",
                             f"{cur!r} is not an ISO 4217 code", True,
                             "Use the three-letter code (e.g. '$' on a US invoice -> USD).", [cur]))
    for name, fv in ex.fields.items():
        if not 0.0 <= fv.confidence <= 1.0:
            issues.append(_issue("confidence_out_of_range", "format", name,
                                 f"confidence {fv.confidence} is outside 0-1", True))
    for i, li in enumerate(ex.line_items):
        where = f"line_items[{i}]"
        if li.category not in LINE_CATEGORIES:
            issues.append(_issue("bad_enum", "format", f"{where}.category",
                                 f"{li.category!r} is not one of {LINE_CATEGORIES}", True))
        if li.category == "other" and not li.category_detail:
            issues.append(_issue(
                "other_without_detail", "format", f"{where}.category_detail",
                f"line {i} ({li.description!r}) has category 'other' but no category_detail", True,
                "Add a short free-text label describing what this line is.",
            ))
    return issues


def check_grounding(ex: Extraction, source: str) -> list[ValidationIssue]:
    """Every non-null value must be backed by a verbatim quote from the source."""
    issues: list[ValidationIssue] = []
    norm_source = normalize(source)
    null_hint = "If the document does not state this, set value and evidence to null."
    for name, fv in ex.fields.items():
        if fv.value is None:
            continue
        if not fv.evidence:
            issues.append(_issue("missing_evidence", "grounding", name,
                                 f"{name} has value {fv.value!r} but no evidence quote", True,
                                 f"Quote the text that states it. {null_hint}", [fv.value]))
            continue
        if normalize(fv.evidence) not in norm_source:
            issues.append(_issue("evidence_not_in_source", "grounding", name,
                                 f"the quote {fv.evidence!r} for {name} does not appear in the document", True,
                                 f"Quote the document verbatim. {null_hint}", [fv.value, fv.evidence]))
            continue
        if name in NUMERIC_FIELDS and not amount_in_text(float(fv.value), fv.evidence):
            issues.append(_issue("value_not_in_evidence", "grounding", name,
                                 f"{name}={fv.value} does not match its quote {fv.evidence!r}", True,
                                 "Copy the number shown in the quote.", [fv.value]))
        if name in IDENTIFIER_FIELDS and normalize(str(fv.value)) not in normalize(fv.evidence):
            issues.append(_issue("value_not_in_evidence", "grounding", name,
                                 f"{name}={fv.value!r} does not appear in its quote {fv.evidence!r}", True,
                                 "Copy the identifier exactly as printed.", [fv.value]))
    for i, li in enumerate(ex.line_items):
        where = f"line_items[{i}]"
        if normalize(li.evidence) not in norm_source:
            issues.append(_issue("evidence_not_in_source", "grounding", where,
                                 f"line {i} quote {li.evidence!r} does not appear in the document", True,
                                 "Quote the line as printed, or drop it if it is not in the document.",
                                 [li.evidence]))
        elif not amount_in_text(li.amount, li.evidence):
            issues.append(_issue("value_not_in_evidence", "grounding", where,
                                 f"line {i} amount {li.amount} does not match its quote {li.evidence!r}", True,
                                 "Copy the line total shown in the quote.", [li.amount]))
    return issues


def check_arithmetic(ex: Extraction) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    subtotal, tax, total = ex.value("subtotal"), ex.value("tax"), ex.value("total")
    feedback = (
        "Re-read the document: a line may have been dropped, duplicated, or mis-keyed. "
        "If the document itself does not add up, return the figures exactly as printed -- "
        "the discrepancy will be sent to human review."
    )
    for i, li in enumerate(ex.line_items):
        if li.quantity is not None and li.unit_price is not None:
            expected = round(li.quantity * li.unit_price, 2)
            if abs(expected - li.amount) > TOLERANCE:
                issues.append(_issue("line_amount_mismatch", "arithmetic", f"line_items[{i}]",
                                     f"line {i}: {li.quantity} x {li.unit_price} = {expected}, but amount is {li.amount}",
                                     True, feedback, [li.quantity, li.unit_price, li.amount]))
    if ex.line_items:
        items_sum = round(sum(li.amount for li in ex.line_items), 2)
        target_name, target = None, None
        if subtotal is not None:
            target_name, target = "subtotal", float(subtotal)
        elif total is not None:
            target_name, target = "total", float(total) - (float(tax) if tax is not None else 0.0)
        if target is not None and abs(items_sum - target) > TOLERANCE:
            issues.append(_issue(
                "line_items_sum_mismatch", "arithmetic", "line_items",
                f"{len(ex.line_items)} line items sum to {items_sum:.2f} but the {target_name} implies {target:.2f} "
                f"(difference {target - items_sum:+.2f})",
                True, feedback, [items_sum, target, len(ex.line_items)],
            ))
    if subtotal is not None and total is not None:
        expected_total = round(float(subtotal) + (float(tax) if tax is not None else 0.0), 2)
        if abs(expected_total - float(total)) > TOLERANCE:
            issues.append(_issue("total_mismatch", "arithmetic", "total",
                                 f"subtotal {subtotal} + tax {tax} = {expected_total}, but total is {total}",
                                 True, feedback, [subtotal, tax, total]))
    return issues


def check_required(ex: Extraction) -> list[ValidationIssue]:
    """A null required field is the document's gap, not the model's -- never retried."""
    return [
        _issue("absent_from_source", "absent", name,
               f"{name} is required for posting but the extraction reports it absent", False,
               "Route to human review; retrying cannot supply data the document lacks.")
        for name in REQUIRED_FOR_POSTING
        if ex.value(name) is None
    ]


def validate(ex: Extraction, source: str) -> list[ValidationIssue]:
    return check_format(ex) + check_grounding(ex, source) + check_arithmetic(ex) + check_required(ex)
