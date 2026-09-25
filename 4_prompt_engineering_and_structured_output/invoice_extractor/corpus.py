"""Sample documents, their hand-labelled ground truth, and a synthetic generator.

The 12 files in `corpus/` are the varied set the brief asks for. The batch
path needs 100+ documents, which `synthesize()` produces deterministically
(same seed, same documents, same ground truth) so batch runs are comparable.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from .models import Document
from .schema import HEADER_FIELDS

HIGH_CONFIDENCE = 0.95
LOW_CONFIDENCE = 0.6

MOCK_FAULTS = ("drop_line_item", "fabricate_due_date", "other_without_detail")


@dataclass
class GroundTruth:
    doc_id: str
    expected: dict[str, Any]  # tool-input shape, confidences filled in
    mock_fault: str | None = None
    low_confidence: list[str] = field(default_factory=list)


def _expand(doc_id: str, raw: dict[str, Any]) -> GroundTruth:
    low = list(raw.get("low_confidence", []))
    expected: dict[str, Any] = {
        "document_type": raw["document_type"],
        "document_type_detail": raw["document_type_detail"],
    }
    for name in HEADER_FIELDS:
        value, evidence = raw[name]
        expected[name] = {
            "value": value,
            "evidence": evidence,
            "confidence": LOW_CONFIDENCE if name in low else HIGH_CONFIDENCE,
        }
    expected["line_items"] = [dict(li, confidence=HIGH_CONFIDENCE) for li in raw["line_items"]]
    return GroundTruth(doc_id, expected, raw.get("mock_fault"), low)


def load_corpus(corpus_dir: Path) -> tuple[list[Document], dict[str, GroundTruth]]:
    docs = [Document.from_path(p) for p in sorted(corpus_dir.glob("*.txt"))]
    truth: dict[str, GroundTruth] = {}
    gt_path = corpus_dir / "ground_truth.json"
    if gt_path.exists():
        raw = json.loads(gt_path.read_text(encoding="utf-8"))
        truth = {k: _expand(k, v) for k, v in raw.items() if not k.startswith("_")}
    return docs, truth


# --------------------------------------------------------------------------
# Synthetic documents for the batch path
# --------------------------------------------------------------------------

VENDORS = [
    ("Alder & Finch Stationers", "GBP"), ("Bluewater Marine Supply", "USD"),
    ("Cobalt Print Works", "EUR"), ("Driftwood Catering", "CAD"),
    ("Everline Cleaning Services", "AUD"), ("Foxglove Florists", "GBP"),
    ("Granite Peak Hardware", "USD"), ("Harbourlight Electrical", "EUR"),
]
ITEMS = [
    ("Printer paper, A4 ream", "goods"), ("Cleaning visit, 2 hrs", "services"),
    ("Courier delivery", "shipping"), ("Cable ties, pack of 100", "goods"),
    ("Consulting, per hour", "services"), ("Admin fee", "fee"),
    ("LED panel 600x600", "goods"), ("Site survey", "services"),
]
TAX_RATES = [Decimal("0"), Decimal("10"), Decimal("20")]


def _money(x: Decimal) -> Decimal:
    return x.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def synthesize(n: int, seed: int = 20260321) -> tuple[list[Document], dict[str, GroundTruth]]:
    rng = random.Random(seed)
    docs: list[Document] = []
    truth: dict[str, GroundTruth] = {}
    for i in range(n):
        doc_id = f"syn_{i:04d}"
        vendor, currency = VENDORS[i % len(VENDORS)]
        number = f"SYN-{rng.randint(10000, 99999)}-{i:04d}"
        day = 1 + (i % 27)
        issue = f"2026-03-{day:02d}"
        has_due = i % 4 != 0
        due = f"2026-04-{day:02d}"
        has_po = i % 3 == 0
        po = f"PO-{rng.randint(1000, 9999)}"
        rate = TAX_RATES[i % len(TAX_RATES)]

        lines, items = [], []
        for desc, category in rng.sample(ITEMS, rng.randint(2, 5)):
            qty = Decimal(rng.randint(1, 12))
            unit = _money(Decimal(rng.randint(250, 25000)) / 100)
            amount = _money(qty * unit)
            line = f"{desc:<30}{qty:>4} x {unit:>9} = {amount:>10}"
            lines.append(line)
            items.append({
                "description": desc, "quantity": float(qty), "unit_price": float(unit),
                "amount": float(amount), "category": category, "category_detail": None,
                "evidence": line, "confidence": HIGH_CONFIDENCE,
            })
        subtotal = _money(sum((Decimal(str(it["amount"])) for it in items), Decimal(0)))
        tax = _money(subtotal * rate / 100)
        total = subtotal + tax

        header = [vendor, f"INVOICE {number}", f"Date: {issue}"]
        if has_due:
            header.append(f"Due: {due}")
        if has_po:
            header.append(f"PO: {po}")
        footer = [
            f"Subtotal: {subtotal}",
            f"Tax ({rate}%): {tax}",
            f"Total {currency}: {total}",
        ]
        text = "\n".join(header + [""] + lines + [""] + footer) + "\n"
        docs.append(Document(doc_id, text, "synthetic"))

        def fv(value, evidence):
            return {"value": value, "evidence": evidence, "confidence": HIGH_CONFIDENCE}

        expected = {
            "document_type": "invoice",
            "document_type_detail": None,
            "vendor_name": fv(vendor, vendor),
            "invoice_number": fv(number, f"INVOICE {number}"),
            "invoice_date": fv(issue, f"Date: {issue}"),
            "due_date": fv(due, f"Due: {due}") if has_due else fv(None, None),
            "purchase_order": fv(po, f"PO: {po}") if has_po else fv(None, None),
            "currency": fv(currency, f"Total {currency}: {total}"),
            "subtotal": fv(float(subtotal), f"Subtotal: {subtotal}"),
            "tax": fv(float(tax), f"Tax ({rate}%): {tax}"),
            "total": fv(float(total), f"Total {currency}: {total}"),
            "line_items": items,
        }
        fault = None
        if i % 15 == 7 and len(items) >= 2:
            fault = "drop_line_item"
        elif i % 15 == 11 and not has_due:
            fault = "fabricate_due_date"
        truth[doc_id] = GroundTruth(doc_id, expected, fault, [])
    return docs, truth
