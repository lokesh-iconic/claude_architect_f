"""A personal scratchpad MCP server, for registering at USER scope.

This is deliberately a different kind of server from the issue tracker: it is
yours, it follows you across every project, and nobody else should inherit
it. That is the whole point of user scope -- see user_scope.example.json.

Register it with:

    claude mcp add --scope user scratchpad -- uv run python personal_scratchpad.py

Notes live in SCRATCHPAD_DIR (default ~/.claude-scratchpad) as plain .md
files, so they outlive any single session.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

SCRATCHPAD_DIR = Path(
    os.getenv("SCRATCHPAD_DIR") or (Path.home() / ".claude-scratchpad")
).expanduser()

SAFE_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def _note_path(slug: str) -> Path:
    if not SAFE_SLUG.match(slug):
        raise ToolError(
            f"{slug!r} is not a valid note name. Use lowercase letters, digits "
            "and hyphens, 1-64 characters."
        )
    # Belt and braces: the slug pattern already forbids separators.
    path = (SCRATCHPAD_DIR / f"{slug}.md").resolve()
    if path.parent != SCRATCHPAD_DIR.resolve():
        raise ToolError(f"{slug!r} resolves outside the scratchpad directory.")
    return path


def build_server() -> MCPServer:
    server = MCPServer(
        name="scratchpad",
        version="1.0.0",
        instructions=(
            "A personal, cross-project note store. Notes are named by slug and "
            "persist on disk. Use it for things worth keeping between sessions; "
            "it is not shared with anyone else."
        ),
        log_level=os.getenv("SCRATCHPAD_LOG_LEVEL", "INFO"),  # type: ignore[arg-type]
    )

    @server.tool(
        name="append_note",
        description=(
            "Append a timestamped entry to one of your personal notes, creating "
            "it if it does not exist. Input: `slug` names the note "
            "(lowercase, hyphens, e.g. 'mcp-gotchas'); `text` is the entry. "
            "Returns the note's path and its new size."
        ),
    )
    def append_note(
        slug: Annotated[str, Field(description="Note name: lowercase, digits, hyphens.")],
        text: Annotated[str, Field(description="The entry to append.", min_length=1)],
    ) -> dict[str, Any]:
        path = _note_path(slug)
        SCRATCHPAD_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"\n## {stamp}\n\n{text.strip()}\n")
        return {"slug": slug, "path": str(path), "bytes": path.stat().st_size}

    @server.tool(
        name="read_note",
        description=(
            "Read one personal note in full by its slug. Use list_notes first "
            "if you do not know the slug."
        ),
    )
    def read_note(
        slug: Annotated[str, Field(description="Note name, as shown by list_notes.")],
    ) -> dict[str, Any]:
        path = _note_path(slug)
        if not path.is_file():
            raise ToolError(f"no note named {slug!r}. Call list_notes to see what exists.")
        return {"slug": slug, "content": path.read_text(encoding="utf-8")}

    @server.tool(
        name="list_notes",
        description="List every personal note with its size and last-modified time.",
    )
    def list_notes() -> dict[str, Any]:
        if not SCRATCHPAD_DIR.is_dir():
            return {"directory": str(SCRATCHPAD_DIR), "notes": []}
        notes = [
            {
                "slug": p.stem,
                "bytes": p.stat().st_size,
                "modified": datetime.fromtimestamp(
                    p.stat().st_mtime, tz=timezone.utc
                ).isoformat(timespec="seconds"),
            }
            for p in sorted(SCRATCHPAD_DIR.glob("*.md"))
        ]
        return {"directory": str(SCRATCHPAD_DIR), "notes": notes}

    return server


if __name__ == "__main__":
    build_server().run("stdio")
