"""Result types shared by the scenario runner, the suites and the reports."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..conversation.agent import TurnRecord


@dataclass
class Check:
    claim: str
    passed: bool
    detail: str


@dataclass
class Transcript:
    title: str
    records: list[TurnRecord]
    note: str = ""


@dataclass
class SuiteResult:
    name: str
    checks: list[Check] = field(default_factory=list)
    transcripts: list[Transcript] = field(default_factory=list)
    tables: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)
