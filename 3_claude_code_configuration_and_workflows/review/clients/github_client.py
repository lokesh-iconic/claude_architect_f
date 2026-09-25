"""Posting inline PR review comments via `gh api`.

The real subprocess call is a thin, untested-by-design wrapper; everything
that decides *what* to post lives in `dedupe.py` and is tested against a
fake transport here, so no test needs `gh` or network access.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from typing import Callable

from ..pipeline.findings import Finding

# A transport takes (argv-after-"gh", optional stdin payload) and returns stdout.
Transport = Callable[[list[str], str | None], str]


def _gh_transport(args: list[str], input_data: str | None = None) -> str:
    result = subprocess.run(
        ["gh", *args], input=input_data, capture_output=True, text=True, check=True
    )
    return result.stdout


@dataclass
class GithubReviewClient:
    repo: str  # "owner/name"
    pr_number: int
    transport: Transport = field(default=_gh_transport)

    def fetch_existing_comment_bodies(self) -> list[str]:
        out = self.transport(
            ["api", f"repos/{self.repo}/pulls/{self.pr_number}/comments", "--paginate"],
            None,
        )
        comments = json.loads(out) if out.strip() else []
        return [c.get("body", "") for c in comments]

    def post_review(self, commit_id: str, findings: list[Finding]) -> None:
        if not findings:
            return
        payload = {
            "commit_id": commit_id,
            "event": "COMMENT",
            "body": f"Claude review: {len(findings)} new finding(s).",
            "comments": [
                {"path": f.path, "line": f.line, "body": f.to_comment_body()} for f in findings
            ],
        }
        self.transport(
            ["api", f"repos/{self.repo}/pulls/{self.pr_number}/reviews", "--input", "-"],
            json.dumps(payload),
        )
