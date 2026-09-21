"""Structured error responses.

A tool failure has to tell the agent three things it can act on: what kind of
failure it was, whether trying again could help, and what was attempted. A
bare "Error: something went wrong" string forces the agent to guess, and it
usually guesses "retry".
"""

from __future__ import annotations

import json
from typing import Any, Literal

# transient  -> the call could succeed later (timeout, upstream 5xx, rate limit)
# validation -> the input was wrong; retrying it unchanged cannot help
# permission -> the caller is not allowed; escalate to a human
Category = Literal["transient", "validation", "permission"]

RETRYABLE_BY_CATEGORY: dict[str, bool] = {
    "transient": True,
    "validation": False,
    "permission": False,
}


class TrackerError(Exception):
    """Raised by tool handlers; serialized into every error tool result."""

    def __init__(
        self,
        description: str,
        *,
        category: Category,
        attempted: str,
        retryable: bool | None = None,
        remediation: str = "",
        partial: Any = None,
    ) -> None:
        super().__init__(description)
        self.description = description
        self.category: Category = category
        self.attempted = attempted
        self.retryable = (
            RETRYABLE_BY_CATEGORY[category] if retryable is None else retryable
        )
        self.remediation = remediation
        self.partial = partial

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "error": True,
            "errorCategory": self.category,
            "isRetryable": self.retryable,
            "description": self.description,
            "attempted": self.attempted,
        }
        if self.remediation:
            payload["remediation"] = self.remediation
        if self.partial is not None:
            payload["partialResults"] = self.partial
        return payload

    def to_json(self) -> str:
        return json.dumps(self.to_payload(), indent=2)


def validation_error(description: str, *, attempted: str, remediation: str = "") -> TrackerError:
    return TrackerError(
        description, category="validation", attempted=attempted, remediation=remediation
    )


def permission_error(description: str, *, attempted: str) -> TrackerError:
    return TrackerError(
        description,
        category="permission",
        attempted=attempted,
        remediation="Ask a human with tracker admin rights; do not retry.",
    )


def transient_error(
    description: str, *, attempted: str, partial: Any = None
) -> TrackerError:
    return TrackerError(
        description,
        category="transient",
        attempted=attempted,
        remediation="Retry once after a short delay, then report the failure.",
        partial=partial,
    )


def parse_error_payload(text: str) -> dict[str, Any]:
    """Recover the structured payload from an MCP error result.

    The SDK prefixes a ToolError message with "Error executing tool <name>: ",
    so the JSON starts at the first brace. Raises ValueError if the text is
    not one of our structured errors -- which is itself worth knowing, because
    it means something upstream replaced the payload with prose.
    """
    start = text.find("{")
    if start == -1:
        raise ValueError(f"not a structured tracker error: {text!r}")
    payload = json.loads(text[start:])
    if "errorCategory" not in payload:
        raise ValueError(f"missing errorCategory in {payload!r}")
    return payload
