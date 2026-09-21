"""Config inspection for the two MCP scopes.

Checks the things that actually break: unresolved `${VAR}` placeholders in the
project config, and whether the personal server has been registered at user
scope rather than pasted into the committed project file.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .settings import MODULE_DIR

PROJECT_CONFIG = MODULE_DIR / ".mcp.json"
USER_EXAMPLE = MODULE_DIR / "user_scope.example.json"

# ${VAR} and ${VAR:-default}
PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


@dataclass
class EnvVar:
    name: str
    raw: str
    default: str | None
    resolved: str | None
    has_default: bool

    @property
    def status(self) -> str:
        if self.resolved is not None:
            return "set in environment"
        if self.has_default:
            return f"unset, falls back to {self.default!r}"
        return "UNSET and no default"

    @property
    def blocking(self) -> bool:
        return self.resolved is None and not self.has_default


# Where uv lands when it is installed but not added to PATH.
UV_FALLBACK_DIRS = (
    Path.home() / ".local" / "bin",
    Path.home() / ".cargo" / "bin",
)


def resolve_command(command: str) -> tuple[str | None, str | None]:
    """Find `command` the way a spawning client would.

    Returns (resolved path, hint). A config whose command does not resolve on
    PATH fails at spawn time with a bare FileNotFoundError, and the client
    just reports the server as failed -- so this is checked explicitly rather
    than discovered in a session.
    """
    found = shutil.which(command)
    if found:
        return found, None

    for directory in UV_FALLBACK_DIRS:
        for name in (command, f"{command}.exe"):
            candidate = directory / name
            if candidate.is_file():
                return None, (
                    f"{command!r} is installed at {candidate} but that "
                    f"directory is not on PATH. Add {directory} to PATH, or "
                    f"set \"command\" to the absolute path."
                )
    return None, f"{command!r} was not found on PATH and no fallback install was located."


@dataclass
class ServerConfig:
    scope: str
    name: str
    command: str
    args: list[str]
    env_vars: list[EnvVar] = field(default_factory=list)

    @property
    def blocking_vars(self) -> list[EnvVar]:
        return [v for v in self.env_vars if v.blocking]

    @property
    def resolved_command(self) -> str | None:
        return resolve_command(self.command)[0]

    @property
    def command_hint(self) -> str | None:
        return resolve_command(self.command)[1]

    @property
    def spawnable(self) -> bool:
        return self.resolved_command is not None


def _expand(value: str) -> list[EnvVar]:
    found = []
    for match in PLACEHOLDER.finditer(value):
        name, default = match.group(1), match.group(2)
        found.append(
            EnvVar(
                name=name,
                raw=match.group(0),
                default=default,
                # An empty value is unset for our purposes: an empty
                # credential fails exactly like a missing one.
                resolved=os.getenv(name) or None,
                has_default=default is not None,
            )
        )
    return found


def _read(path: Path, scope: str) -> list[ServerConfig]:
    if not path.is_file():
        return []
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    servers = []
    for name, spec in (data.get("mcpServers") or {}).items():
        env_vars: list[EnvVar] = []
        for value in (spec.get("env") or {}).values():
            env_vars.extend(_expand(str(value)))
        servers.append(
            ServerConfig(
                scope=scope,
                name=name,
                command=str(spec.get("command", "")),
                args=[str(a) for a in spec.get("args", [])],
                env_vars=env_vars,
            )
        )
    return servers


def project_servers() -> list[ServerConfig]:
    return _read(PROJECT_CONFIG, "project")


def user_scope_servers() -> list[ServerConfig]:
    """The personal server is only an *example* here. It becomes real when
    registered at user scope with `claude mcp add --scope user`, which writes
    to the user's own settings, not to this repo."""
    return _read(USER_EXAMPLE, "user (example)")


def secrets_are_inlined() -> list[str]:
    """Credentials must arrive by expansion, never as literals in the file."""
    if not PROJECT_CONFIG.is_file():
        return []
    offenders = []
    data = json.loads(PROJECT_CONFIG.read_text(encoding="utf-8"))
    for name, spec in (data.get("mcpServers") or {}).items():
        for key, value in (spec.get("env") or {}).items():
            looks_secret = any(w in key.upper() for w in ("TOKEN", "KEY", "SECRET", "PASSWORD"))
            if looks_secret and "${" not in str(value) and str(value).strip():
                offenders.append(f"{name}.env.{key}")
    return offenders


def summarize() -> dict[str, Any]:
    project = project_servers()
    user = user_scope_servers()
    return {
        "projectConfig": str(PROJECT_CONFIG),
        "projectConfigExists": PROJECT_CONFIG.is_file(),
        "userScopeExample": str(USER_EXAMPLE),
        "servers": [
            {
                "scope": s.scope,
                "name": s.name,
                "command": " ".join([s.command, *s.args]),
                "spawnable": s.spawnable,
                "resolvedCommand": s.resolved_command,
                "commandHint": s.command_hint,
                "env": [
                    {"name": v.name, "status": v.status, "blocking": v.blocking}
                    for v in s.env_vars
                ],
            }
            for s in project + user
        ],
        "unspawnableServers": [
            {"name": s.name, "scope": s.scope, "command": s.command, "hint": s.command_hint}
            for s in project + user
            if not s.spawnable
        ],
        "blockingVars": sorted(
            {v.name for s in project for v in s.blocking_vars}
        ),
        "inlinedSecrets": secrets_are_inlined(),
        "registerUserScope": (
            "claude mcp add --scope user scratchpad -- uv run python personal_scratchpad.py"
        ),
        "confirmBothScopes": "Run /mcp inside Claude Code; both servers should be listed.",
    }
