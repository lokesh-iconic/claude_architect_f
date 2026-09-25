"""One conversation's tool session -- the single path every tool call takes.

In order, for every call, each step able to stop it before the handler runs:

1. **Scope.** A tool outside the four this workflow needs is rejected.
2. **Refund prerequisite.** `process_refund` is rejected unless `get_customer`
   returned `verified: true` for that exact `customer_id` earlier in this
   session. It runs before schema validation, so an unverified refund is a
   `prerequisite` error however malformed it is.
3. **Contract.** The input is checked against the full schema (constraints
   strict mode cannot carry), and action records are checked against the
   case facts. A failure is a retryable `validation` error listing each
   issue; the retry budget turns it non-retryable after
   `max_action_attempts`, or at once if the model resubmits the same issues.
4. **Deadline**, **retry budget** for transient failures, **trimming** and
   **fact extraction**, as in module 5.
5. **Ledger.** Every action record that passed step 3 and reached its
   handler is written to `ledger`, whatever the handler decided -- so a
   handler invocation for a money or case-state tool without a validated
   record cannot happen.

The MCP server holds one of these per server process (stdio is one client
per process), so every guarantee holds over MCP as well as in-process.
"""

from __future__ import annotations

import copy
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from . import trimming
from .actions import SEMANTIC_CHECKS, ActionRecord
from .errors import SupportToolError, describe_call
from ..conversation.facts import CaseFacts
from .handlers import HANDLERS, TOOL_NAMES, Faults, Progress, UpstreamState, _Cancelled
from .schema import ACTION_TOOLS, validate_input


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
    authored_by: str = "agent"

    @property
    def content(self) -> str:
        return json.dumps(self.payload, separators=(",", ":"))


@dataclass
class ToolSession:
    tool_timeout_s: float = 5.0
    faults: Faults = field(default_factory=Faults)
    max_transient_retries: int = 1
    max_action_attempts: int = 3
    state: UpstreamState = field(default_factory=UpstreamState)
    facts: CaseFacts = field(default_factory=CaseFacts)
    verified_ids: set[str] = field(default_factory=set)
    log: list[ToolOutcome] = field(default_factory=list)
    ledger: list[ActionRecord] = field(default_factory=list)
    turn: int = 0
    _transient: dict[str, int] = field(default_factory=dict)
    _invalid: dict[str, list[frozenset]] = field(default_factory=dict)
    _rejected: dict[str, list[list[dict[str, str]]]] = field(default_factory=dict)

    # ------------------------------------------------------------------
    def begin_turn(self) -> int:
        self.turn += 1
        self._invalid.clear()
        self._rejected.clear()
        return self.turn

    def call(self, tool: str, args: dict[str, Any], *, authored_by: str = "agent") -> ToolOutcome:
        args = dict(args or {})
        started = time.perf_counter()
        pending: dict[str, Any] = {}
        try:
            outcome = self._call(tool, args, pending)
        except SupportToolError as exc:
            outcome = ToolOutcome(tool, args, True, exc.to_payload(),
                                  blocked=exc.category in ("prerequisite", "escalation"),
                                  handler_ran="record" in pending)
            self.facts.record_failure(tool, outcome.payload, self.turn)
        outcome.elapsed_s = round(time.perf_counter() - started, 3)
        outcome.turn = self.turn
        outcome.authored_by = authored_by
        if "record" in pending:
            self._write_ledger(tool, pending["record"], outcome, authored_by)
        self.log.append(outcome)
        return outcome

    def blocked(self, tool: str, args: dict[str, Any], error: SupportToolError) -> ToolOutcome:
        """Record a call the *agent layer* refused (e.g. the escalation gate)."""
        outcome = ToolOutcome(tool, dict(args), True, error.to_payload(), blocked=True, turn=self.turn)
        self.log.append(outcome)
        return outcome

    def _call(self, tool: str, args: dict[str, Any], pending: dict[str, Any]) -> ToolOutcome:
        attempted = describe_call(tool, args)
        handler = HANDLERS.get(tool)
        if handler is None or tool not in TOOL_NAMES:
            raise SupportToolError(
                f"{tool!r} is not a tool in this workflow", category="validation",
                failure_type="tool_out_of_scope", attempted=attempted,
                remediation=f"Use one of {', '.join(TOOL_NAMES)}.",
            )
        if tool == "process_refund":
            self._require_verified(str(args.get("customer_id") or ""), attempted)
        self._check_contract(tool, args, attempted)

        if tool in ACTION_TOOLS:
            pending["record"] = copy.deepcopy(args)
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

    def _check_contract(self, tool: str, args: dict[str, Any], attempted: str) -> None:
        issues = validate_input(tool, args)
        # Report every problem in one round trip, but only judge fields whose shape is sound.
        if tool in SEMANTIC_CHECKS and not any(i.code == "wrong_type" for i in issues):
            flagged = {i.field for i in issues}
            issues += [i for i in SEMANTIC_CHECKS[tool](args, self.facts) if i.field not in flagged]
        if not issues:
            self._invalid.pop(tool, None)
            return
        payloads = [i.to_payload() for i in issues]
        if tool not in ACTION_TOOLS:
            raise SupportToolError(
                f"{tool} input does not match its contract", category="validation",
                failure_type="invalid_input", attempted=attempted,
                remediation="Fix the listed fields and call again.", detail={"issues": payloads},
            )
        keys = frozenset((i.field, i.code) for i in issues)
        history = self._invalid.setdefault(tool, [])
        repeated = bool(history) and history[-1] == keys
        history.append(keys)
        self._rejected.setdefault(tool, []).append(payloads)
        attempt = len(history)
        exhausted = repeated or attempt >= self.max_action_attempts
        if not exhausted:
            remediation = f"Nothing was executed. Correct the listed fields and call {tool} again."
        elif tool == "process_refund":
            remediation = ("Validation budget exhausted; nothing was executed. Do not retry. Tell the customer "
                           "the refund could not be completed and call escalate_to_human with reason_category "
                           "unable_to_progress.")
        else:
            remediation = "Validation budget exhausted; the system will file the handoff itself."
        raise SupportToolError(
            f"{tool} action record rejected before execution: {len(issues)} issue(s)",
            category="validation", failure_type="invalid_action_record", attempted=attempted,
            retryable=not exhausted, remediation=remediation,
            detail={"issues": payloads, "validationAttempt": attempt, "maxAttempts": self.max_action_attempts,
                    **({"sameIssuesAsLastAttempt": True} if repeated else {})},
        )

    def validation_exhausted(self, tool: str) -> bool:
        history = self._invalid.get(tool, [])
        return len(history) >= self.max_action_attempts or (len(history) > 1 and history[-1] == history[-2])

    def _write_ledger(self, tool: str, record: dict[str, Any], outcome: ToolOutcome, authored_by: str) -> None:
        rejected = self._rejected.pop(tool, [])
        status = f"not_executed: {outcome.payload.get('failureType')}" if outcome.is_error else "executed"
        self.ledger.append(ActionRecord(
            action_id=f"AR-{len(self.ledger) + 1:04d}", tool=tool, turn=self.turn, authored_by=authored_by,
            record=record, validation_attempts=len(rejected) + 1, rejected_attempts=rejected, status=status,
            result_id=None if outcome.is_error else outcome.payload.get("refund_id") or outcome.payload.get("ticket_id"),
        ))

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
