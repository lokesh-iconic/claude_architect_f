"""Structured errors and validation issues.

Follows the repo's `ToolError` pattern: every failure says what kind it is,
whether trying again can help, and what to do about it. For extraction the
`isRetryable` bit is the whole game -- it is what separates "the model made a
fixable mistake" from "the document simply doesn't contain this".
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal

# transient     -> the call could succeed later (timeout, overload, expired batch row)
# configuration -> the request itself is wrong; resending it unchanged cannot help
# schema        -> the model's output did not match the extraction shape
Category = Literal["transient", "configuration", "schema"]

RETRYABLE_BY_CATEGORY: dict[str, bool] = {
    "transient": True,
    "configuration": False,
    "schema": True,
}


class ExtractionError(Exception):
    """Raised at the API boundary (live backend, batch client)."""

    def __init__(
        self,
        description: str,
        *,
        category: Category,
        retryable: bool | None = None,
        remediation: str = "",
    ) -> None:
        super().__init__(description)
        self.description = description
        self.category: Category = category
        self.retryable = RETRYABLE_BY_CATEGORY[category] if retryable is None else retryable
        self.remediation = remediation

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "error": True,
            "errorCategory": self.category,
            "isRetryable": self.retryable,
            "description": self.description,
        }
        if self.remediation:
            payload["remediation"] = self.remediation
        return payload


# format     -> shape/format is wrong but the information is there (bad date, enum misuse)
# arithmetic -> numbers don't reconcile; either a transcription slip or a source error
# grounding  -> a value has no verbatim support in the document (possible fabrication)
# absent     -> a field the business needs is genuinely not in the document
IssueKind = Literal["format", "arithmetic", "grounding", "absent", "schema"]


@dataclass
class ValidationIssue:
    code: str
    kind: IssueKind
    field: str
    message: str
    retryable: bool
    remediation: str = ""
    # Values that identify *this* failure, so an unchanged repeat can be detected.
    signature: tuple = field(default_factory=tuple)

    def to_payload(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "kind": self.kind,
            "field": self.field,
            "message": self.message,
            "isRetryable": self.retryable,
            "remediation": self.remediation,
        }

    def key(self) -> tuple:
        return (self.code, self.field, self.signature)


def issues_to_json(issues: list[ValidationIssue]) -> str:
    return json.dumps([i.to_payload() for i in issues], indent=2)
