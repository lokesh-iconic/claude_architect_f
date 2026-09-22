"""Local stand-in for "what did the prior CI run already post", used in mock
mode and local dry-runs so the dedup behaviour (self-check #4) is
demonstrable without GitHub at all. In live mode with --repo/--pr, GitHub's
own PR comments are the source of truth instead (see review/github_client.py).
"""

from __future__ import annotations

import json
from pathlib import Path


def load_state(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    return set(json.loads(path.read_text(encoding="utf-8")))


def save_state(path: Path, fingerprints: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(fingerprints), indent=2), encoding="utf-8")
