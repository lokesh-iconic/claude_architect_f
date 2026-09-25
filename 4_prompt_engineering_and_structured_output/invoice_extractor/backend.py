"""Model backends: the live Messages API and an offline mock.

Both take the exact Messages API request dict (`params`) the pipeline builds,
so the same dict can be sent synchronously or dropped into a batch request.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Protocol

from .corpus import GroundTruth
from .errors import ExtractionError
from .schema import TOOL_NAME
from .settings import Settings

# Content block types that are valid to send back in an assistant turn.
RESENDABLE_BLOCKS = {"text", "thinking", "redacted_thinking", "tool_use"}


@dataclass
class ModelTurn:
    stop_reason: str
    tool_use_id: str | None
    tool_input: dict[str, Any] | None
    content: list[dict[str, Any]]
    usage: dict[str, int] = field(default_factory=dict)


class Backend(Protocol):
    name: str

    def create(self, params: dict[str, Any]) -> ModelTurn: ...


def turn_from_message(msg: Any) -> ModelTurn:
    """Convert an SDK `Message` (sync response or batch result) into a ModelTurn."""
    content = [b.to_dict() for b in msg.content if b.type in RESENDABLE_BLOCKS]
    tool = next((b for b in msg.content if b.type == "tool_use" and b.name == TOOL_NAME), None)
    usage = {
        "input_tokens": getattr(msg.usage, "input_tokens", 0) or 0,
        "output_tokens": getattr(msg.usage, "output_tokens", 0) or 0,
        "cache_read_input_tokens": getattr(msg.usage, "cache_read_input_tokens", 0) or 0,
    }
    return ModelTurn(
        stop_reason=msg.stop_reason or "unknown",
        tool_use_id=tool.id if tool else None,
        tool_input=dict(tool.input) if tool else None,
        content=content,
        usage=usage,
    )


class LiveBackend:
    name = "live"

    def __init__(self, settings: Settings) -> None:
        import anthropic

        self._anthropic = anthropic
        self.settings = settings
        self.client = anthropic.Anthropic(
            api_key=settings.api_key, timeout=settings.request_timeout_s, max_retries=2
        )

    def create(self, params: dict[str, Any]) -> ModelTurn:
        a = self._anthropic
        kwargs = dict(params)
        if self.settings.refusal_fallback:
            # Server-side refusal fallback; not accepted on the Batches API, so sync path only.
            kwargs["extra_headers"] = {"anthropic-beta": "server-side-fallback-2026-07-01"}
            kwargs["extra_body"] = {"fallbacks": "default"}
        try:
            return turn_from_message(self.client.messages.create(**kwargs))
        except (a.RateLimitError, a.APITimeoutError, a.APIConnectionError, a.InternalServerError) as exc:
            raise ExtractionError(
                f"transient API failure: {type(exc).__name__}", category="transient",
                remediation="Retry later; the SDK already retried twice.",
            ) from exc
        except (a.BadRequestError, a.AuthenticationError, a.PermissionDeniedError, a.NotFoundError) as exc:
            raise ExtractionError(
                f"request rejected: {exc}", category="configuration",
                remediation="Fix the request or credentials; resending it unchanged will fail again.",
            ) from exc
        except a.APIStatusError as exc:
            raise ExtractionError(
                f"API status {exc.status_code}", category="transient" if exc.status_code >= 500 else "configuration",
            ) from exc


# --------------------------------------------------------------------------
# Mock
# --------------------------------------------------------------------------

DOC_ID_RE = re.compile(r'<document id="([^"]+)">')

# The validation issue code that, once fed back, makes the mock correct each fault.
FAULT_FIX_SIGNAL = {
    "drop_line_item": "line_items_sum_mismatch",
    "fabricate_due_date": "evidence_not_in_source",
    "other_without_detail": "other_without_detail",
}


def apply_fault(expected: dict[str, Any], fault: str) -> dict[str, Any]:
    out = copy.deepcopy(expected)
    if fault == "drop_line_item":
        del out["line_items"][len(out["line_items"]) // 2]
    elif fault == "fabricate_due_date":
        issued = out["invoice_date"]["value"]
        guess = (date.fromisoformat(issued) + timedelta(days=30)).isoformat() if issued else "2026-04-30"
        out["due_date"] = {"value": guess, "evidence": f"Payment due: {guess}", "confidence": 0.7}
    elif fault == "other_without_detail":
        for li in out["line_items"]:
            if li["category"] == "other":
                li["category_detail"] = None
        if out["document_type"] == "other":
            out["document_type_detail"] = None
    else:
        raise ValueError(f"unknown mock fault {fault!r}")
    return out


def _last_feedback(messages: list[dict[str, Any]]) -> str:
    last = messages[-1] if messages else {}
    content = last.get("content")
    if last.get("role") != "user" or not isinstance(content, list):
        return ""
    return " ".join(
        str(block.get("content", "")) for block in content
        if block.get("type") == "tool_result" and block.get("is_error")
    )


class MockBackend:
    """Answers from hand-labelled ground truth, with a scripted first-attempt defect.

    A faulted document keeps coming back wrong until the request carries a
    `tool_result` error naming the matching issue code -- so a pipeline that
    retries *without* sending the specific error never gets a clean answer.
    This measures the pipeline's handling of model mistakes, not the model.
    """

    name = "mock"

    def __init__(self, truth: dict[str, GroundTruth]) -> None:
        self.truth = truth
        self.calls: list[dict[str, Any]] = []

    def respond(self, params: dict[str, Any]) -> ModelTurn:
        messages = params["messages"]
        first = messages[0]["content"]
        match = DOC_ID_RE.search(first if isinstance(first, str) else str(first))
        if not match or match.group(1) not in self.truth:
            raise ExtractionError("mock has no ground truth for this document", category="configuration")
        doc_id = match.group(1)
        gt = self.truth[doc_id]
        attempt = 1 + sum(1 for m in messages if m["role"] == "assistant")

        if not any(t.get("name") == TOOL_NAME for t in params.get("tools", [])):
            text = [{"type": "text", "text": "No extraction tool was offered."}]
            return ModelTurn("end_turn", None, None, text)

        fault = gt.mock_fault
        fixed = fault is None or FAULT_FIX_SIGNAL[fault] in _last_feedback(messages)
        tool_input = copy.deepcopy(gt.expected) if fixed else apply_fault(gt.expected, fault)
        tool_id = f"toolu_mock_{doc_id}_{attempt}"
        block = {"type": "tool_use", "id": tool_id, "name": TOOL_NAME, "input": tool_input}
        return ModelTurn("tool_use", tool_id, tool_input, [block], {"input_tokens": 0, "output_tokens": 0})

    def create(self, params: dict[str, Any]) -> ModelTurn:
        self.calls.append(copy.deepcopy(params))
        return self.respond(params)


def make_backend(settings: Settings, truth: dict[str, GroundTruth]) -> Backend:
    return LiveBackend(settings) if settings.is_live else MockBackend(truth)
