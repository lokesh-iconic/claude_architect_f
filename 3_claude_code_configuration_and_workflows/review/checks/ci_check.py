"""Self-check #3: does the CI step complete without hanging on interactive
input? Checked statically: the workflow bounds the job with a timeout and
has no interactive-input step, and the underlying `claude` invocation
(review/clients/claude_cli.py) actually uses `-p` -- Claude Code's non-interactive
print mode -- with `--output-format json`. Also confirms the posting side
(review/clients/github_client.py) targets the PR review-comments API, which is what
"inline PR comments" means on GitHub's REST API.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "claude-review.yml"
CLAUDE_CLI_PATH = Path(__file__).resolve().parents[1] / "clients" / "claude_cli.py"
GITHUB_CLIENT_PATH = Path(__file__).resolve().parents[1] / "clients" / "github_client.py"

INTERACTIVE_MARKERS = ("stdin:", "read -p ", "input()", "Read-Host")


@dataclass(frozen=True)
class CiCheckResult:
    workflow_exists: bool
    has_timeout: bool
    no_interactive_markers: bool
    invokes_print_mode: bool
    invokes_json_output: bool
    posts_inline_review_comments: bool

    @property
    def ok(self) -> bool:
        return all(
            (
                self.workflow_exists,
                self.has_timeout,
                self.no_interactive_markers,
                self.invokes_print_mode,
                self.invokes_json_output,
                self.posts_inline_review_comments,
            )
        )


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def check_ci() -> CiCheckResult:
    workflow = _read(WORKFLOW_PATH)
    claude_cli = _read(CLAUDE_CLI_PATH)
    github_client = _read(GITHUB_CLIENT_PATH)

    return CiCheckResult(
        workflow_exists=bool(workflow),
        has_timeout="timeout-minutes" in workflow,
        no_interactive_markers=not any(m in workflow for m in INTERACTIVE_MARKERS),
        invokes_print_mode='"-p"' in claude_cli or "'-p'" in claude_cli,
        invokes_json_output="--output-format" in claude_cli and '"json"' in claude_cli,
        posts_inline_review_comments="/pulls/" in github_client and "/reviews" in github_client,
    )
