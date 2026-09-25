"""Structured tool errors.

Same shape as the repo's `ToolError` pattern (category, `isRetryable`,
remediation), plus the two fields a coordinator or human reviewer needs to
recover from a failure without re-running it blind: `attempted` (the exact
call) and `partialResults` (what completed before it failed).
"""

from __future__ import annotations

import json
from typing import Any, Literal

# transient    -> could succeed later (timeout, upstream unavailable)
# validation   -> the input was wrong; the same call cannot succeed
# permission   -> the caller may not do this (order belongs to someone else)
# prerequisite -> a required earlier step has not happened (identity not verified)
# policy       -> allowed by the tool, not by the refund policy; a human decides
# escalation   -> the harness requires a handoff before any other action
# internal     -> a bug in this code, still reported in the same shape
Category = Literal[
    "transient", "validation", "permission", "prerequisite", "policy", "escalation", "internal"
]

RETRYABLE_BY_CATEGORY: dict[str, bool] = {
    "transient": True,
    "validation": False,
    "permission": False,
    "prerequisite": False,
    "policy": False,
    "escalation": False,
    "internal": False,
}


class SupportToolError(Exception):
    """Raised at the tool boundary; serialized into every error tool_result."""

    def __init__(
        self,
        description: str,
        *,
        category: Category,
        failure_type: str,
        attempted: str,
        retryable: bool | None = None,
        remediation: str = "",
        partial: Any = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(description)
        self.description = description
        self.category: Category = category
        self.failure_type = failure_type
        self.attempted = attempted
        self.retryable = RETRYABLE_BY_CATEGORY[category] if retryable is None else retryable
        self.remediation = remediation
        self.partial = partial
        self.detail = detail or {}

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "error": True,
            "errorCategory": self.category,
            "failureType": self.failure_type,
            "isRetryable": self.retryable,
            "description": self.description,
            "attempted": self.attempted,
        }
        if self.remediation:
            payload["remediation"] = self.remediation
        if self.partial is not None:
            payload["partialResults"] = self.partial
        payload.update(self.detail)
        return payload

    def to_json(self) -> str:
        return json.dumps(self.to_payload(), indent=2)


def describe_call(tool: str, args: dict[str, Any]) -> str:
    rendered = ", ".join(f"{k}={v!r}" for k, v in args.items() if v is not None)
    return f"{tool}({rendered})"


def parse_error_payload(text: str) -> dict[str, Any]:
    """Recover the structured payload from an error tool result.

    The MCP SDK prefixes a ToolError message with "Error executing tool
    <name>: ", so the JSON starts at the first brace. Raises ValueError when
    the text is not one of ours -- meaning something replaced it with prose.
    """
    start = text.find("{")
    if start == -1:
        raise ValueError(f"not a structured support error: {text!r}")
    payload = json.loads(text[start:])
    if "errorCategory" not in payload:
        raise ValueError(f"missing errorCategory in {payload!r}")
    return payload
