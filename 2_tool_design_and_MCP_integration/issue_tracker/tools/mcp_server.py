"""MCP server wiring.

Thin adapter: the MCP layer owns transport and schema generation, every tool
body delegates to `handlers.py`.

Two details are load-bearing, and both were established by probing the SDK
rather than assumed:

* Argument types are `Annotated[..., Field(description=...)]`. The generated
  JSON schema carries those descriptions and constraints, and the SDK
  validates against them before the handler runs -- so a bad `limit` comes
  back as a schema error instead of reaching our code.
* Failures raise the SDK's `ToolError`. Any other exception is swallowed and
  replaced with the string "Error executing tool <name>", which destroys the
  structured payload. `ToolError` passes its message through intact.
"""

from __future__ import annotations

import os
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from . import handlers, resources
from ..core.errors import TrackerError
from .toolspecs import DESCRIPTIONS_V2

SERVER_INSTRUCTIONS = """\
Issue tracker for the sample_service codebase.

Read the issues://catalog resource before your first query: it lists every
project, status, label and linked source path, which saves probing for valid
filter values.

Two tools return issue lists and are easy to confuse. search_issues matches
issue prose; find_issues_for_path matches the tracker's structured path index.
If the user named a file or directory, use find_issues_for_path -- issues
rarely mention their own paths, so a text search will miss them.

Every failure comes back as JSON with errorCategory (transient, validation, or
permission) and isRetryable. Retry only when isRetryable is true.
"""


def _run(tool: str, args: dict[str, Any]) -> dict[str, Any]:
    try:
        return handlers.call(tool, args)
    except TrackerError as exc:
        # ToolError specifically: its message reaches the client verbatim.
        raise ToolError(exc.to_json()) from exc


def build_server() -> MCPServer:
    server = MCPServer(
        name="issue-tracker",
        version="1.0.0",
        instructions=SERVER_INSTRUCTIONS,
        # Expected tool failures are logged at ERROR by the SDK. The error
        # probe deliberately triggers six of them, so it raises this to
        # CRITICAL rather than drowning its own output.
        log_level=os.getenv("TRACKER_LOG_LEVEL", "INFO"),  # type: ignore[arg-type]
    )

    @server.tool(name="search_issues", description=DESCRIPTIONS_V2["search_issues"])
    def search_issues(
        query: Annotated[
            str,
            Field(
                description=(
                    "Free text to match against issue titles, descriptions and "
                    "labels. Not a file path and not an issue key."
                )
            ),
        ],
        project: Annotated[
            Literal["CHK", "AUTH", "PLAT"] | None,
            Field(description="Optional project key filter."),
        ] = None,
        status: Annotated[
            Literal["open", "in_progress", "in_review", "closed"] | None,
            Field(description="Optional status filter."),
        ] = None,
        limit: Annotated[
            int, Field(description="Maximum results to return.", ge=1, le=25)
        ] = 10,
    ) -> dict[str, Any]:
        return _run(
            "search_issues",
            {"query": query, "project": project, "status": status, "limit": limit},
        )

    @server.tool(
        name="find_issues_for_path", description=DESCRIPTIONS_V2["find_issues_for_path"]
    )
    def find_issues_for_path(
        path: Annotated[
            str,
            Field(
                description=(
                    "Repo-relative file or directory path with forward slashes "
                    "and no leading './', e.g. 'sample_service/auth/session.py'. "
                    "A bare filename will not match. See issues://paths."
                )
            ),
        ],
        include_closed: Annotated[
            bool, Field(description="Include closed issues in the result.")
        ] = False,
    ) -> dict[str, Any]:
        return _run("find_issues_for_path", {"path": path, "include_closed": include_closed})

    @server.tool(name="get_issue", description=DESCRIPTIONS_V2["get_issue"])
    def get_issue(
        key: Annotated[
            str,
            Field(
                description="Issue key in PROJECT-NUMBER form, e.g. 'AUTH-77'.",
                pattern=r"^[A-Za-z]+-\d+$",
            ),
        ],
    ) -> dict[str, Any]:
        return _run("get_issue", {"key": key})

    @server.tool(name="list_sprint_board", description=DESCRIPTIONS_V2["list_sprint_board"])
    def list_sprint_board(
        sprint: Annotated[
            str | None,
            Field(description="Sprint id like '2026-S18'. Omit for the current sprint."),
        ] = None,
    ) -> dict[str, Any]:
        return _run("list_sprint_board", {"sprint": sprint})

    for uri, spec in resources.RESOURCES.items():
        _register_resource(server, uri, spec)

    return server


def _register_resource(server: MCPServer, uri: str, spec: dict[str, Any]) -> None:
    loader = spec["loader"]

    @server.resource(
        uri, name=spec["name"], description=spec["description"], mime_type=spec["mime_type"]
    )
    def _read() -> str:
        return loader()


def main() -> None:
    build_server().run("stdio")
