"""One conversation's tool session -- the single path every tool call takes.

In order, for every call:

1. **Refund prerequisite.** `process_refund` is rejected before its handler
   runs unless `get_customer` has returned `verified: true` for that exact
   `customer_id` earlier in this session. This is program state, not
   something the model has to remember, so it holds on every call -- the
   system prompt also says it, but nothing depends on the prompt being obeyed.
2. **Deadline.** The handler runs on a worker thread with a timeout. A call
   that overruns returns a structured `timeout` error carrying what was
   attempted and whatever the handler finished before the deadline.
3. **Retry budget.** Repeating a call that already failed transiently is
   allowed once; after that the payload flips to `isRetryable: false` and
   says to escalate, so a model cannot loop on a dead dependency.
4. **Trimming**, then **fact extraction** from the trimmed result.

The MCP server holds one of these per server process (stdio is one client
per process), so the same guarantees hold over MCP as in-process.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from . import trimming
from .errors import SupportToolError, describe_call
from ..conversation.facts import CaseFacts
from .handlers import HANDLERS, Faults, Progress, UpstreamState, _Cancelled


@dataclass
class ToolOutcome:
    tool: str
    input: dict[str, Any]
    is_error: bool
    payload: dict[str, Any]
    raw_chars: int = 0
    trimmed_chars: int = 0
    blocked: bool = False
    handler_ran: bool = False
    elapsed_s: float = 0.0
    turn: int = 0

    @property
    def content(self) -> str:
        return json.dumps(self.payload, separators=(",", ":"))


@dataclass
class ToolSession:
    tool_timeout_s: float = 5.0
    faults: Faults = field(default_factory=Faults)
    max_transient_retries: int = 1
    state: UpstreamState = field(default_factory=UpstreamState)
    facts: CaseFacts = field(default_factory=CaseFacts)
    verified_ids: set[str] = field(default_factory=set)
    log: list[ToolOutcome] = field(default_factory=list)
    turn: int = 0
    _transient: dict[str, int] = field(default_factory=dict)

    # ------------------------------------------------------------------
    def call(self, tool: str, args: dict[str, Any]) -> ToolOutcome:
        args = dict(args or {})
        started = time.perf_counter()
        try:
            outcome = self._call(tool, args)
        except SupportToolError as exc:
            outcome = ToolOutcome(tool, args, True, exc.to_payload(),
                                  blocked=exc.category in ("prerequisite", "escalation"))
            self.facts.record_failure(tool, outcome.payload, self.turn)
        outcome.elapsed_s = round(time.perf_counter() - started, 3)
        outcome.turn = self.turn
        self.log.append(outcome)
        return outcome

    def blocked(self, tool: str, args: dict[str, Any], error: SupportToolError) -> ToolOutcome:
        """Record a call the *agent layer* refused (e.g. the escalation gate)."""
        outcome = ToolOutcome(tool, dict(args), True, error.to_payload(), blocked=True, turn=self.turn)
        self.log.append(outcome)
        return outcome

    def _call(self, tool: str, args: dict[str, Any]) -> ToolOutcome:
        attempted = describe_call(tool, args)
        handler = HANDLERS.get(tool)
        if handler is None:
            raise SupportToolError(f"unknown tool {tool!r}", category="validation",
                                   failure_type="unknown_tool", attempted=attempted)

        if tool == "process_refund":
            self._require_verified(str(args.get("customer_id") or ""), attempted)
        if tool == "escalate_to_human":
            args = {**args, "handoff": self.handoff_context()}

        self.state.invocations[tool] += 1
        raw = self._run_with_deadline(tool, handler, args, attempted)
        trimmed = trimming.trim(tool, raw)
        if tool == "get_customer" and trimmed.get("verified"):
            self.verified_ids.add(trimmed["customer_id"])
        self.facts.record_success(tool, trimmed, self.turn)
        self._transient.pop(attempted, None)
        return ToolOutcome(tool, args, False, trimmed, raw_chars=trimming.size(raw),
                           trimmed_chars=trimming.size(trimmed), handler_ran=True)

    # ------------------------------------------------------------------
    def _require_verified(self, customer_id: str, attempted: str) -> None:
        if customer_id and customer_id in self.verified_ids:
            return
        if not self.verified_ids:
            why = "get_customer has not returned a verified customer in this session"
        else:
            why = (f"customer_id {customer_id!r} is not the customer verified in this session "
                   f"({', '.join(sorted(self.verified_ids))})")
        raise SupportToolError(
            f"refund blocked: {why}",
            category="prerequisite", failure_type="identity_not_verified", attempted=attempted,
            remediation="Ask for the email and postal code on the account and call get_customer. "
                        "Call process_refund only with the customer_id it returns with verified: true.",
        )

    def _run_with_deadline(self, tool: str, handler, args: dict[str, Any], attempted: str) -> dict[str, Any]:
        progress, cancel, box = Progress(), threading.Event(), {}

        def target() -> None:
            try:
                box["value"] = handler(args, self.state, self.faults, progress, cancel)
            except _Cancelled:
                box["cancelled"] = True
            except SupportToolError as exc:
                box["error"] = exc
            except Exception as exc:  # a handler bug still comes back structured
                box["error"] = SupportToolError(
                    f"{type(exc).__name__}: {exc}", category="internal",
                    failure_type="handler_exception", attempted=attempted)

        worker = threading.Thread(target=target, name=f"tool-{tool}", daemon=True)
        worker.start()
        worker.join(self.tool_timeout_s)
        if worker.is_alive() or box.get("cancelled"):
            cancel.set()
            raise self._timeout_error(tool, attempted, progress)
        if "error" in box:
            raise box["error"]
        return box["value"]

    def _timeout_error(self, tool: str, attempted: str, progress: Progress) -> SupportToolError:
        attempt = self._transient.get(attempted, 0) + 1
        self._transient[attempted] = attempt
        exhausted = attempt > self.max_transient_retries
        completed = [step for step, _ in progress.steps]
        return SupportToolError(
            f"{tool} did not respond within {self.tool_timeout_s:g}s",
            category="transient", failure_type="timeout", attempted=attempted,
            retryable=not exhausted,
            remediation=(
                "Retry budget exhausted. Do not retry again and do not guess the missing data: "
                "tell the customer, then call escalate_to_human with reason_category "
                "unable_to_progress."
                if exhausted else
                "Retry this call once. If it fails again, escalate with reason_category "
                "unable_to_progress."
            ),
            partial=trimming.trim_partial(tool, progress.steps) or None,
            detail={
                "attemptNumber": attempt,
                "deadlineSeconds": self.tool_timeout_s,
                "completedSteps": completed,
                "incompleteStep": _next_step(tool, completed),
            },
        )

    # ------------------------------------------------------------------
    def handoff_context(self) -> dict[str, Any]:
        """Attached to every escalation by the program, so a reviewer never gets a bare ticket."""
        errors = [o.payload for o in self.log if o.is_error][-3:]
        return {"case_facts": self.facts.to_dict(), "recent_errors": errors, "turn": self.turn}


_STEPS = {
    "lookup_order": ["order_header", "shipment"],
    "get_customer": ["profile"],
    "process_refund": ["refund"],
    "escalate_to_human": ["ticket"],
}


def _next_step(tool: str, completed: list[str]) -> str | None:
    return next((s for s in _STEPS.get(tool, []) if s not in completed), None)
