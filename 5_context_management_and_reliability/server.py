"""stdio entry point for the support-desk MCP server.

Keep it import-light: anything printed to stdout that is not JSON-RPC breaks
the transport.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support_agent.tools.mcp_server import main  # noqa: E402

if __name__ == "__main__":
    main()
