"""stdio entry point for the issue-tracker MCP server.

This is the command `.mcp.json` runs. Keep it import-light: anything printed
to stdout that is not JSON-RPC will break the transport.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from issue_tracker.mcp_server import main  # noqa: E402

if __name__ == "__main__":
    main()
