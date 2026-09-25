"""Few-shot examples for the extraction prompt.

Chosen for the cases the instructions alone handle worst, not for typical
invoices (the model already does those):

1. A credit memo -- negative signs, and header fields (PO, due date) that
   are absent and must come back null rather than be borrowed from the
   original invoice it references.
2. A German invoice -- `1.234,56` number format, DD.MM.YYYY dates, and a
   line (a keg deposit) that fits no fixed category, so it shows the
   `other` + `category_detail` pattern.
3. An informal email -- no invoice number, no date, no table, a currency
   written as a word: the example where most of the header is null and the
   confidence on what *is* filled in is honestly low.

None of these documents is in the evaluation corpus. A test checks each
expected extraction passes the pipeline's own validator against its own
document, so an example can't teach something the validator would reject.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class FewShotExample:
    name: str
    lesson: str
    document: str
    extraction: dict[str, Any]


def _f(value, evidence, confidence) -> dict[str, Any]:
    return {"value": value, "evidence": evidence, "confidence": confidence}


def _null(confidence: float = 0.95) -> dict[str, Any]:
    return {"value": None, "evidence": None, "confidence": confidence}


def _line(description, quantity, unit_price, amount, category, evidence,
          confidence=0.95, detail=None) -> dict[str, Any]:
    return {
        "description": description, "quantity": quantity, "unit_price": unit_price,
        "amount": amount, "category": category, "category_detail": detail,
        "evidence": evidence, "confidence": confidence,
    }


CREDIT_MEMO = FewShotExample(
    name="credit_memo_with_absent_fields",
    lesson="Credits are negative; PO and due date are absent, so they are null -- not copied from the referenced invoice.",
    document="""CREDIT MEMO
Lumen Signage Ltd
Memo ref: LS-CM-219
Issued 2025-11-04
Against invoice LS-3310

Returned: 2 x acrylic panel 600x400 @ 42.00   -84.00
Restocking fee                                  12.00

Net: -72.00
VAT 20%: -14.40
Credit total: -86.40 GBP
""",
    extraction={
        "document_type": "credit_note",
        "document_type_detail": None,
        "vendor_name": _f("Lumen Signage Ltd", "Lumen Signage Ltd", 0.97),
        "invoice_number": _f("LS-CM-219", "Memo ref: LS-CM-219", 0.95),
        "invoice_date": _f("2025-11-04", "Issued 2025-11-04", 0.97),
        "due_date": _null(),
        "purchase_order": _null(),
        "currency": _f("GBP", "Credit total: -86.40 GBP", 0.97),
        "subtotal": _f(-72.00, "Net: -72.00", 0.96),
        "tax": _f(-14.40, "VAT 20%: -14.40", 0.96),
        "total": _f(-86.40, "Credit total: -86.40 GBP", 0.97),
        "line_items": [
            _line("Returned acrylic panel 600x400", -2, 42.00, -84.00, "goods",
                  "Returned: 2 x acrylic panel 600x400 @ 42.00 -84.00", 0.9),
            _line("Restocking fee", None, None, 12.00, "fee", "Restocking fee 12.00", 0.93),
        ],
    },
)

GERMAN_INVOICE = FewShotExample(
    name="european_format_with_other_category",
    lesson="1.780,00 means 1780.00; 12.10.2025 is 12 October; a deposit line is 'other' with a detail label.",
    document="""Brasserie Lindenhof GmbH
Lieferschein-Rechnung Nr. BL-2025-771
Datum: 12.10.2025
Kunden-Bestellnr.: K-8812

20 x Fassbier 30l        à 89,00     1.780,00
20 x Fasspfand           à 30,00       600,00
Lieferpauschale                         25,00

Summe netto              2.405,00
USt 19 %                   456,95
Rechnungsbetrag          2.861,95 EUR
""",
    extraction={
        "document_type": "invoice",
        "document_type_detail": None,
        "vendor_name": _f("Brasserie Lindenhof GmbH", "Brasserie Lindenhof GmbH", 0.97),
        "invoice_number": _f("BL-2025-771", "Lieferschein-Rechnung Nr. BL-2025-771", 0.95),
        "invoice_date": _f("2025-10-12", "Datum: 12.10.2025", 0.9),
        "due_date": _null(),
        "purchase_order": _f("K-8812", "Kunden-Bestellnr.: K-8812", 0.93),
        "currency": _f("EUR", "Rechnungsbetrag 2.861,95 EUR", 0.97),
        "subtotal": _f(2405.00, "Summe netto 2.405,00", 0.96),
        "tax": _f(456.95, "USt 19 % 456,95", 0.96),
        "total": _f(2861.95, "Rechnungsbetrag 2.861,95 EUR", 0.97),
        "line_items": [
            _line("Fassbier 30l", 20, 89.00, 1780.00, "goods", "20 x Fassbier 30l à 89,00 1.780,00"),
            _line("Fasspfand", 20, 30.00, 600.00, "other", "20 x Fasspfand à 30,00 600,00",
                  0.85, detail="returnable keg deposit"),
            _line("Lieferpauschale", None, None, 25.00, "shipping", "Lieferpauschale 25,00", 0.9),
        ],
    },
)

EMAIL_NOTE = FewShotExample(
    name="informal_email_mostly_null",
    lesson="No number, no date, no table: most of the header is null, and 'dollars' is low-confidence USD.",
    document="""Hi there,
Quick note to bill for last week's tuning visit: piano tuning for the
studio Steinway, 150 dollars, plus 20 dollars for the replacement felt.
Total 170 dollars. Cheers, Marlow Piano Care
""",
    extraction={
        "document_type": "invoice",
        "document_type_detail": None,
        "vendor_name": _f("Marlow Piano Care", "Cheers, Marlow Piano Care", 0.9),
        "invoice_number": _null(0.9),
        "invoice_date": _null(0.9),
        "due_date": _null(0.9),
        "purchase_order": _null(0.95),
        "currency": _f("USD", "Total 170 dollars.", 0.6),
        "subtotal": _null(0.9),
        "tax": _null(0.9),
        "total": _f(170.00, "Total 170 dollars.", 0.92),
        "line_items": [
            _line("Piano tuning", None, None, 150.00, "services",
                  "piano tuning for the studio Steinway, 150 dollars", 0.9),
            _line("Replacement felt", None, None, 20.00, "goods",
                  "plus 20 dollars for the replacement felt", 0.88),
        ],
    },
)

EXAMPLES: list[FewShotExample] = [CREDIT_MEMO, GERMAN_INVOICE, EMAIL_NOTE]
