"""Configuration and live/mock resolution for the evaluation harnesses.

The MCP server itself needs no API key -- it is a plain tool server. The key
only matters for the harnesses that ask a model to choose between tools.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

Mode = Literal["live", "mock"]

PACKAGE_DIR = Path(__file__).resolve().parent
MODULE_DIR = PACKAGE_DIR.parent
PROJECT_ROOT = MODULE_DIR.parent
OUTPUT_DIR = MODULE_DIR / "output"


@dataclass
class Settings:
    mode: Mode
    mode_reason: str
    api_key: str | None = None
    model: str = "claude-opus-5"
    effort: str = "low"
    max_tokens: int = 2048

    @property
    def is_live(self) -> bool:
        return self.mode == "live"


def load_settings(
    requested_mode: str = "auto",
    *,
    env_file: Path | None = None,
    validate: bool = True,
) -> Settings:
    dotenv_path = env_file or (PROJECT_ROOT / ".env")
    load_dotenv(dotenv_path, override=False)

    key = (os.getenv("ANTHROPIC_API_KEY") or "").strip()
    model = os.getenv("MCP_EVAL_MODEL", "claude-opus-5")
    base = Settings(mode="mock", mode_reason="not resolved", api_key=key or None, model=model)

    if requested_mode == "mock":
        base.mode_reason = "forced by --mode mock"
        return base

    if not key:
        if requested_mode == "live":
            raise SystemExit(
                "--mode live needs ANTHROPIC_API_KEY.\n"
                f"Set it in {dotenv_path}, or drop --mode live to use the offline proxy."
            )
        base.mode_reason = f"no ANTHROPIC_API_KEY in {dotenv_path}"
        return base

    if not validate:
        base.mode, base.mode_reason = "live", "key present (validation skipped)"
        return base

    ok, reason = _validate_key(key, model)
    if ok:
        base.mode, base.mode_reason = "live", f"validated against {model}"
        return base
    if requested_mode == "live":
        raise SystemExit(f"--mode live was requested but the key check failed: {reason}")
    base.mode_reason = f"falling back to mock: {reason}"
    return base


def _validate_key(key: str, model: str) -> tuple[bool, str]:
    """Zero-token credential check via GET /v1/models/{id}."""
    try:
        import anthropic
    except ImportError:  # pragma: no cover
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
