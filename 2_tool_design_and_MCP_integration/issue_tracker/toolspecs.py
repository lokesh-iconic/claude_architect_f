"""Tool interfaces, in two description generations.

`search_issues` and `find_issues_for_path` overlap on purpose: both return
lists of issues, and "which issues involve the pricing code?" is a plausible
request for either. v1 describes them the way most tool servers do -- a short
sentence each -- and the selection eval shows that failing. v2 adds input
formats, example queries, and explicit negative boundaries pointing at the
sibling tool.

The schemas are identical across versions, so the eval isolates exactly one
variable: the prose.
"""

from __future__ import annotations

from typing import Any

# --------------------------------------------------------------------------
# Schemas (shared by both description generations)
# --------------------------------------------------------------------------

SCHEMAS: dict[str, dict[str, Any]] = {
    "search_issues": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Words to look for in issue text."},
            "project": {
                "type": "string",
                "description": "Optional project key filter: CHK, AUTH, or PLAT.",
            },
            "status": {
                "type": "string",
                "enum": ["open", "in_progress", "in_review", "closed"],
                "description": "Optional status filter.",
            },
            "limit": {"type": "integer", "description": "Max results, 1-25. Default 10."},
        },
        "required": ["query"],
        "additionalProperties": False,
    },
    "find_issues_for_path": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "Repo-relative file or directory path, forward slashes, no "
                    "leading './' -- e.g. 'sample_service/auth/session.py' or "
                    "'sample_service/checkout'."
                ),
            },
            "include_closed": {
                "type": "boolean",
                "description": "Include closed issues. Default false.",
            },
        },
        "required": ["path"],
        "additionalProperties": False,
    },
    "get_issue": {
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "description": "Issue key in PROJECT-NUMBER form, e.g. 'AUTH-77'.",
            }
        },
        "required": ["key"],
        "additionalProperties": False,
    },
    "list_sprint_board": {
        "type": "object",
        "properties": {
            "sprint": {
                "type": "string",
                "description": "Sprint id like '2026-S18'. Defaults to the current sprint.",
            }
        },
        "required": [],
        "additionalProperties": False,
    },
}

TOOL_NAMES = tuple(SCHEMAS)

# --------------------------------------------------------------------------
# v1 -- the descriptions this exercise starts with. Deliberately thin.
# --------------------------------------------------------------------------

DESCRIPTIONS_V1: dict[str, str] = {
    "search_issues": "Search the issue tracker and return issues that match.",
    "find_issues_for_path": "Find issues related to a file in the codebase.",
    "get_issue": "Get an issue from the tracker.",
    "list_sprint_board": "List the issues on the sprint board.",
}

# --------------------------------------------------------------------------
# v2 -- rewritten until selection was reliable.
# --------------------------------------------------------------------------

DESCRIPTIONS_V2: dict[str, str] = {
    "search_issues": (
        "Full-text search over issue TITLES, DESCRIPTIONS, and LABELS. Use this "
        "when the user describes a problem, symptom, or theme in their own words "
        "and does not name a specific source file.\n"
        "\n"
        "Input: `query` is free text, not a path and not an issue key.\n"
        "Example queries: 'rounding errors on totals', 'rate limiting work', "
        "'open security bugs in AUTH'.\n"
        "Returns: compact issue summaries (key, title, status, priority, "
        "assignee). Call get_issue for the full description and comments.\n"
        "\n"
        "Do NOT use this to look up issues by source file or directory. Text "
        "search only matches words that appear in the issue prose, and most "
        "issues never mention their own file paths -- searching 'session.py' "
        "will miss issues that are genuinely linked to that file. Use "
        "find_issues_for_path instead, which reads the tracker's structured "
        "path links.\n"
        "Do NOT use this when the user already gave an issue key like "
        "'CHK-104'; use get_issue."
    ),
    "find_issues_for_path": (
        "Look up issues by the source paths LINKED to them in the tracker. Use "
        "this whenever the user names a file, directory, or module -- including "
        "when they ask what work is outstanding on some part of the codebase.\n"
        "\n"
        "Input: `path` is a repo-relative path with forward slashes and no "
        "leading './', e.g. 'sample_service/auth/session.py'. A directory path "
        "matches every issue linked to a file beneath it. This is a structured "
        "lookup on the tracker's path index, NOT a text search -- the path is "
        "matched exactly or by directory prefix, so a bare filename such as "
        "'session.py' will not match.\n"
        "Example queries: 'what's broken in sample_service/checkout', 'any open "
        "issues touching pricing.py', 'outstanding work on the auth module'.\n"
        "Returns: compact issue summaries, open ones only unless "
        "`include_closed` is true.\n"
        "\n"
        "Do NOT use this when the request names no path at all and is purely a "
        "topic or symptom; that is search_issues territory."
    ),
    "get_issue": (
        "Fetch ONE issue by its key and return the full record: description, "
        "reporter, sprint, labels, linked source paths, and every comment.\n"
        "\n"
        "Input: `key` in PROJECT-NUMBER form, e.g. 'AUTH-77'. Project keys are "
        "CHK, AUTH, and PLAT.\n"
        "Example queries: 'what is the story on CHK-104', 'pull up AUTH-77', "
        "'who commented on PLAT-31'.\n"
        "\n"
        "Do NOT use this to find issues -- it takes a key you already have. If "
        "you do not have a key, use search_issues or find_issues_for_path first. "
        "This is the only tool that returns comments and linked paths."
    ),
    "list_sprint_board": (
        "Return every issue in one sprint, grouped by status (open, "
        "in_progress, in_review, closed). Use this for questions about the "
        "sprint as a whole -- what is in flight, what is unstarted, how loaded "
        "the team is.\n"
        "\n"
        "Input: `sprint` like '2026-S18'; omit it for the current sprint.\n"
        "Example queries: 'what's on the board', 'what's still in review this "
        "sprint', 'how much is in progress'.\n"
        "\n"
        "Do NOT use this to search by topic or by file -- it returns the whole "
        "sprint unfiltered. Issues with no sprint assigned never appear here."
    ),
}

VERSIONS: dict[str, dict[str, str]] = {"v1": DESCRIPTIONS_V1, "v2": DESCRIPTIONS_V2}


def tool_definitions(version: str = "v2") -> list[dict[str, Any]]:
    """Anthropic Messages API tool definitions for the given description set."""
    descriptions = VERSIONS[version]
    return [
        {"name": name, "description": descriptions[name], "input_schema": SCHEMAS[name]}
        for name in TOOL_NAMES
    ]
