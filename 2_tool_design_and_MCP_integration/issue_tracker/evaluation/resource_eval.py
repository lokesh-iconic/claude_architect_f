"""Do MCP resources measurably reduce exploratory tool calls?

What this measures, precisely: for each question, how many tool calls it takes
before every required fact is present in the agent's context -- with the
`issues://catalog` resource preloaded, and without it.

What this is NOT: a measurement of how Claude behaves. The probe plan (the
call sequence an agent runs when it does not know the tracker's shape) is
authored here, not observed. So this is an analysis of the information
architecture: it shows what the resource makes unnecessary. Both conditions
run the *same* probe plan against the *real* handlers and the *same*
fact-satisfaction check, so the difference between them is not hand-written.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ..tools import handlers, resources


@dataclass(frozen=True)
class Question:
    id: str
    text: str
    # Every string here must appear in the gathered context for the question
    # to count as answerable.
    required_facts: tuple[str, ...]
    # What an agent that does not know the tracker's shape would have to try,
    # in order, to uncover those facts.
    probe_plan: tuple[tuple[str, dict[str, Any]], ...]


QUESTIONS: tuple[Question, ...] = (
    Question(
        "statuses",
        "What status values can I filter on?",
        ("open", "in_progress", "in_review", "closed"),
        (("list_sprint_board", {}),),
    ),
    Question(
        "projects",
        "Which projects exist, and what is every issue in each of them?",
        # AUTH-83 and PLAT-52 carry no sprint, so the board never shows them:
        # a complete answer needs more than the obvious probe.
        ("CHK", "AUTH", "PLAT", "AUTH-83", "PLAT-52"),
        (
            ("list_sprint_board", {}),
            ("search_issues", {"query": "bug", "limit": 25}),
            ("search_issues", {"query": "security", "limit": 25}),
        ),
    ),
    Question(
        "repo-mapping",
        "Which project owns the sample_service/checkout directory?",
        ("CHK",),
        (
            ("find_issues_for_path", {"path": "sample_service/checkout"}),
        ),
    ),
    Question(
        "linked-paths",
        "Which source paths is the tracker able to match on?",
        (
            "sample_service/auth/session.py",
            "sample_service/checkout/pricing.py",
            "sample_service/platform/retry.py",
        ),
        (
            # Linked paths are only exposed by get_issue, one issue at a time,
            # and you need the keys before you can ask.
            ("list_sprint_board", {}),
            ("get_issue", {"key": "AUTH-77"}),
            ("get_issue", {"key": "CHK-104"}),
            ("get_issue", {"key": "CHK-118"}),
            ("get_issue", {"key": "PLAT-31"}),
        ),
    ),
)

CATALOG_URI = "issues://catalog"


@dataclass
class QuestionResult:
    question: Question
    calls_without: int
    calls_with: int
    satisfied_without: bool
    satisfied_with: bool
    missing_without: list[str] = field(default_factory=list)
    missing_with: list[str] = field(default_factory=list)

    @property
    def saved(self) -> int:
        return self.calls_without - self.calls_with


def _missing(context: str, facts: tuple[str, ...]) -> list[str]:
    return [f for f in facts if f not in context]


def _probe(question: Question, seed: str) -> tuple[int, bool, list[str]]:
    """Run the probe plan until every fact is in context. Returns
    (calls made, satisfied, still-missing facts)."""
    context = seed
    if not _missing(context, question.required_facts):
        return 0, True, []

    calls = 0
    for tool, args in question.probe_plan:
        try:
            result = handlers.call(tool, args)
            context += "\n" + json.dumps(result)
        except Exception as exc:  # a failed probe still costs a call
            context += f"\n{exc}"
        calls += 1
        if not _missing(context, question.required_facts):
            return calls, True, []

    return calls, False, _missing(context, question.required_facts)


def run() -> list[QuestionResult]:
    catalog = resources.read(CATALOG_URI)
    rows: list[QuestionResult] = []
    for question in QUESTIONS:
        without_calls, without_ok, without_missing = _probe(question, seed="")
        with_calls, with_ok, with_missing = _probe(question, seed=catalog)
        rows.append(
            QuestionResult(
                question=question,
                calls_without=without_calls,
                calls_with=with_calls,
                satisfied_without=without_ok,
                satisfied_with=with_ok,
                missing_without=without_missing,
                missing_with=with_missing,
            )
        )
    return rows


def summarize(rows: list[QuestionResult]) -> dict[str, Any]:
    total_without = sum(r.calls_without for r in rows)
    total_with = sum(r.calls_with for r in rows)
    return {
        "method": (
            "Call-count analysis of the information architecture, not a "
            "measurement of model behaviour. Both conditions run the same "
            "authored probe plan against the real tool handlers."
        ),
        "questions": len(rows),
        "toolCallsWithoutResources": total_without,
        "toolCallsWithResources": total_with,
        "callsAvoided": total_without - total_with,
        "unanswerableWithoutResources": [
            r.question.id for r in rows if not r.satisfied_without
        ],
        "perQuestion": [
            {
                "id": r.question.id,
                "question": r.question.text,
                "without": r.calls_without,
                "with": r.calls_with,
                "saved": r.saved,
                "answerableWithoutResources": r.satisfied_without,
                "stillMissingWithoutResources": r.missing_without,
            }
            for r in rows
        ],
    }
