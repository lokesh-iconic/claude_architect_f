"""Tool implementations, independent of transport.

The MCP server and the evaluation harness both call these, so a behaviour
verified in a test is the same behaviour an MCP client gets.
"""

from __future__ import annotations

import os
from typing import Any, Callable

from ..core import store
from ..core.data import PROJECTS, RESTRICTED_KEYS, STATUSES
from ..core.errors import TrackerError, permission_error, transient_error, validation_error

# Set TRACKER_SIMULATE=transient|permission|validation to force the next call
# on the named tool to fail. Wired through .mcp.json so a failure can be
# triggered from a real Claude Code session.
SIMULATE_ENV = "TRACKER_SIMULATE"
SIMULATE_TOOL_ENV = "TRACKER_SIMULATE_TOOL"


def _maybe_simulate(tool: str, args: dict[str, Any]) -> None:
    mode = (os.getenv(SIMULATE_ENV) or "").strip().lower()
    if not mode:
        return
    target = (os.getenv(SIMULATE_TOOL_ENV) or "").strip()
    if target and target != tool:
        return

    attempted = f"{tool}({args})"
    if mode == "transient":
        raise transient_error(
            "issue tracker upstream did not respond within 5s",
            attempted=attempted,
            partial={"note": "no rows were read; the query was never executed"},
        )
    if mode == "permission":
        raise permission_error(
            "the configured tracker token lacks read scope for this project",
            attempted=attempted,
        )
    if mode == "validation":
        raise validation_error(
            "simulated validation failure", attempted=attempted, remediation="Fix the input."
        )
    raise TrackerError(
        f"unknown simulation mode {mode!r}",
        category="validation",
        attempted=attempted,
        remediation=f"Set {SIMULATE_ENV} to transient, permission, or validation.",
    )


def _require_str(args: dict[str, Any], field: str, tool: str) -> str:
    value = args.get(field)
    if not isinstance(value, str) or not value.strip():
        raise validation_error(
            f"{field!r} is required and must be a non-empty string",
            attempted=f"{tool}({args})",
            remediation=f"Call {tool} again with a {field}.",
        )
    return value.strip()


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------


def search_issues(args: dict[str, Any]) -> dict[str, Any]:
    _maybe_simulate("search_issues", args)
    query = _require_str(args, "query", "search_issues")

    if "/" in query or query.endswith((".py", ".ts", ".go", ".java")):
        raise validation_error(
            f"{query!r} looks like a file path, and search_issues only matches "
            "issue prose. Most issues never mention their own paths.",
            attempted=f"search_issues(query={query!r})",
            remediation="Call find_issues_for_path with this path instead.",
        )

    project = args.get("project")
    if project is not None:
        project = str(project).upper()
        if project not in PROJECTS:
            raise validation_error(
                f"unknown project {project!r}",
                attempted=f"search_issues(project={project!r})",
                remediation=f"Valid project keys: {', '.join(sorted(PROJECTS))}.",
            )

    status = args.get("status")
    if status is not None and status not in STATUSES:
        raise validation_error(
            f"unknown status {status!r}",
            attempted=f"search_issues(status={status!r})",
            remediation=f"Valid statuses: {', '.join(STATUSES)}.",
        )

    limit = args.get("limit", 10)
    if not isinstance(limit, int) or not 1 <= limit <= 25:
        raise validation_error(
            f"limit must be an integer between 1 and 25, got {limit!r}",
            attempted=f"search_issues(limit={limit!r})",
        )

    hits = store.text_search(query, project=project, status=status, limit=limit)
    return {
        "query": query,
        "matchCount": len(hits),
        "issues": [i.summary() for i in hits],
        "note": "Summaries only. Call get_issue for description, comments and linked paths.",
    }


def find_issues_for_path(args: dict[str, Any]) -> dict[str, Any]:
    _maybe_simulate("find_issues_for_path", args)
    path = _require_str(args, "path", "find_issues_for_path").replace("\\", "/").lstrip("./")

    if "/" not in path:
        raise validation_error(
            f"{path!r} is a bare filename, not a repo-relative path. This tool "
            "matches the tracker's path index exactly or by directory prefix.",
            attempted=f"find_issues_for_path(path={path!r})",
            remediation=(
                "Pass a full repo-relative path such as "
                "'sample_service/auth/session.py'. Known paths are listed in the "
                "issues://catalog resource."
            ),
        )

    include_closed = bool(args.get("include_closed", False))
    matches = store.by_path(path, include_closed=include_closed)
    return {
        "path": path,
        "matchCount": len(matches),
        "includedClosed": include_closed,
        "issues": [i.summary() for i in matches],
    }


def get_issue(args: dict[str, Any]) -> dict[str, Any]:
    _maybe_simulate("get_issue", args)
    key = _require_str(args, "key", "get_issue").upper()

    if key in RESTRICTED_KEYS:
        raise permission_error(
            f"{key} is in a restricted project the current token cannot read",
            attempted=f"get_issue(key={key!r})",
        )

    if "-" not in key:
        raise validation_error(
            f"{key!r} is not a valid issue key",
            attempted=f"get_issue(key={key!r})",
            remediation="Keys look like 'AUTH-77'. Use search_issues to find one.",
        )

    issue = store.get(key)
    if issue is None:
        raise validation_error(
            f"no issue named {key}",
            attempted=f"get_issue(key={key!r})",
            remediation="Use search_issues or find_issues_for_path to find a valid key.",
        )
    return issue.detail()


def list_sprint_board(args: dict[str, Any]) -> dict[str, Any]:
    _maybe_simulate("list_sprint_board", args)
    sprint = args.get("sprint")
    if sprint is not None and not isinstance(sprint, str):
        raise validation_error(
            "sprint must be a string like '2026-S18'",
            attempted=f"list_sprint_board(sprint={sprint!r})",
        )

    board = store.sprint_board(sprint)
    if not any(board.values()):
        raise validation_error(
            f"sprint {sprint!r} has no issues; it may not exist",
            attempted=f"list_sprint_board(sprint={sprint!r})",
            remediation="Omit `sprint` for the current sprint, or check issues://catalog.",
        )
    return {
        "sprint": sprint or store.catalog()["currentSprint"],
        "columns": {status: [i.summary() for i in issues] for status, issues in board.items()},
        "counts": {status: len(issues) for status, issues in board.items()},
    }


HANDLERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "search_issues": search_issues,
    "find_issues_for_path": find_issues_for_path,
    "get_issue": get_issue,
    "list_sprint_board": list_sprint_board,
}


def call(tool: str, args: dict[str, Any]) -> dict[str, Any]:
    handler = HANDLERS.get(tool)
    if handler is None:
        raise validation_error(
            f"no such tool {tool!r}",
            attempted=f"call({tool!r})",
            remediation=f"Available tools: {', '.join(HANDLERS)}.",
        )
    return handler(args)
