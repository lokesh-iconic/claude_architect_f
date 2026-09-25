"""Model backends.

The agentic loop in `loop.py` is written against `Backend`, not against the
Anthropic SDK. `LiveBackend` normalizes a real `Message` into the small block
types below; `MockBackend` fabricates the same shapes. That is what lets the
whole orchestration -- decomposition, parallel Task fan-out, timeouts,
attribution -- be exercised offline and asserted in tests.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..config.settings import Settings


@dataclass
class TextBlock:
    text: str


@dataclass
class ToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class ServerToolBlock:
    """A server-side tool result (e.g. web_search) the API already executed."""

    kind: str
    payload: Any


Block = TextBlock | ToolUseBlock | ServerToolBlock


@dataclass
class ModelResponse:
    stop_reason: str
    blocks: list[Block]
    # What must be appended back into `messages` as the assistant turn. For the
    # live backend these are SDK content blocks; for mock, plain dicts.
    raw_content: Any
    input_tokens: int = 0
    output_tokens: int = 0
    stop_details: Any = None

    @property
    def text(self) -> str:
        return "\n".join(b.text for b in self.blocks if isinstance(b, TextBlock)).strip()

    @property
    def tool_uses(self) -> list[ToolUseBlock]:
        return [b for b in self.blocks if isinstance(b, ToolUseBlock)]


class BackendError(RuntimeError):
    """A model call failed in a way the caller should surface, not swallow."""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class Backend(Protocol):
    name: str

    async def create(
        self,
        *,
        model: str,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        effort: str,
        max_tokens: int,
    ) -> ModelResponse: ...


# --------------------------------------------------------------------------
# Live
# --------------------------------------------------------------------------


class LiveBackend:
    """Talks to the real Messages API."""

    name = "live"

    def __init__(self, settings: Settings) -> None:
        import anthropic

        self._anthropic = anthropic
        self._client = anthropic.AsyncAnthropic(api_key=settings.api_key)
        self.calls = 0

    async def create(
        self,
        *,
        model: str,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        effort: str,
        max_tokens: int,
    ) -> ModelResponse:
        anthropic = self._anthropic
        self.calls += 1
        try:
            response = await self._client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=messages,
                tools=tools or anthropic.NOT_GIVEN,
                thinking={"type": "adaptive"},
                output_config={"effort": effort},
            )
        except anthropic.AuthenticationError as exc:
            raise BackendError(f"authentication failed: {exc}", retryable=False) from exc
        except anthropic.BadRequestError as exc:
            raise BackendError(f"bad request: {exc}", retryable=False) from exc
        except anthropic.RateLimitError as exc:
            raise BackendError(f"rate limited: {exc}", retryable=True) from exc
        except anthropic.APITimeoutError as exc:
            raise BackendError(f"request timed out: {exc}", retryable=True) from exc
        except anthropic.APIConnectionError as exc:
            raise BackendError(f"connection error: {exc}", retryable=True) from exc
        except anthropic.APIStatusError as exc:
            raise BackendError(
                f"api error {exc.status_code}: {exc.message}", retryable=exc.status_code >= 500
            ) from exc

        return ModelResponse(
            stop_reason=response.stop_reason or "end_turn",
            blocks=_normalize(response.content),
            raw_content=response.content,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            stop_details=getattr(response, "stop_details", None),
        )


def _normalize(content: Any) -> list[Block]:
    blocks: list[Block] = []
    for block in content:
        btype = getattr(block, "type", None)
        if btype == "text":
            blocks.append(TextBlock(text=block.text))
        elif btype == "tool_use":
            blocks.append(ToolUseBlock(id=block.id, name=block.name, input=dict(block.input)))
        elif btype in {"web_search_tool_result", "web_fetch_tool_result", "server_tool_use"}:
            blocks.append(ServerToolBlock(kind=btype, payload=block))
        # thinking blocks carry no text under the default display setting and
        # are replayed via raw_content, so they need no normalized form.
    return blocks


# --------------------------------------------------------------------------
# Mock
# --------------------------------------------------------------------------


@dataclass
class MockBackend:
    """Deterministic stand-in used when no valid API key is configured.

    It is a scripted planner, not a language model: it reads the same tool
    schemas the live backend sees and emits well-formed `tool_use` blocks so
    the orchestration code path is identical in both modes.
    """

    settings: Settings
    name: str = "mock"
    calls: int = 0
    _counter: int = field(default=0, init=False)

    async def create(
        self,
        *,
        model: str,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        effort: str,
        max_tokens: int,
    ) -> ModelResponse:
        self.calls += 1
        role = _role_of(system)
        # Subagent turns are the expensive ones in a real run, so give them the
        # bulk of the simulated latency -- that is what makes the parallel vs.
        # --serial wall-clock comparison meaningful offline.
        await asyncio.sleep(0.02 if role == "coordinator" else 0.20)

        if role == "coordinator":
            return self._coordinator_turn(system, messages, tools)
        return self._subagent_turn(system, messages, tools)

    # -- coordinator -------------------------------------------------------

    def _coordinator_turn(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelResponse:
        from .mock_plans import decompose, gap_brief, synthesize

        topic = _topic_from(messages)
        rounds = _count_assistant_turns(messages)

        if rounds == 0:
            briefs = decompose(topic)
            content = [
                {
                    "type": "text",
                    "text": f"Decomposing {topic!r} into {len(briefs)} parallel subtasks.",
                }
            ]
            for brief in briefs:
                content.append(
                    {
                        "type": "tool_use",
                        "id": self._next_id(),
                        "name": "Task",
                        "input": brief,
                    }
                )
            return self._respond("tool_use", content)

        if rounds == 1:
            brief = gap_brief(topic)
            content = [
                {"type": "text", "text": "Reviewing coverage; one gap needs a follow-up pass."},
                {"type": "tool_use", "id": self._next_id(), "name": "Task", "input": brief},
            ]
            return self._respond("tool_use", content)

        return self._respond("end_turn", [{"type": "text", "text": synthesize(topic, messages)}])

    # -- subagents ---------------------------------------------------------

    def _subagent_turn(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelResponse:
        from .mock_plans import mock_findings

        tool_names = {t.get("name") for t in tools}
        turns = _count_assistant_turns(messages)
        brief = _first_user_text(messages)

        if turns == 0:
            if "search_documents" in tool_names:
                call = {"type": "tool_use", "id": self._next_id(), "name": "list_documents", "input": {}}
            else:
                call = {
                    "type": "tool_use",
                    "id": self._next_id(),
                    "name": "web_search",
                    "input": {"query": _query_from(brief)},
                }
            return self._respond("tool_use", [{"type": "text", "text": "Gathering sources."}, call])

        if turns == 1 and "read_document" in tool_names:
            doc = _first_listed_document(messages)
            if doc:
                return self._respond(
                    "tool_use",
                    [
                        {
                            "type": "tool_use",
                            "id": self._next_id(),
                            "name": "read_document",
                            "input": {"path": doc},
                        }
                    ],
                )

        findings = mock_findings(brief, messages, has_docs="read_document" in tool_names)
        return self._respond(
            "tool_use",
            [
                {
                    "type": "tool_use",
                    "id": self._next_id(),
                    "name": "submit_findings",
                    "input": findings,
                }
            ],
        )

    # -- helpers -----------------------------------------------------------

    def _next_id(self) -> str:
        self._counter += 1
        return f"mock_tool_{self._counter:03d}"

    def _respond(self, stop_reason: str, content: list[dict[str, Any]]) -> ModelResponse:
        return ModelResponse(
            stop_reason=stop_reason,
            blocks=_normalize_dicts(content),
            raw_content=content,
            input_tokens=0,
            output_tokens=0,
        )


def _normalize_dicts(content: list[dict[str, Any]]) -> list[Block]:
    blocks: list[Block] = []
    for block in content:
        if block["type"] == "text":
            blocks.append(TextBlock(text=block["text"]))
        elif block["type"] == "tool_use":
            blocks.append(ToolUseBlock(id=block["id"], name=block["name"], input=block["input"]))
    return blocks


def _role_of(system: str) -> str:
    return "coordinator" if "ROLE: coordinator" in system else "subagent"


def _first_user_text(messages: list[dict[str, Any]]) -> str:
    for message in messages:
        if message["role"] == "user" and isinstance(message["content"], str):
            return message["content"]
    return ""


def _count_assistant_turns(messages: list[dict[str, Any]]) -> int:
    return sum(1 for m in messages if m["role"] == "assistant")


def _topic_from(messages: list[dict[str, Any]]) -> str:
    """Pull the bare topic out of the coordinator's opening prompt."""
    text = _first_user_text(messages)
    for line in text.splitlines():
        if line.lower().startswith("research topic:"):
            return line.split(":", 1)[1].strip()
    return text.strip()


def _first_listed_document(messages: list[dict[str, Any]]) -> str | None:
    """First corpus path mentioned in any tool result, e.g. `notes.md (901 bytes)`."""
    for message in reversed(messages):
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not (isinstance(block, dict) and block.get("type") == "tool_result"):
                continue
            for line in str(block.get("content", "")).splitlines():
                head = line.strip().split()[0] if line.strip() else ""
                if head.endswith((".md", ".txt")):
                    return head.rstrip(":")
    return None


def _query_from(brief: str) -> str:
    """Build a search query from the brief's angle line, not its preamble."""
    for line in brief.splitlines():
        if line.startswith("Your angle:"):
            angle = line.split(":", 1)[1]
            angle = angle.split(" as they relate")[0].replace("Cover", " ")
            return " ".join(angle.replace(".", " ").split()[:14])
    return " ".join(brief.split()[:12])


def build_backend(settings: Settings) -> Backend:
    return LiveBackend(settings) if settings.is_live else MockBackend(settings)
