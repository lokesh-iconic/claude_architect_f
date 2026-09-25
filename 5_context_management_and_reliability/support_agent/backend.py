"""Model backends: the live Messages API and an offline mock.

Both take the exact request dict the agent builds (`system` blocks, `tools`,
`messages`). The mock reads *only* that dict -- it keeps no memory of its own
between calls -- so whatever it "knows" at turn 16 is exactly what the
request carried at turn 16. That is what makes the long-conversation check
a test of context management rather than of the mock.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Protocol

from .errors import SupportToolError
from .memory import Exchange, MockSummarizer, Summarizer
from .prompts import SUMMARIZER_PROMPT
from .settings import Settings

RESENDABLE_BLOCKS = {"text", "thinking", "redacted_thinking", "tool_use"}


@dataclass
class ToolUse:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class ModelTurn:
    stop_reason: str
    content: list[dict[str, Any]]
    usage: dict[str, int] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return "\n".join(b["text"] for b in self.content if b.get("type") == "text").strip()

    @property
    def tool_uses(self) -> list[ToolUse]:
        return [ToolUse(b["id"], b["name"], dict(b.get("input") or {}))
                for b in self.content if b.get("type") == "tool_use"]


class Backend(Protocol):
    name: str

    def create(self, params: dict[str, Any]) -> ModelTurn: ...


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
        kwargs = {
            **params,
            "model": self.settings.model,
            "max_tokens": self.settings.max_tokens,
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": self.settings.effort},
        }
        if self.settings.refusal_fallback:
            kwargs["extra_headers"] = {"anthropic-beta": "server-side-fallback-2026-07-01"}
            kwargs["extra_body"] = {"fallbacks": "default"}
        try:
            msg = self.client.messages.create(**kwargs)
        except (a.RateLimitError, a.APITimeoutError, a.APIConnectionError, a.InternalServerError) as exc:
            raise SupportToolError(
                f"model call failed: {type(exc).__name__}", category="transient",
                failure_type="model_unavailable", attempted="messages.create",
                remediation="Retry later; the SDK already retried twice.",
            ) from exc
        except a.APIStatusError as exc:
            raise SupportToolError(
                f"model call rejected ({exc.status_code})", category="internal",
                failure_type="model_request_rejected", attempted="messages.create",
                remediation="Fix the request or credentials; resending it unchanged will fail again.",
            ) from exc
        content = [b.to_dict() for b in msg.content if b.type in RESENDABLE_BLOCKS]
        usage = {
            "input_tokens": getattr(msg.usage, "input_tokens", 0) or 0,
            "output_tokens": getattr(msg.usage, "output_tokens", 0) or 0,
            "cache_read_input_tokens": getattr(msg.usage, "cache_read_input_tokens", 0) or 0,
        }
        return ModelTurn(msg.stop_reason or "end_turn", content, usage)


class LiveSummarizer:
    """Model-written summary. Low effort: it is condensing prose, not reasoning."""

    def __init__(self, backend: LiveBackend) -> None:
        self.backend = backend

    def summarize(self, previous: str, exchange: Exchange) -> str:
        excerpt = (f"Existing summary:\n{previous or '(none)'}\n\nNew exchange (turn {exchange.turn}):\n"
                   f"Customer: {exchange.customer_text}\nAgent tools: {', '.join(exchange.tools) or 'none'}\n"
                   f"Agent reply: {exchange.reply}")
        msg = self.backend.client.messages.create(
            model=self.backend.settings.model,
            max_tokens=2000,
            system=SUMMARIZER_PROMPT,
            messages=[{"role": "user", "content": excerpt}],
            thinking={"type": "adaptive"},
            output_config={"effort": "low"},
        )
        text = "\n".join(b.text for b in msg.content if b.type == "text").strip()
        return text or previous


class MockBackend:
    """Rule-based stand-in for the model. See `mock_agent.py`."""

    name = "mock"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def create(self, params: dict[str, Any]) -> ModelTurn:
        from .mock_agent import decide

        self.calls.append(copy.deepcopy(params))
        return decide(params)


def make_backend(settings: Settings) -> tuple[Backend, Summarizer]:
    if settings.is_live:
        live = LiveBackend(settings)
        return live, LiveSummarizer(live)
    return MockBackend(), MockSummarizer()

