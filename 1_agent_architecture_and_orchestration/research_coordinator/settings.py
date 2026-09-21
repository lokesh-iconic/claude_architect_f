"""Configuration and live/mock mode resolution.

The single source of truth for *how* the system talks to Claude. Everything
else in the package takes a `Settings` object and never reads the environment
itself, so tests can construct a Settings directly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

Mode = Literal["live", "mock"]

# <repo root>/1_agent_architecture_and_orchestration/research_coordinator/settings.py
PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PACKAGE_DIR.parent
REPO_ROOT = PROJECT_DIR.parent

DEFAULT_CORPUS = PACKAGE_DIR / "corpus"


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass
class Settings:
    """Resolved runtime configuration."""

    mode: Mode
    mode_reason: str
    api_key: str | None = None
    coordinator_model: str = "claude-opus-5"
    subagent_model: str = "claude-opus-5"
    coordinator_effort: str = "high"
    subagent_effort: str = "medium"
    max_tokens: int = 16000
    enable_web_search: bool = True
    subagent_timeout_s: float = 120.0
    max_rounds: int = 4
    corpus_dir: Path = field(default_factory=lambda: DEFAULT_CORPUS)

    @property
    def is_live(self) -> bool:
        return self.mode == "live"


def load_settings(
    requested_mode: str = "auto",
    corpus_dir: Path | None = None,
    *,
    env_file: Path | None = None,
    validate: bool = True,
) -> Settings:
    """Load `.env` from the repo root and decide whether we can run live.

    `requested_mode` is one of "auto" (default), "live", or "mock".

    In "auto" mode the key is validated with a `GET /v1/models/{id}` call: it
    authenticates and confirms the model id without spending a single token.
    Any authentication, permission, or connectivity failure falls back to mock
    rather than crashing the run.
    """
    dotenv_path = env_file or (REPO_ROOT / ".env")
    load_dotenv(dotenv_path, override=False)

    key = (os.getenv("ANTHROPIC_API_KEY") or "").strip()
    base = Settings(
        mode="mock",
        mode_reason="not resolved",
        api_key=key or None,
        coordinator_model=os.getenv("RC_MODEL_COORDINATOR", "claude-opus-5"),
        subagent_model=os.getenv("RC_MODEL_SUBAGENT", "claude-opus-5"),
        coordinator_effort=os.getenv("RC_EFFORT_COORDINATOR", "high"),
        subagent_effort=os.getenv("RC_EFFORT_SUBAGENT", "medium"),
        enable_web_search=_env_flag("RC_ENABLE_WEB_SEARCH", True),
        subagent_timeout_s=float(_env_int("RC_SUBAGENT_TIMEOUT", 120)),
        max_rounds=_env_int("RC_MAX_ROUNDS", 4),
        corpus_dir=Path(corpus_dir) if corpus_dir else DEFAULT_CORPUS,
    )

    if requested_mode == "mock":
        base.mode = "mock"
        base.mode_reason = "forced by --mode mock"
        return base

    if not key:
        if requested_mode == "live":
            raise SystemExit(
                "--mode live was requested but ANTHROPIC_API_KEY is empty.\n"
                f"Set it in {dotenv_path} or drop --mode live to run in mock mode."
            )
        base.mode = "mock"
        base.mode_reason = f"no ANTHROPIC_API_KEY found in {dotenv_path}"
        return base

    if not validate:
        base.mode = "live"
        base.mode_reason = "key present (validation skipped)"
        return base

    ok, reason = _validate_key(key, base.coordinator_model)
    if ok:
        base.mode = "live"
        base.mode_reason = f"validated against {base.coordinator_model}"
        return base

    if requested_mode == "live":
        raise SystemExit(f"--mode live was requested but the key check failed: {reason}")

    base.mode = "mock"
    base.mode_reason = f"falling back to mock: {reason}"
    return base


def _validate_key(key: str, model: str) -> tuple[bool, str]:
    """Zero-token credential check. Returns (ok, human-readable reason)."""
    try:
        import anthropic
    except ImportError:  # pragma: no cover - dependency is declared
        return False, "the `anthropic` package is not installed"

    client = anthropic.Anthropic(api_key=key, max_retries=1, timeout=15.0)
    try:
        client.models.retrieve(model)
        return True, "ok"
    except anthropic.AuthenticationError:
        return False, "ANTHROPIC_API_KEY was rejected (401 invalid key)"
    except anthropic.PermissionDeniedError:
        return False, "the API key lacks permission for this model (403)"
    except anthropic.NotFoundError:
        return False, f"model {model!r} is not available to this key (404)"
    except anthropic.RateLimitError:
        # The key is valid - it is just busy. Live mode is still the right call.
        return True, "ok (rate limited during check)"
    except anthropic.APIConnectionError:
        return False, "could not reach the Claude API (offline or blocked)"
    except anthropic.APIStatusError as exc:
        return False, f"unexpected API status {exc.status_code} during key check"
