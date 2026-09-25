"""Git diff access. Local, offline -- no network involved in a `git diff`
against a ref already fetched into the working copy."""

from __future__ import annotations

import subprocess
from pathlib import Path


class DiffError(RuntimeError):
    pass


def _git(args: list[str], cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False,
        # Diffs are UTF-8; the platform default (cp1252 on Windows) fails on them and leaves stdout None.
        encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        raise DiffError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def get_diff(base_ref: str, head_ref: str = "HEAD", cwd: Path | None = None) -> str:
    """Unified diff of `head_ref` against the merge base with `base_ref`."""
    return _git(["diff", f"{base_ref}...{head_ref}"], cwd or Path.cwd())


def changed_files(base_ref: str, head_ref: str = "HEAD", cwd: Path | None = None) -> list[str]:
    out = _git(["diff", "--name-only", f"{base_ref}...{head_ref}"], cwd or Path.cwd())
    return [line for line in out.splitlines() if line]
