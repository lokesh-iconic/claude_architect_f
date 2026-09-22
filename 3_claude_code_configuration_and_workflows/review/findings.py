"""The `Finding` type: what Claude's review produces, and how it's
fingerprinted so the same issue isn't posted twice across CI runs.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

VALID_CATEGORIES = frozenset({"bug", "security", "test-coverage", "style", "simplification"})
VALID_SEVERITIES = frozenset({"low", "medium", "high"})


class FindingsParseError(ValueError):
    """Claude's `-p --output-format json` reply didn't parse as findings."""


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    category: str
    message: str
    severity: str = "medium"

    def fingerprint(self) -> str:
        """Stable id for cross-run dedup.

        Deliberately excludes `line`: a later commit that shifts line numbers
        (e.g. adding an import above the flagged line) must not make an
        unresolved finding look new.
        """
        normalized = " ".join(self.message.strip().lower().split())
        digest = hashlib.sha256(f"{self.path}|{self.category}|{normalized}".encode("utf-8"))
        return digest.hexdigest()[:16]

    def marker(self) -> str:
        return f"<!-- claude-review:{self.fingerprint()} -->"

    def to_comment_body(self) -> str:
        return f"**[{self.category}/{self.severity}]** {self.message}\n\n{self.marker()}"


def parse_findings(raw: str) -> list[Finding]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise FindingsParseError(f"not valid JSON: {exc}") from exc

    items = data.get("findings") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise FindingsParseError("expected {'findings': [...]} or a bare JSON array")

    findings: list[Finding] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise FindingsParseError(f"finding #{i} is not an object")
        try:
            findings.append(
                Finding(
                    path=str(item["path"]),
                    line=int(item["line"]),
                    category=str(item.get("category", "bug")),
                    message=str(item["message"]),
                    severity=str(item.get("severity", "medium")),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise FindingsParseError(f"finding #{i} malformed: {exc}") from exc
    return findings
