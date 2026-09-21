"""MCP resources.

Resources answer the questions an agent would otherwise discover by probing:
which projects exist, what statuses are in use, which source paths the tracker
knows about. Serving that as a resource the client can read up front is what
removes the exploratory round trips; `resource_eval.py` measures the effect.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from . import store
from .toolspecs import DESCRIPTIONS_V2, SCHEMAS


def catalog_json() -> str:
    return json.dumps(store.catalog(), indent=2)


def paths_json() -> str:
    """The exact path strings find_issues_for_path will match."""
    return json.dumps(
        {
            "note": (
                "These are the only paths linked in the tracker. Pass one of "
                "them verbatim to find_issues_for_path; bare filenames do not "
                "match."
            ),
            "paths": store.known_paths(),
        },
        indent=2,
    )


def tool_guide_markdown() -> str:
    """The tool contract as prose, for clients that read resources but cannot
    see full tool descriptions."""
    lines = ["# Issue tracker tool guide", ""]
    for name, description in DESCRIPTIONS_V2.items():
        required = ", ".join(SCHEMAS[name].get("required", [])) or "none"
        lines += [f"## {name}", "", f"Required arguments: {required}", "", description, ""]
    return "\n".join(lines)


RESOURCES: dict[str, dict[str, Any]] = {
    "issues://catalog": {
        "name": "tracker_catalog",
        "description": (
            "Shape of the tracker in one read: projects with lead, repo path and "
            "issue counts, the current sprint id, and every status, priority, "
            "label, assignee and linked path in use. Read this before searching "
            "-- it removes the need to probe for valid filter values."
        ),
        "mime_type": "application/json",
        "loader": catalog_json,
    },
    "issues://paths": {
        "name": "linked_paths",
        "description": (
            "Every source path the tracker links to issues, in the exact form "
            "find_issues_for_path expects."
        ),
        "mime_type": "application/json",
        "loader": paths_json,
    },
    "issues://tool-guide": {
        "name": "tool_guide",
        "description": "When to use each tracker tool, including the boundaries between them.",
        "mime_type": "text/markdown",
        "loader": tool_guide_markdown,
    },
}


def read(uri: str) -> str:
    entry = RESOURCES.get(uri)
    if entry is None:
        raise KeyError(f"no resource at {uri!r}; known: {', '.join(RESOURCES)}")
    loader: Callable[[], str] = entry["loader"]
    return loader()
