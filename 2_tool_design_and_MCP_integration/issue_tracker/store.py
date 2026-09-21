"""Query layer over the sample dataset.

Kept separate from the tool handlers so the same queries can be exercised by
tests and by the resource-benefit measurement without going through MCP.
"""

from __future__ import annotations

from typing import Any

from .data import CURRENT_SPRINT, ISSUES, PROJECTS, STATUSES, Issue


def all_issues() -> tuple[Issue, ...]:
    return ISSUES


def get(key: str) -> Issue | None:
    key = key.strip().upper()
    return next((i for i in ISSUES if i.key == key), None)


def text_search(
    query: str, *, project: str | None = None, status: str | None = None, limit: int = 10
) -> list[Issue]:
    """Substring match over title, description, and labels."""
    needle = query.strip().lower()
    hits: list[tuple[int, Issue]] = []
    for issue in ISSUES:
        if project and issue.project != project.upper():
            continue
        if status and issue.status != status:
            continue
        haystacks = (issue.title.lower(), issue.description.lower(), " ".join(issue.labels))
        score = sum(2 if needle in haystacks[0] else 0 for _ in (1,))
        score += 1 if needle in haystacks[1] else 0
        score += 1 if needle in haystacks[2] else 0
        if score:
            hits.append((score, issue))
    hits.sort(key=lambda pair: (-pair[0], pair[1].key))
    return [issue for _, issue in hits[:limit]]


def by_path(path: str, *, include_closed: bool = False) -> list[Issue]:
    """Issues whose linked paths match `path` exactly or by directory prefix."""
    needle = path.strip().replace("\\", "/").lstrip("./")
    matches = []
    for issue in ISSUES:
        if not include_closed and issue.status == "closed":
            continue
        for linked in issue.paths:
            if linked == needle or linked.startswith(needle.rstrip("/") + "/"):
                matches.append(issue)
                break
    matches.sort(key=lambda i: (i.priority, i.key))
    return matches


def known_paths() -> list[str]:
    return sorted({p for issue in ISSUES for p in issue.paths})


def sprint_board(sprint: str | None = None) -> dict[str, list[Issue]]:
    target = sprint or CURRENT_SPRINT
    board: dict[str, list[Issue]] = {status: [] for status in STATUSES}
    for issue in ISSUES:
        if issue.sprint == target:
            board[issue.status].append(issue)
    for bucket in board.values():
        bucket.sort(key=lambda i: (i.priority, i.key))
    return board


def catalog() -> dict[str, Any]:
    """Everything an agent would otherwise discover by probing: the shape of
    the tracker. Served as an MCP resource."""
    per_project = {}
    for key, meta in PROJECTS.items():
        issues = [i for i in ISSUES if i.project == key]
        per_project[key] = {
            "name": meta["name"],
            "lead": meta["lead"],
            "repoPath": meta["repo_path"],
            "issueCount": len(issues),
            "openCount": sum(1 for i in issues if i.status != "closed"),
            "issueKeys": [i.key for i in issues],
        }
    return {
        "currentSprint": CURRENT_SPRINT,
        "statuses": list(STATUSES),
        "priorities": sorted({i.priority for i in ISSUES}),
        "labels": sorted({label for i in ISSUES for label in i.labels}),
        "assignees": sorted({i.assignee for i in ISSUES}),
        "projects": per_project,
        "totalIssues": len(ISSUES),
        "linkedPaths": known_paths(),
    }
