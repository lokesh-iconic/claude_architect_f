"""Tool-selection evaluation.

Answers the first self-check question: does the agent reliably pick the right
tool between two that overlap, across repeated tries?

The same cases run against both description generations, with identical
schemas, so the only variable is the prose. Live mode asks the model; mock
mode uses a discriminability proxy (see `proxy_select`).
"""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from ..config.settings import Settings
from ..tools.toolspecs import TOOL_NAMES, VERSIONS, tool_definitions


@dataclass(frozen=True)
class Case:
    id: str
    prompt: str
    expected: str
    ambiguous: bool
    why: str


# The four `ambiguous=True` cases are the ones the overlapping pair fights
# over: each names a file or module, which reads like "search for this text"
# but must route to the structured path lookup.
CASES: tuple[Case, ...] = (
    Case(
        "path-exact",
        "Which issues touch sample_service/auth/session.py?",
        "find_issues_for_path",
        True,
        "Names an exact linked path.",
    ),
    Case(
        "path-dir",
        "Is anything open in sample_service/checkout?",
        "find_issues_for_path",
        True,
        "A directory, which the path index matches by prefix.",
    ),
    Case(
        "path-module",
        "What outstanding work is there on the auth module?",
        "find_issues_for_path",
        True,
        "Module-shaped, no quoted path - the hardest routing case.",
    ),
    Case(
        "path-file-bare",
        "Any open issues touching pricing.py?",
        "find_issues_for_path",
        True,
        "A bare filename; still a path question, not a text search.",
    ),
    Case(
        "text-theme",
        "Show me the open security bugs.",
        "search_issues",
        False,
        "A theme that lives in labels and prose.",
    ),
    Case(
        "text-symptom",
        "Is there anything about rounding errors on order totals?",
        "search_issues",
        False,
        "A symptom described in the user's own words.",
    ),
    Case(
        "key-detail",
        "What is the story on CHK-104?",
        "get_issue",
        False,
        "An issue key is already in hand.",
    ),
    Case(
        "key-comments",
        "Who commented on PLAT-31 and what did they say?",
        "get_issue",
        False,
        "Comments come only from the detail tool.",
    ),
    Case(
        "sprint-all",
        "What is on the board this sprint?",
        "list_sprint_board",
        False,
        "Sprint-wide question.",
    ),
    Case(
        "sprint-status",
        "What is still in review this sprint?",
        "list_sprint_board",
        False,
        "Sprint-wide, filtered by column.",
    ),
)

SYSTEM = (
    "You are a developer-productivity agent with access to an internal issue "
    "tracker. Choose the single most appropriate tool for the user's request "
    "and call it with well-formed arguments."
)

UNDECIDED = "__undecided__"
NO_CALL = "__no_tool_call__"


@dataclass
class CaseResult:
    case: Case
    picks: list[str] = field(default_factory=list)

    @property
    def correct(self) -> int:
        return sum(1 for p in self.picks if p == self.case.expected)

    @property
    def trials(self) -> int:
        return len(self.picks)

    @property
    def accuracy(self) -> float:
        return self.correct / self.trials if self.trials else 0.0

    @property
    def consistent(self) -> bool:
        """Same answer every time - reliability, separate from correctness."""
        return len(set(self.picks)) == 1

    @property
    def confusions(self) -> dict[str, int]:
        return {k: v for k, v in Counter(self.picks).items() if k != self.case.expected}


@dataclass
class VersionResult:
    version: str
    mode: str
    results: list[CaseResult]

    @property
    def accuracy(self) -> float:
        total = sum(r.trials for r in self.results)
        return sum(r.correct for r in self.results) / total if total else 0.0

    @property
    def ambiguous_accuracy(self) -> float:
        rows = [r for r in self.results if r.case.ambiguous]
        total = sum(r.trials for r in rows)
        return sum(r.correct for r in rows) / total if total else 0.0

    @property
    def failures(self) -> list[CaseResult]:
        return [r for r in self.results if r.accuracy < 1.0]


# --------------------------------------------------------------------------
# Offline proxy
# --------------------------------------------------------------------------

STOPWORDS = frozenset(
    """a an and any are as at be by can do does for from has have in is it its of on or
    that the there this to use used user what when which who with your you me my""".split()
)


def _tokens(text: str) -> list[str]:
    cleaned = "".join(c.lower() if (c.isalnum() or c in "/._-") else " " for c in text)
    return [t for t in cleaned.split() if t not in STOPWORDS and len(t) > 2]


def proxy_select(prompt: str, version: str) -> str:
    """Pick a tool by how *discriminating* its description is for this prompt.

    This is not a model. It scores each tool by the prompt terms appearing in
    its description, weighting each term by how few tools mention it - so a
    word present in every description contributes nothing, and a word unique
    to one description decides the match. That makes it a fair measure of one
    specific thing: whether the descriptions carry the vocabulary needed to
    tell the tools apart. It is a proxy for description quality, not evidence
    about how Claude behaves.
    """
    descriptions = VERSIONS[version]
    doc_tokens = {name: set(_tokens(text)) for name, text in descriptions.items()}
    prompt_tokens = _tokens(prompt)

    scores: dict[str, float] = {name: 0.0 for name in TOOL_NAMES}
    for term in prompt_tokens:
        holders = [name for name, toks in doc_tokens.items() if term in toks]
        if not holders or len(holders) == len(TOOL_NAMES):
            continue  # absent, or present everywhere: carries no signal
        weight = 1.0 / len(holders)
        for name in holders:
            scores[name] += weight

    best = max(scores.values())
    if best == 0.0:
        # Nothing in the descriptions distinguishes the tools for this prompt.
        return UNDECIDED
    winners = sorted(name for name, score in scores.items() if score == best)
    return winners[0] if len(winners) == 1 else UNDECIDED


# --------------------------------------------------------------------------
# Live
# --------------------------------------------------------------------------


async def live_select(settings: Settings, prompt: str, version: str) -> str:
    import anthropic

    client = anthropic.AsyncAnthropic(api_key=settings.api_key)
    response = await client.messages.create(
        model=settings.model,
        max_tokens=settings.max_tokens,
        system=SYSTEM,
        messages=[{"role": "user", "content": prompt}],
        tools=tool_definitions(version),
        # Force a call so we measure routing, not whether it answers in prose.
        tool_choice={"type": "any", "disable_parallel_tool_use": True},
        output_config={"effort": settings.effort},
    )
    for block in response.content:
        if block.type == "tool_use":
            return block.name
    return NO_CALL


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------


async def run_version(
    settings: Settings, version: str, trials: int, cases: tuple[Case, ...] = CASES
) -> VersionResult:
    results: list[CaseResult] = []
    for case in cases:
        row = CaseResult(case=case)
        if settings.is_live:
            picks = await asyncio.gather(
                *(live_select(settings, case.prompt, version) for _ in range(trials))
            )
            row.picks.extend(picks)
        else:
            # The proxy is deterministic, so repeating it proves nothing; one
            # evaluation is recorded per requested trial for shape parity.
            row.picks.extend([proxy_select(case.prompt, version)] * trials)
        results.append(row)
    return VersionResult(version=version, mode=settings.mode, results=results)


async def run_all(
    settings: Settings, trials: int = 5, versions: tuple[str, ...] = ("v1", "v2")
) -> dict[str, VersionResult]:
    return {version: await run_version(settings, version, trials) for version in versions}


def summarize(results: dict[str, VersionResult]) -> dict[str, Any]:
    return {
        version: {
            "overallAccuracy": round(result.accuracy, 3),
            "ambiguousPairAccuracy": round(result.ambiguous_accuracy, 3),
            "failingCases": [
                {
                    "id": row.case.id,
                    "expected": row.case.expected,
                    "picked": row.confusions,
                    "accuracy": round(row.accuracy, 3),
                }
                for row in result.failures
            ],
        }
        for version, result in results.items()
    }
