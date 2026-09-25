"""The agentic loop: one customer message in, one reply out.

Per customer turn the request is rebuilt from scratch (static system prompt
with `cache_control`, then the case-facts block, then summary + verbatim
window + this turn), and the loop branches on `stop_reason`:

* `tool_use`      -- run every requested tool through `ToolSession.call`, return
                     all results in one user message, continue.
* `pause_turn`    -- replay the assistant content unchanged and continue.
* `end_turn` / `stop_sequence` -- the reply. If the customer explicitly asked
                     for a human and nothing escalated, nudge once; if the
                     model still doesn't, the program files the handoff.
* `max_tokens`    -- the response may hold a truncated tool call, so none of
                     it is executed; retry once with a notice, then hand off.
* `refusal` / anything unrecognised -- stop and hand off.

"Hand off" always means a real ticket through the same validator
(`authored_by: system`): the customer is never told a colleague will pick it
up unless one will.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from ..backends.backend import Backend
from ..tools.errors import SupportToolError, describe_call
from .escalation import explicit_human_request
from .memory import ConversationMemory, Exchange, Summarizer
from .prompts import ESCALATION_NUDGE, SYSTEM_PROMPT, TRUNCATION_NOTICE
from ..tools.schema import api_tool_specs
from ..tools.session import ToolOutcome, ToolSession
from ..config.settings import Settings

ORDER_RE = re.compile(r"\bORD-\d{4}\b", re.I)


@dataclass
class TurnRecord:
    turn: int
    customer_text: str
    reply: str
    outcomes: list[ToolOutcome] = field(default_factory=list)
    stop_reasons: list[str] = field(default_factory=list)
    explicit_human_request: bool = False
    nudged: bool = False
    system_handoff: bool = False
    request_chars: int = 0
    facts_in_request: bool = False
    facts_seen: dict[str, Any] | None = None
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
        self._tools = api_tool_specs()

    # ------------------------------------------------------------------
    def build_request(self, current: list[dict[str, Any]]) -> dict[str, Any]:
        system = [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}]
        if self.case_facts:
            system.append({"type": "text", "text": self.session.facts.render()})
        return {"system": system, "tools": self._tools, "messages": self.memory.request_messages(current)}

    def handle(self, customer_text: str) -> TurnRecord:
        record = TurnRecord(self.session.begin_turn(), customer_text, reply="",
                            explicit_human_request=explicit_human_request(customer_text))
        current: list[dict[str, Any]] = [{"role": "user", "content": customer_text}]
        truncations = 0

        for _ in range(self.settings.max_tool_rounds):
            params = self.build_request(current)
            record.request_chars = max(record.request_chars, len(json.dumps(params, default=str)))
            record.facts_in_request = self.case_facts
            record.facts_seen = self.session.facts.to_dict() if self.case_facts else None
            record.summary_in_request = bool(self.memory.summary)
            turn = self.backend.create(params)
            reason = turn.stop_reason
            record.stop_reasons.append(reason)

            if reason == "tool_use":
                uses = turn.tool_uses
                if not uses:
                    self._finish_with_handoff(record, current, "empty_tool_use",
                                              "The model signalled a tool call but sent none.")
                    break
                current.append({"role": "assistant", "content": turn.content})
                results = []
                for use in uses:
                    outcome = self._execute(use.name, use.input, record)
                    record.outcomes.append(outcome)
                    results.append({"type": "tool_result", "tool_use_id": use.id, "content": outcome.content,
                                    **({"is_error": True} if outcome.is_error else {})})
                # All results for one assistant turn go back in a single user message.
                current.append({"role": "user", "content": results})
                if self.session.validation_exhausted("escalate_to_human") and not self._escalated(record):
                    self._finish_with_handoff(
                        record, current, "handoff_validation_exhausted",
                        "The agent could not produce a valid handoff record within the validation budget.",
                        reason_category=self._attempted_reason(record))
                    break
                continue

            if reason == "pause_turn":
                current.append({"role": "assistant", "content": turn.content})
                continue

            if reason == "max_tokens":
                truncations += 1
                if truncations == 1:
                    current.append({"role": "user", "content": [{"type": "text", "text": TRUNCATION_NOTICE}]})
                    continue
                self._finish_with_handoff(record, current, "max_tokens",
                                          "The model's response was cut off at the output limit twice.")
                break

            if reason in ("end_turn", "stop_sequence"):
                current.append({"role": "assistant", "content": turn.content})
                if record.explicit_human_request and not self._escalated(record):
                    if not record.nudged:
                        record.nudged = True
                        current.append({"role": "user", "content": [{"type": "text", "text": ESCALATION_NUDGE}]})
                        continue
                    self._finish_with_handoff(record, current, "explicit_request_not_escalated",
                                              "The customer asked for a human and the model did not escalate.",
                                              reason_category="customer_request")
                    break
                record.reply, record.stop = turn.text, reason
                break

            label = "refusal" if reason == "refusal" else f"unexpected_stop_reason:{reason}"
            self._finish_with_handoff(record, current, label,
                                      f"The model stopped with stop_reason {reason!r} instead of answering.")
            break
        else:
            self._finish_with_handoff(record, current, "max_tool_rounds",
                                      f"The turn used all {self.settings.max_tool_rounds} tool rounds without "
                                      "finishing.")

        self.memory.commit(Exchange(record.turn, customer_text, current, record.reply, record.tools))
        self.records.append(record)
        return record

    # ------------------------------------------------------------------
    def _execute(self, name: str, args: dict[str, Any], record: TurnRecord) -> ToolOutcome:
        if record.explicit_human_request and not self._escalated(record) and name != "escalate_to_human":
            return self.session.blocked(name, args, SupportToolError(
                "the customer explicitly asked for a human; no other action is allowed first",
                category="escalation", failure_type="escalation_required",
                attempted=describe_call(name, args),
                remediation="Call escalate_to_human with reason_category customer_request now.",
            ))
        return self.session.call(name, args)

    @staticmethod
    def _escalated(record: TurnRecord) -> bool:
        return any(o.tool == "escalate_to_human" and not o.is_error for o in record.outcomes)

    @staticmethod
    def _attempted_reason(record: TurnRecord) -> str:
        from ..tools.handlers import ESCALATION_REASONS

        for o in reversed(record.outcomes):
            if o.tool == "escalate_to_human" and o.input.get("reason_category") in ESCALATION_REASONS:
                return o.input["reason_category"]
        return "unable_to_progress"

    def _finish_with_handoff(self, record: TurnRecord, current: list[dict[str, Any]], stop: str,
                             root_cause: str, *, reason_category: str | None = None) -> None:
        record.stop = stop
        if self._escalated(record):
            ticket = next(o for o in reversed(record.outcomes) if o.tool == "escalate_to_human" and not o.is_error)
        else:
            ticket = self.system_handoff(record, reason_category or (
                "customer_request" if record.explicit_human_request else "unable_to_progress"), root_cause)
        if not ticket.is_error:
            record.reply = (f"I've passed this to a colleague (ticket {ticket.payload['ticket_id']}, expected wait "
                            f"about {ticket.payload['estimated_wait_minutes']} minutes). They'll see everything "
                            "we've covered, so you won't need to repeat yourself.")
        else:
            record.reply = ("I'm sorry, I couldn't finish that and our handoff system didn't respond. Everything "
                            "we covered is saved; please contact us again and a colleague will pick it up.")
        current.append({"role": "assistant", "content": [{"type": "text", "text": record.reply}]})

    def system_handoff(self, record: TurnRecord, reason_category: str, root_cause: str) -> ToolOutcome:
        """A handoff the program files itself -- through the same validator as the model's."""
        facts = self.session.facts
        customer_id = facts.customer.customer_id if facts.customer else None
        mentioned = {m.upper() for m in ORDER_RE.findall(record.customer_text)}
        args = {
            "reason_category": reason_category,
            "customer_id": customer_id,
            "identity_verified": customer_id is not None,
            "order_ids": sorted(mentioned | set(facts.orders)),
            "customer_request": f'The customer wrote: "{record.customer_text[:280]}"',
            "root_cause": root_cause,
            "recommended_action": ("Read the customer's message and the case facts and errors attached to this "
                                   "ticket, then contact the customer to resolve the request directly."),
            "actions_taken": [f"{o.tool}: {o.payload.get('failureType') or 'ok'}" for o in record.outcomes],
        }
        outcome = self.session.call("escalate_to_human", args, authored_by="system")
        record.outcomes.append(outcome)
        record.system_handoff = True
        return outcome
