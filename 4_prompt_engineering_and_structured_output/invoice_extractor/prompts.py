"""Prompt text: the system prompt, few-shot rendering, and the retry message.

The system prompt is identical for every document (instructions, then the
few-shot block), so it caches across a run and across a batch. Anything that
varies per document goes in the user turn.
"""

from __future__ import annotations

import json
from typing import Any

from .errors import ValidationIssue
from .few_shot import EXAMPLES, FewShotExample
from .models import Document
from .schema import TOOL_NAME

INSTRUCTIONS = f"""You extract structured data from commercial documents (invoices, credit notes,
proformas, receipts, statements) for an accounts-payable system that posts what you
return without a human reading the original. A wrong value that looks plausible is
worse than an honest null, because nobody will catch it.

Record every document with the {TOOL_NAME} tool.

Rules:
- Record only what the document states. If a field is not printed, set its value and
  evidence to null. Do not derive a due date from payment terms, borrow a PO number
  from a referenced invoice, or assume a currency the document does not indicate.
- Evidence is a verbatim quote from the document (whitespace may differ). It is checked
  against the source text; a quote that isn't there is treated as a fabricated value.
- Dates are YYYY-MM-DD. When a date could be read two ways (03/04/2026), use the
  document's locale cues to pick one and lower that field's confidence.
- Amounts are plain numbers as printed, converted from any locale format
  (1.234,56 -> 1234.56). Credits, returns, and discounts are negative; keep
  quantity x unit_price = amount, putting the sign on quantity for returned goods.
- Use category "other" only when no listed category fits, and then always give a short
  category_detail. The same applies to document_type "other" and document_type_detail.
- Confidence is your probability that the field is right (including "right to be
  null"). Below 0.8 means a human should look; use it when OCR noise, ambiguity, or an
  inference is involved, not as a formality.
- Do not "fix" the document's arithmetic. If its lines don't add up to its printed
  total, record both as printed."""


def render_example(example: FewShotExample) -> str:
    return (
        f'<example name="{example.name}">\n'
        f"<lesson>{example.lesson}</lesson>\n"
        f"<document>\n{example.document}</document>\n"
        f"<{TOOL_NAME}>\n{json.dumps(example.extraction, indent=2, ensure_ascii=False)}\n</{TOOL_NAME}>\n"
        f"</example>"
    )


def system_prompt(examples: list[FewShotExample] | None = None) -> str:
    examples = EXAMPLES if examples is None else examples
    if not examples:
        return INSTRUCTIONS
    rendered = "\n\n".join(render_example(e) for e in examples)
    return (
        f"{INSTRUCTIONS}\n\n"
        "The examples below show the input each call receives and the tool input you "
        "should produce. They were chosen for the cases these rules handle least well.\n\n"
        f"<examples>\n{rendered}\n</examples>"
    )


def document_message(doc: Document) -> dict[str, Any]:
    return {
        "role": "user",
        "content": (
            f'<document id="{doc.doc_id}">\n{doc.text}\n</document>\n\n'
            f"Extract this document with the {TOOL_NAME} tool."
        ),
    }


def feedback_messages(
    assistant_content: list[dict[str, Any]],
    tool_use_id: str | None,
    issues: list[ValidationIssue],
) -> list[dict[str, Any]]:
    """The retry turn: the failed extraction (as the model's own turn) plus the specific errors.

    The document itself is already the first user message, so the model sees
    document -> its failed extraction -> what exactly was wrong with it.
    """
    body = (
        "The extraction failed validation. Fix exactly these problems and call "
        f"{TOOL_NAME} again with the complete corrected extraction:\n"
        + json.dumps([i.to_payload() for i in issues], indent=2)
    )
    if tool_use_id is None:
        # No tool call to answer, so this is a plain user turn.
        return [
            {"role": "assistant", "content": assistant_content or [{"type": "text", "text": "(no output)"}]},
            {"role": "user", "content": body},
        ]
    return [
        {"role": "assistant", "content": assistant_content},
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": tool_use_id, "is_error": True, "content": body}
            ],
        },
    ]
