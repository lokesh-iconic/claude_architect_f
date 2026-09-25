"""Configuration and live/mock resolution.

Same contract as the other modules: `--mode mock` is always honoured, `auto`
validates the key with a zero-token `GET /v1/models/{id}` and falls back to
mock with the reason printed, and `--mode live` turns that fallback into a
hard error. Nothing else in the package reads the environment.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

Mode = Literal["live", "mock"]

PACKAGE_DIR = Path(__file__).resolve().parents[1]  # this file lives in <package>/config/
MODULE_DIR = PACKAGE_DIR.parent
PROJECT_ROOT = MODULE_DIR.parent


def _env_float(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    return int(_env_float(name, float(default)))


def _env_flag(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    return default if not raw else raw in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    mode: Mode
    mode_reason: str
    api_key: str | None = None
    model: str = "claude-opus-5"
    effort: str = "medium"
    max_tokens: int = 16000
    request_timeout_s: float = 120.0
    refusal_fallback: bool = True
    tool_timeout_s: float = 3.0
    keep_recent_turns: int = 4
    max_tool_rounds: int = 8

    @property
    def is_live(self) -> bool:
        return self.mode == "live"


def load_settings(
    requested_mode: str = "auto",
    *,
    env_file: Path | None = None,
    validate: bool = True,
) -> Settings:
    if requested_mode not in {"auto", "live", "mock"}:
        raise ValueError(f"unknown mode: {requested_mode!r}")

    dotenv_path = env_file or (PROJECT_ROOT / ".env")
    load_dotenv(dotenv_path, override=False)

    key = (os.getenv("ANTHROPIC_API_KEY") or "").strip()
    base = Settings(
        mode="mock",
        mode_reason="not resolved",
        api_key=key or None,
        model=os.getenv("SA_MODEL", "claude-opus-5"),
        effort=os.getenv("SA_EFFORT", "medium"),
        request_timeout_s=_env_float("SA_REQUEST_TIMEOUT", 120.0),
        refusal_fallback=_env_flag("SA_REFUSAL_FALLBACK", True),
        tool_timeout_s=_env_float("SA_TOOL_TIMEOUT", 3.0),
        keep_recent_turns=_env_int("SA_KEEP_RECENT_TURNS", 4),
        max_tool_rounds=_env_int("SA_MAX_TOOL_ROUNDS", 8),
    )

    if requested_mode == "mock":
        base.mode_reason = "forced by --mode mock"
        return base

    if not key:
        if requested_mode == "live":
            raise SystemExit(
                "--mode live needs ANTHROPIC_API_KEY.\n"
                "Set it in the project .env, or drop --mode live to run the mock backend."
            )
        base.mode_reason = "no ANTHROPIC_API_KEY in the environment or the project .env"
        return base

    if not validate:
        base.mode, base.mode_reason = "live", "key present (validation skipped)"
        return base

    ok, reason = _validate_key(key, base.model)
    if ok:
        base.mode, base.mode_reason = "live", f"validated against {base.model}"
        return base
    if requested_mode == "live":
        raise SystemExit(f"--mode live was requested but the key check failed: {reason}")
    base.mode_reason = f"falling back to mock: {reason}"
    return base


def _validate_key(key: str, model: str) -> tuple[bool, str]:
    """Zero-token credential check via GET /v1/models/{id}."""
    try:
        import anthropic
    except ImportError:
        return False, "the `anthropic` package is not installed"

    client = anthropic.Anthropic(api_key=key, max_retries=1, timeout=15.0)
    try:
        client.models.retrieve(model)
        return True, "ok"
    except anthropic.AuthenticationError:
        return False, "ANTHROPIC_API_KEY was rejected (401)"
    except anthropic.PermissionDeniedError:
        return False, "the key lacks permission for this model (403)"
    except anthropic.NotFoundError:
        return False, f"model {model!r} is not available to this key (404)"
    except anthropic.RateLimitError:
        return True, "ok (rate limited during check)"
    except anthropic.APIConnectionError:
        return False, "could not reach the Claude API (offline or blocked)"
    except anthropic.APIStatusError as exc:
        return False, f"unexpected API status {exc.status_code} during key check"
