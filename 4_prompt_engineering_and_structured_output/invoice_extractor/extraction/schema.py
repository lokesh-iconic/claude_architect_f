"""The extraction tool: the JSON schema is the output contract.

Structured output is enforced by the API, not requested in prose: the tool is
`strict: true` (the API guarantees `tool_use.input` validates against this
schema) and `tool_choice` forces the call. Every property is `required`, and a
field a document may not contain is typed `["<type>", "null"]` -- so "absent"
is an explicit, schema-legal answer rather than an omitted key the model is
tempted to fill with something plausible.
"""

from __future__ import annotations

from typing import Any

TOOL_NAME = "record_extraction"

DOCUMENT_TYPES = ["invoice", "credit_note", "proforma", "receipt", "other"]
LINE_CATEGORIES = ["goods", "services", "shipping", "fee", "discount", "tax", "other"]

HEADER_FIELDS: dict[str, str] = {
    "vendor_name": "string",
    "invoice_number": "string",
    "invoice_date": "string",
    "due_date": "string",
    "purchase_order": "string",
    "currency": "string",
    "subtotal": "number",
    "tax": "number",
    "total": "number",
}

HEADER_DESCRIPTIONS: dict[str, str] = {
    "vendor_name": "The issuing business, as printed (OCR slips may be corrected).",
    "invoice_number": "The document's own number/reference, copied exactly.",
    "invoice_date": "Issue date as YYYY-MM-DD.",
    "due_date": "Payment due date as YYYY-MM-DD. Null unless the document states one.",
    "purchase_order": "The buyer's PO/order reference, copied exactly.",
    "currency": "ISO 4217 code, e.g. USD, EUR, GBP.",
    "subtotal": "Pre-tax total as printed. Null if the document prints none.",
    "tax": "Total tax as printed. Null if the document prints none.",
    "total": "Amount payable (negative on a credit note).",
}

# Fields a downstream AP system cannot post without. If one is null the
# document goes to a human -- retrying cannot conjure data that isn't there.
REQUIRED_FOR_POSTING = ("vendor_name", "invoice_number", "currency", "total")


def _nullable(type_name: str) -> list[str]:
    return [type_name, "null"]


def _field_schema(name: str, type_name: str) -> dict[str, Any]:
    return {
        "type": "object",
        "description": HEADER_DESCRIPTIONS[name],
        "properties": {
            "value": {"type": _nullable(type_name)},
            "evidence": {
                "type": _nullable("string"),
                "description": "Verbatim quote from the document supporting the value. Null iff value is null.",
            },
            "confidence": {
                "type": "number",
                "description": "0.0-1.0: how sure you are the value is correct (or correctly null).",
            },
        },
        "required": ["value", "evidence", "confidence"],
        "additionalProperties": False,
    }


LINE_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "description": {"type": "string"},
        "quantity": {"type": _nullable("number")},
        "unit_price": {"type": _nullable("number")},
        "amount": {"type": "number", "description": "Line total as printed; negative for credits/discounts."},
        "category": {"type": "string", "enum": LINE_CATEGORIES},
        "category_detail": {
            "type": _nullable("string"),
            "description": "Required when category is 'other': a short free-text label. Null otherwise.",
        },
        "evidence": {"type": "string", "description": "The line as it appears in the document."},
        "confidence": {"type": "number"},
    },
    "required": [
        "description", "quantity", "unit_price", "amount",
        "category", "category_detail", "evidence", "confidence",
    ],
    "additionalProperties": False,
}


def input_schema() -> dict[str, Any]:
    properties: dict[str, Any] = {
        "document_type": {"type": "string", "enum": DOCUMENT_TYPES},
        "document_type_detail": {
            "type": _nullable("string"),
            "description": "Required when document_type is 'other': what the document calls itself. Null otherwise.",
        },
    }
    for name, type_name in HEADER_FIELDS.items():
        properties[name] = _field_schema(name, type_name)
    properties["line_items"] = {"type": "array", "items": LINE_ITEM_SCHEMA}
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def extraction_tool() -> dict[str, Any]:
    return {
        "name": TOOL_NAME,
        "description": (
            "Record the structured data extracted from one commercial document "
            "(invoice, credit note, proforma, receipt, or similar). Call exactly once "
            "per document. Record only what the document states: when a field is not "
            "present, set value and evidence to null rather than inferring one."
        ),
        "strict": True,
        "input_schema": input_schema(),
    }


# These models reject forced tool_choice ({"type": "tool"} / "any") with a 400.
# On them we fall back to "auto" + strict, and the pipeline treats a turn with
# no tool call as a retryable issue.
FORCED_TOOL_CHOICE_REJECTED = {"claude-opus-5-5", "claude-fable-5-1", "claude-mythos-5-1"}


def tool_choice_for(model: str) -> dict[str, Any]:
    if model in FORCED_TOOL_CHOICE_REJECTED:
        return {"type": "auto"}
    return {"type": "tool", "name": TOOL_NAME}
