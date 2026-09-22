"""Self-check #1: does a fresh clone pick up CLAUDE.md with no manual step?

Checked here as: the file exists, and git tracks it (so a clone actually
receives it) rather than it only existing in this working tree. `tracked`
reads false until the file is `git add`-ed -- run this after staging, or
after the commit that ships this module.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

EXPECTED = (
    ("project (root)", REPO_ROOT / "CLAUDE.md"),
    (
        "directory (module 3)",
        REPO_ROOT / "3_claude_code_configuration_and_workflows" / "CLAUDE.md",
    ),
)


@dataclass(frozen=True)
class HierarchyEntry:
    scope: str
    path: Path
    exists: bool
    tracked: bool
    ignored: bool

    @property
    def ok(self) -> bool:
        return self.exists and self.tracked and not self.ignored


def _git(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=False
    )


def _is_tracked(rel: str) -> bool:
    return _git(["ls-files", "--error-unmatch", rel]).returncode == 0


def _is_ignored(rel: str) -> bool:
    return _git(["check-ignore", "-q", rel]).returncode == 0


def check_hierarchy() -> list[HierarchyEntry]:
    entries = []
    for scope, path in EXPECTED:
        exists = path.is_file()
        rel = str(path.relative_to(REPO_ROOT)).replace("\\", "/")
        entries.append(
            HierarchyEntry(
                scope=scope,
                path=path,
                exists=exists,
                tracked=_is_tracked(rel) if exists else False,
                ignored=_is_ignored(rel) if exists else False,
            )
        )
    return entries
