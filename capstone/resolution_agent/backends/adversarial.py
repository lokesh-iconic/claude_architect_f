"""Scripted backends that break the rules on purpose.

The mock agent follows the rules, so it cannot show that the *program*
enforces them. These backends refund before verifying, send malformed action
records, look things up when the customer asked for a human, and return
every stop_reason the loop has to handle. The checks assert the harness
stops or recovers from each one anyway.
"""

from __future__ import annotations

import json
from typing import Any

from .backend import ModelTurn


def _tool(name: str, args: dict[str, Any], n: int) -> ModelTurn:
    return ModelTurn("tool_use", [{"type": "tool_use", "id": f"toolu_adv_{n:04d}", "name": name, "input": args}])


def _text(text: str, stop_reason: str = "end_turn") -> ModelTurn:
    return ModelTurn(stop_reason, [{"type": "text", "text": text}])


def _turn_results(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """tool_result payloads since the last plain customer message."""
    out: list[dict[str, Any]] = []
    for msg in messages:
        content = msg["content"]
        if msg["role"] == "user" and isinstance(content, str):
            out = []
        elif msg["role"] == "user":
            for b in content:
                if b.get("type") == "tool_result":
                    out.append(json.loads(b["content"]))
    return out


class ScriptedToolBackend:
    """Makes a fixed list of tool calls on each customer turn, whatever comes back.

    `plan` is a list of per-turn actions: each is a list of (tool, args)
    calls to make in order before replying "Done.".
    """

    name = "scripted"

    def __init__(self, plan: list[list[tuple[str, dict[str, Any]]]]) -> None:
        self.plan = plan
        self.turn = -1
        self.step = 0
        self.n = 0

    def create(self, params: dict[str, Any]) -> ModelTurn:
        last = params["messages"][-1]
        if last["role"] == "user" and isinstance(last["content"], str):
            self.turn, self.step = self.turn + 1, 0
        calls = self.plan[self.turn] if self.turn < len(self.plan) else []
        if self.step < len(calls):
            name, args = calls[self.step]
            self.step += 1
            self.n += 1
            return _tool(name, args, self.n)
        return _text("Done.")


# The reckless refunder is the scripted backend pointed at process_refund.
RecklessRefundBackend = ScriptedToolBackend


class SequenceBackend:
    """Returns the given model turns in order, one per request; then "Done."."""

    name = "sequence"

    def __init__(self, turns: list[ModelTurn]) -> None:
        self.turns = list(turns)
        self.requests: list[dict[str, Any]] = []

    def create(self, params: dict[str, Any]) -> ModelTurn:
        self.requests.append(params)
        return self.turns.pop(0) if self.turns else _text("Done.")


UNVERIFIED_HANDOFF = {
    "reason_category": "customer_request", "customer_id": None, "identity_verified": False,
    "order_ids": ["ORD-5530"],
    "customer_request": "Customer wants to talk to a person about ORD-5530.",
    "root_cause": "The customer explicitly asked for a human before any troubleshooting.",
    "recommended_action": "Contact the customer about ORD-5530 after verifying their email and postal code.",
    "actions_taken": [],
}


class PromptIgnoringBackend:
    """On an explicit request for a human, tries to solve the issue anyway.

    It calls lookup_order, then tries to finish with prose. Only after the
    harness nudge does it escalate -- so the check can show the gate blocked
    the lookup and the nudge produced the handoff. With `stubborn=True` it
    ignores the nudge too, and the program has to file the handoff itself.
    """

    name = "prompt-ignoring"

    def __init__(self, order_id: str = "ORD-5530", *, stubborn: bool = False) -> None:
        self.order_id = order_id
        self.stubborn = stubborn
        self.n = 0

    def create(self, params: dict[str, Any]) -> ModelTurn:
        self.n += 1
        messages = params["messages"]
        last = messages[-1]
        texts = [b.get("text", "") for b in last["content"]] if isinstance(last["content"], list) else []
        if any(t.startswith("<harness_notice>") for t in texts) and not self.stubborn:
            return _tool("escalate_to_human", dict(UNVERIFIED_HANDOFF), self.n)
        results = _turn_results(messages)
        if any(r.get("ticket_id") for r in results):
            return _text("A colleague will be with you shortly.")
        if not results:
            return _tool("lookup_order", {"order_id": self.order_id}, self.n)
        return _text("Your order is on its way, no need for a human!")
