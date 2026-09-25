"""Live/mock mode resolution for the CI review CLI.

Mirrors the pattern in modules 1 and 2: `--mode mock` is always honoured,
`auto` falls back to mock and prints why, `--mode live` turns that fallback
into a hard error. Here "live" means the `claude` CLI binary itself, not the
`anthropic` Python SDK -- this module shells out to Claude Code, per the
brief.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass

DEFAULT_MODEL = os.getenv("RC_REVIEW_MODEL", "claude-opus-5")
CLAUDE_BINARY = os.getenv("CLAUDE_CODE_BIN", "claude")


@dataclass(frozen=True)
class ReviewSettings:
    mode: str  # "live" or "mock"
    mode_reason: str
    claude_binary: str
    model: str

    @property
    def is_live(self) -> bool:
        return self.mode == "live"


def _claude_availability() -> tuple[bool, str]:
    if shutil.which(CLAUDE_BINARY) is None:
        return False, f"{CLAUDE_BINARY!r} not found on PATH"
    if not (os.getenv("ANTHROPIC_API_KEY") or os.getenv("CLAUDE_CODE_OAUTH_TOKEN")):
        return False, "neither ANTHROPIC_API_KEY nor CLAUDE_CODE_OAUTH_TOKEN is set"
    return True, f"{CLAUDE_BINARY!r} on PATH, credentials present"


def load_settings(requested_mode: str = "auto") -> ReviewSettings:
    if requested_mode == "mock":
        return ReviewSettings("mock", "--mode mock forced", CLAUDE_BINARY, DEFAULT_MODEL)

    available, reason = _claude_availability()

    if requested_mode == "live":
        if not available:
            raise RuntimeError(f"--mode live requested but unavailable: {reason}")
        return ReviewSettings("live", reason, CLAUDE_BINARY, DEFAULT_MODEL)

    if requested_mode != "auto":
        raise ValueError(f"unknown mode: {requested_mode!r}")

    if available:
        return ReviewSettings("live", reason, CLAUDE_BINARY, DEFAULT_MODEL)
    return ReviewSettings("mock", f"auto fallback ({reason})", CLAUDE_BINARY, DEFAULT_MODEL)
