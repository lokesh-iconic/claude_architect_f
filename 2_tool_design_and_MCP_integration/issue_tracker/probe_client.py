"""Talks to the server the way a real MCP client does.

Everything here goes over stdio to a separately spawned process, so what it
reports is what Claude Code would receive -- not what an in-process call
happens to return.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp import Client, StdioServerParameters

from .errors import parse_error_payload
from .settings import MODULE_DIR

SERVER_SCRIPT = MODULE_DIR / "server.py"


def server_params(extra_env: dict[str, str] | None = None) -> StdioServerParameters:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "TRACKER_LOG_LEVEL": "CRITICAL"}
    env.update(extra_env or {})
    return StdioServerParameters(
        command=sys.executable, args=[str(SERVER_SCRIPT)], env=env
    )


@dataclass
class Capabilities:
    tools: list[dict[str, Any]]
    resources: list[dict[str, str]]
    instructions: str


async def describe() -> Capabilities:
    async with Client(server_params()) as client:
        listed = await client.list_tools()
        resources = await client.list_resources()
        return Capabilities(
            tools=[
                {
                    "name": t.name,
                    "description": t.description or "",
                    "required": list((t.input_schema or {}).get("required", [])),
                    "properties": sorted((t.input_schema or {}).get("properties", {})),
                }
                for t in listed.tools
            ],
            resources=[
                {"uri": str(r.uri), "name": r.name or "", "description": r.description or ""}
                for r in resources.resources
            ],
            instructions=(await _instructions(client)),
        )


async def _instructions(client: Client) -> str:
    value = getattr(client, "instructions", "")
    return value if isinstance(value, str) else ""


@dataclass
class ErrorProbe:
    label: str
    tool: str
    args: dict[str, Any]
    is_error: bool
    payload: dict[str, Any] | None
    raw: str


async def probe_errors(
    probes: list[tuple[str, str, dict[str, Any], dict[str, str]]],
) -> list[ErrorProbe]:
    """Run each probe against a freshly spawned server, so a probe that needs
    TRACKER_SIMULATE set does not leak into the others."""
    out: list[ErrorProbe] = []
    for label, tool, args, env in probes:
        async with Client(server_params(env)) as client:
            result = await client.call_tool(tool, args)
            raw = result.content[0].text if result.content else ""
            payload: dict[str, Any] | None
            try:
                payload = parse_error_payload(raw)
            except ValueError:
                payload = None
            out.append(
                ErrorProbe(
                    label=label,
                    tool=tool,
                    args=args,
                    is_error=bool(result.is_error),
                    payload=payload,
                    raw=raw,
                )
            )
    return out


async def read_resource(uri: str) -> str:
    async with Client(server_params()) as client:
        got = await client.read_resource(uri)
        return got.contents[0].text


def server_script_exists() -> bool:
    return Path(SERVER_SCRIPT).is_file()
