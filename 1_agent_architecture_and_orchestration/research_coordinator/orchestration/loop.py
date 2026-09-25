"""The agentic loop.

One function drives every agent in the system, coordinator and subagents
alike: call the model, branch on `stop_reason`, execute the tools it asked
for, feed the results back, repeat until it stops asking. Everything
role-specific arrives as arguments.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .agents import AgentDefinition
from ..backends.backend import Backend, BackendError, ModelResponse, ToolUseBlock
from ..config.settings import Settings
from .tools import EXECUTORS, ToolContext, ToolError

# Tools the API runs on its own infrastructure - results arrive in the same
# response and must never be executed locally.
SERVER_SIDE_TOOLS = {"web_search"}

SpecialHandler = Callable[[ToolUseBlock], Awaitable[str]]
EventSink = Callable[[str, dict[str, Any]], None]


class AgentFailure(RuntimeError):
    """The agent could not complete its turn. Carries structured context."""

    def __init__(
        self,
        message: str,
        *,
        failure_type: str,
        attempted: str,
        partial: Any = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.failure_type = failure_type
        self.attempted = attempted
        self.partial = partial
        self.retryable = retryable


@dataclass
class LoopResult:
    final_text: str
    terminal_payload: dict[str, Any] | None
    turns: int
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    refused_tool_calls: list[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    elapsed_s: float = 0.0


def supports_mid_conversation_system(model: str) -> bool:
    """Opus 5 / 4.8 and the Fable family accept `role: "system"` in messages."""
    return model.startswith(("claude-opus-5", "claude-opus-4-8", "claude-fable", "claude-mythos"))


async def run_agent(
    *,
    agent: AgentDefinition,
    backend: Backend,
    settings: Settings,
    prompt: str,
    ctx: ToolContext,
    terminal_tool: str | None = None,
    special_handlers: dict[str, SpecialHandler] | None = None,
    max_turns: int = 12,
    on_event: EventSink | None = None,
    round_hook: Callable[[int], str | None] | None = None,
    parallel_tools: bool = True,
) -> LoopResult:
    """Run one agent to completion.

    `terminal_tool` names a tool the agent must call before it is allowed to
    finish -- the programmatic half of "return structured findings", which a
    prompt instruction alone does not reliably deliver.

    `special_handlers` maps a tool name to an async handler the loop awaits
    instead of looking the tool up in `EXECUTORS`. That is how `Task` reaches
    the orchestrator without the loop knowing anything about subagents.
    """
    started = time.perf_counter()
    handlers = special_handlers or {}
    allowed = set(agent.allowed_tools)
    tool_specs = agent.tool_specs(settings)

    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
    result = LoopResult(final_text="", terminal_payload=None, turns=0)
    nudges = 0
    tool_rounds = 0
    last_text = ""

    def emit(kind: str, **payload: Any) -> None:
        if on_event:
            on_event(kind, {"agent": agent.name, **payload})

    while result.turns < max_turns:
        result.turns += 1
        try:
            response: ModelResponse = await backend.create(
                model=agent.model,
                system=agent.system,
                messages=messages,
                tools=tool_specs,
                effort=agent.effort,
                max_tokens=settings.max_tokens,
            )
        except BackendError as exc:
            raise AgentFailure(
                str(exc),
                failure_type="model_call_failed",
                attempted=f"{agent.name} turn {result.turns}",
                partial=last_text or None,
                retryable=exc.retryable,
            ) from exc

        result.input_tokens += response.input_tokens
        result.output_tokens += response.output_tokens
        if response.text:
            last_text = response.text

        if response.stop_reason == "refusal":
            detail = getattr(response.stop_details, "explanation", "") or "no explanation given"
            raise AgentFailure(
                f"model refused: {detail}",
                failure_type="refusal",
                attempted=f"{agent.name} turn {result.turns}",
                partial=last_text or None,
            )

        if response.stop_reason == "max_tokens":
            raise AgentFailure(
                "response hit max_tokens before completing",
                failure_type="output_truncated",
                attempted=f"{agent.name} turn {result.turns}",
                partial=last_text or None,
                retryable=True,
            )

        if response.stop_reason == "pause_turn":
            # A server-side tool paused mid-turn; replay it to resume.
            messages.append({"role": "assistant", "content": response.raw_content})
            continue

        if response.stop_reason == "end_turn":
            if terminal_tool and result.terminal_payload is None:
                if nudges >= 1:
                    raise AgentFailure(
                        f"finished without ever calling {terminal_tool}",
                        failure_type="contract_violation",
                        attempted=f"{agent.name} was asked twice to call {terminal_tool}",
                        partial=last_text or None,
                    )
                nudges += 1
                emit("nudge", tool=terminal_tool)
                messages.append({"role": "assistant", "content": response.raw_content})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"You ended your turn without calling {terminal_tool}. "
                            f"Call {terminal_tool} now with everything you established. "
                            "Do not reply with prose."
                        ),
                    }
                )
                continue
            result.final_text = response.text or last_text
            break

        if response.stop_reason != "tool_use":
            raise AgentFailure(
                f"unexpected stop_reason {response.stop_reason!r}",
                failure_type="unexpected_stop_reason",
                attempted=f"{agent.name} turn {result.turns}",
                partial=last_text or None,
            )

        # --- stop_reason == "tool_use": execute and continue ---------------
        # Server-side tools (live web_search) arrive already executed as
        # ServerToolBlocks and never appear in `tool_uses`, so this list is
        # exactly the client-side work.
        pending = response.tool_uses
        messages.append({"role": "assistant", "content": response.raw_content})

        if not pending:
            raise AgentFailure(
                "stop_reason was tool_use but no client tool call was present",
                failure_type="empty_tool_use",
                attempted=f"{agent.name} turn {result.turns}",
                partial=last_text or None,
            )

        emit("tool_batch", count=len(pending), tools=[c.name for c in pending])
        for call in pending:
            emit("tool_call", tool=call.name, input=call.input)

        async def run(call: ToolUseBlock) -> tuple[str, bool]:
            return await _execute(
                call=call,
                allowed=allowed,
                handlers=handlers,
                ctx=ctx,
                terminal_tool=terminal_tool,
                result=result,
            )

        # Several tool_use blocks in one response are independent by
        # construction, so they run concurrently -- this is what makes a batch
        # of Task calls a parallel fan-out. --serial forces them one at a time
        # so the two can be timed against each other.
        if parallel_tools and len(pending) > 1:
            outcomes = await asyncio.gather(*(run(c) for c in pending))
        else:
            outcomes = [await run(c) for c in pending]

        tool_results: list[dict[str, Any]] = []
        for call, (payload, is_error) in zip(pending, outcomes):
            result.tool_calls.append(
                {"tool": call.name, "input": call.input, "is_error": is_error}
            )
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": call.id,
                    "content": payload,
                    **({"is_error": True} if is_error else {}),
                }
            )

        # All results for one assistant turn go back in a SINGLE user message.
        # Splitting them teaches the model to stop making parallel calls.
        messages.append({"role": "user", "content": tool_results})
        tool_rounds += 1

        if result.terminal_payload is not None:
            # The contract is satisfied; no reason to spend another turn.
            result.final_text = last_text
            break

        if round_hook and supports_mid_conversation_system(agent.model) and settings.is_live:
            note = round_hook(tool_rounds)
            if note:
                messages.append({"role": "system", "content": note})

    else:
        raise AgentFailure(
            f"exceeded {max_turns} turns without finishing",
            failure_type="turn_limit_exceeded",
            attempted=f"{agent.name} agentic loop",
            partial=last_text or None,
        )

    result.elapsed_s = time.perf_counter() - started
    return result


async def _execute(
    *,
    call: ToolUseBlock,
    allowed: set[str],
    handlers: dict[str, SpecialHandler],
    ctx: ToolContext,
    terminal_tool: str | None,
    result: LoopResult,
) -> tuple[str, bool]:
    """Run one tool call. Returns (tool_result content, is_error)."""
    if call.name not in allowed:
        result.refused_tool_calls.append(call.name)
        return (
            ToolError(
                f"{call.name!r} is not in this agent's tool allowlist",
                category="permission",
                retryable=False,
                attempted=f"{call.name}({call.input})",
            ).to_payload(),
            True,
        )

    if terminal_tool and call.name == terminal_tool:
        result.terminal_payload = dict(call.input)
        return "Findings recorded.", False

    handler = handlers.get(call.name)
    if handler is not None:
        try:
            return await handler(call), False
        except ToolError as exc:
            return exc.to_payload(), True

    executor = EXECUTORS.get(call.name)
    if executor is None:
        return (
            ToolError(
                f"no executor registered for {call.name!r}",
                category="internal",
                retryable=False,
                attempted=f"{call.name}({call.input})",
            ).to_payload(),
            True,
        )

    try:
        return executor(call.input, ctx), False
    except ToolError as exc:
        return exc.to_payload(), True
    except Exception as exc:  # unexpected executor bug - still structured
        return (
            ToolError(
                f"{type(exc).__name__}: {exc}",
                category="internal",
                retryable=False,
                attempted=f"{call.name}({call.input})",
            ).to_payload(),
            True,
        )
