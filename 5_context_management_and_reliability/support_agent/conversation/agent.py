"""The support agent: one customer message in, one reply out.

Per customer turn:

* The request is rebuilt from scratch: the static system prompt (cached),
  then the case-facts block as a second system block, then the summary plus
  the verbatim window from `memory.py`, then this turn's messages.
* If the customer explicitly asked for a human, every tool except
  `escalate_to_human` is blocked for the turn, and a turn that ends without
  escalating is nudged once. The prompt asks for the same thing; this is the
  backstop for when the prompt is not followed.
* Every tool call goes through `ToolSession.call`, which owns the refund
  prerequisite, deadlines, trimming and fact extraction.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ..backends.backend import Backend
from ..tools.errors import SupportToolError, describe_call
from .escalation import explicit_human_request
from .memory import ConversationMemory, Exchange, Summarizer
from .prompts import ESCALATION_NUDGE, SYSTEM_PROMPT, tool_specs
from ..tools.session import ToolOutcome, ToolSession
from ..config.settings import Settings


@dataclass
class TurnRecord:
    turn: int
    customer_text: str
    reply: str
    outcomes: list[ToolOutcome] = field(default_factory=list)
    explicit_human_request: bool = False
    nudged: bool = False
    request_chars: int = 0
    facts_in_request: bool = False
    summary_in_request: bool = False
    stop: str = "end_turn"

    @property
    def tools(self) -> list[str]:
        return [o.tool for o in self.outcomes]


class SupportAgent:
    def __init__(
        self,
        backend: Backend,
        summarizer: Summarizer,
        settings: Settings,
        session: ToolSession | None = None,
        *,
        case_facts: bool = True,
    ) -> None:
        self.backend = backend
        self.settings = settings
        self.session = session or ToolSession(tool_timeout_s=settings.tool_timeout_s)
        self.memory = ConversationMemory(summarizer, keep_recent=settings.keep_recent_turns)
        self.case_facts = case_facts
        self.records: list[TurnRecord] = []
        self._tools = tool_specs()

    # ------------------------------------------------------------------
    def build_request(self, current: list[dict[str, Any]]) -> dict[str, Any]:
        system = [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}]
        if self.case_facts:
            system.append({"type": "text", "text": self.session.facts.render()})
        return {"system": system, "tools": self._tools,
                "messages": self.memory.request_messages(current)}

    def handle(self, customer_text: str) -> TurnRecord:
        self.session.turn += 1
        record = TurnRecord(self.session.turn, customer_text, reply="",
                            explicit_human_request=explicit_human_request(customer_text))
        current: list[dict[str, Any]] = [{"role": "user", "content": customer_text}]
        escalated = False

        for _ in range(self.settings.max_tool_rounds):
            params = self.build_request(current)
            record.request_chars = max(record.request_chars, len(json.dumps(params, default=str)))
            record.facts_in_request = self.case_facts
            record.summary_in_request = bool(self.memory.summary)
            turn = self.backend.create(params)
            current.append({"role": "assistant", "content": turn.content})

            if turn.stop_reason == "tool_use" and turn.tool_uses:
                results = []
                for use in turn.tool_uses:
                    outcome = self._execute(use.name, use.input, record, escalated)
                    escalated = escalated or (use.name == "escalate_to_human" and not outcome.is_error)
                    record.outcomes.append(outcome)
                    results.append({"type": "tool_result", "tool_use_id": use.id,
                                    "content": outcome.content,
                                    **({"is_error": True} if outcome.is_error else {})})
                # All results for one assistant turn go back in a single user message.
                current.append({"role": "user", "content": results})
                continue

            if record.explicit_human_request and not escalated and not record.nudged:
                record.nudged = True
                current.append({"role": "user", "content": [{"type": "text", "text": ESCALATION_NUDGE}]})
                continue

            record.reply, record.stop = turn.text, turn.stop_reason
            break
        else:
            record.stop = "max_tool_rounds"
            record.reply = ("I'm sorry, I couldn't finish that. I've kept everything we covered, so a "
                            "colleague can pick it up.")

        self.memory.commit(Exchange(record.turn, customer_text, current, record.reply, record.tools))
        self.records.append(record)
        return record

    def _execute(self, name: str, args: dict[str, Any], record: TurnRecord, escalated: bool) -> ToolOutcome:
        if record.explicit_human_request and not escalated and name != "escalate_to_human":
            return self.session.blocked(name, args, SupportToolError(
                "the customer explicitly asked for a human; no other action is allowed first",
                category="escalation", failure_type="escalation_required",
                attempted=describe_call(name, args),
                remediation="Call escalate_to_human with reason_category customer_request now.",
            ))
        return self.session.call(name, args)
