"""The validation-retry loop and confidence routing.

One document's lifecycle is a `DocState` advanced by `step()`, one model turn
at a time. The synchronous path and the batch path share `step()`, so a
document gets identical retry and routing decisions whichever way it ran.

Retry policy, in order:
1. No retryable issue -> stop. (Non-retryable issues still route to review.)
2. The retryable issues are *identical* to last attempt's, after the model
   was shown them -> stop: the model is standing by its answer, which for an
   arithmetic mismatch means the source document itself doesn't add up.
3. Out of attempts -> stop.
4. Otherwise send the failed extraction back with only the retryable issues.
   Non-retryable "absent" issues are deliberately withheld from the feedback:
   telling the model "invoice_number is missing" pressures it to supply one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from ..backends.backend import Backend, ModelTurn
from .errors import ExtractionError, ValidationIssue
from .models import AttemptRecord, Document, DocumentResult, Extraction
from .prompts import document_message, feedback_messages, system_prompt
from .schema import TOOL_NAME, extraction_tool, tool_choice_for
from ..config.settings import Settings
from .validation import validate

PERSISTED = "persisted_after_feedback"
EXHAUSTED = "attempts_exhausted"


def build_params(settings: Settings, messages: list[dict[str, Any]]) -> dict[str, Any]:
    """The Messages API request body -- sent directly, or wrapped in a batch request."""
    return {
        "model": settings.model,
        "max_tokens": settings.max_tokens,
        "system": [{"type": "text", "text": system_prompt(), "cache_control": {"type": "ephemeral"}}],
        "tools": [extraction_tool()],
        "tool_choice": tool_choice_for(settings.model),
        "output_config": {"effort": settings.effort},
        "messages": messages,
    }


@dataclass
class DocState:
    doc: Document
    messages: list[dict[str, Any]]
    attempts: list[AttemptRecord] = field(default_factory=list)
    extraction: Extraction | None = None
    issues: list[ValidationIssue] = field(default_factory=list)
    prev_keys: frozenset | None = None
    stop_note: str | None = None
    done: bool = False

    @classmethod
    def start(cls, doc: Document) -> "DocState":
        return cls(doc, [document_message(doc)])


def assess(turn: ModelTurn, doc: Document) -> tuple[Extraction | None, list[ValidationIssue]]:
    if turn.tool_input is None:
        retryable = turn.stop_reason != "refusal"
        return None, [ValidationIssue(
            "no_tool_call", "schema", "*",
            f"the response (stop_reason={turn.stop_reason}) did not call {TOOL_NAME}", retryable,
            f"Call {TOOL_NAME} with the extraction.",
        )]
    try:
        extraction = Extraction.from_tool_input(turn.tool_input)
    except (KeyError, TypeError, ValueError) as exc:
        return None, [ValidationIssue(
            "schema_violation", "schema", "*", f"tool input does not match the schema: {exc!r}", True,
            "Return every field defined by the tool schema.",
        )]
    return extraction, validate(extraction, doc.text)


def step(state: DocState, turn: ModelTurn, settings: Settings) -> bool:
    """Apply one model turn. Returns True if the document needs another call."""
    extraction, issues = assess(turn, state.doc)
    record = AttemptRecord(len(state.attempts) + 1, turn.stop_reason, issues, turn.tool_input)
    state.attempts.append(record)
    if extraction is not None:
        state.extraction = extraction
    state.issues = issues

    retryable = [i for i in issues if i.retryable]
    if not retryable:
        state.done = True
        return False
    keys = frozenset(i.key() for i in retryable)
    if state.prev_keys is not None and keys == state.prev_keys:
        state.stop_note, state.done = PERSISTED, True
        return False
    if record.attempt >= settings.max_attempts:
        state.stop_note, state.done = EXHAUSTED, True
        return False

    state.prev_keys = keys
    state.messages = state.messages + feedback_messages(turn.content, turn.tool_use_id, retryable)
    record.feedback_sent = True
    return True


def fail(state: DocState, error: ExtractionError) -> None:
    state.attempts.append(AttemptRecord(len(state.attempts) + 1, "error", [], None))
    state.stop_note = f"api_error: {error.category}: {error.description}"
    state.done = True


def low_confidence_fields(ex: Extraction, threshold: float) -> list[tuple[str, float]]:
    low = [(name, fv.confidence) for name, fv in ex.fields.items() if fv.confidence < threshold]
    low += [(f"line_items[{i}]", li.confidence) for i, li in enumerate(ex.line_items) if li.confidence < threshold]
    return low


def finalize(state: DocState, settings: Settings) -> DocumentResult:
    reasons: list[str] = []
    if state.stop_note not in (None, PERSISTED, EXHAUSTED):
        reasons.append(state.stop_note)
    if state.extraction is None:
        reasons.append("no valid extraction")
    for issue in state.issues:
        if issue.kind == "absent":
            reasons.append(f"absent_from_source: {issue.field}")
        elif issue.retryable:
            reasons.append(f"{state.stop_note or EXHAUSTED}: {issue.code} ({issue.field})")
        else:
            reasons.append(f"{issue.code} ({issue.field})")
    low = low_confidence_fields(state.extraction, settings.confidence_threshold) if state.extraction else []
    if low:
        reasons.append("low_confidence: " + ", ".join(f"{n}={c:.2f}" for n, c in low))
    return DocumentResult(
        doc_id=state.doc.doc_id,
        status="review" if reasons else "accepted",
        extraction=state.extraction,
        attempts=state.attempts,
        review_reasons=reasons,
        low_confidence=low,
        unresolved=list(state.issues),
    )


def extract_document(doc: Document, backend: Backend, settings: Settings) -> DocumentResult:
    state = DocState.start(doc)
    while not state.done:
        try:
            turn = backend.create(build_params(settings, state.messages))
        except ExtractionError as exc:
            fail(state, exc)
            break
        step(state, turn, settings)
    return finalize(state, settings)


def extract_all(
    docs: list[Document],
    backend: Backend,
    settings: Settings,
    progress: Callable[[DocumentResult], None] | None = None,
) -> list[DocumentResult]:
    results = []
    for doc in docs:
        result = extract_document(doc, backend, settings)
        if progress:
            progress(result)
        results.append(result)
    return results
