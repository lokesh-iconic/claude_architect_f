"""Markdown report rendering, one function per `main.py` subcommand --
mirrors modules 1 and 2's `report.py`.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone

from ..checks.ci_check import CiCheckResult
from ..pipeline.dedupe import DedupeResult
from ..pipeline.findings import Finding
from ..checks.hierarchy_check import HierarchyEntry
from ..checks.rules_check import CaseResult, Rule
from ..config.settings import ReviewSettings


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _mark(ok: bool) -> str:
    return "PASS" if ok else "FAIL"


def render_hierarchy(entries: list[HierarchyEntry]) -> str:
    lines = [
        "# CLAUDE.md hierarchy check",
        "",
        f"Run at {_now()}.",
        "",
        "| Scope | Path | Exists | Tracked by git | Ignored | Result |",
        "|---|---|---|---|---|---|",
    ]
    for e in entries:
        lines.append(
            f"| {e.scope} | `{e.path}` | {e.exists} | {e.tracked} | {e.ignored} "
            f"| **{_mark(e.ok)}** |"
        )
    if any(not e.tracked for e in entries):
        lines += [
            "",
            "> `tracked=False` means the file exists but hasn't been "
            "`git add`-ed yet in this working tree -- expected before the "
            "commit that ships it.",
        ]
    return "\n".join(lines) + "\n"


def render_rules(rules: list[Rule], cases: list[CaseResult]) -> str:
    lines = [
        "# Path-scoped rules check",
        "",
        f"Run at {_now()}.",
        "",
        "## Loaded rules",
        "",
        "| Rule | Description | Paths |",
        "|---|---|---|",
    ]
    for r in rules:
        lines.append(f"| `{r.name}` | {r.description} | {', '.join(r.paths)} |")
    lines += [
        "",
        "## Sample file matrix",
        "",
        "| File | " + " | ".join(r.name for r in rules) + " | Result |",
        "|---|" + "---|" * len(rules) + "---|",
    ]
    for c in cases:
        cells = ["Y" if c.actual.get(r.name) else "." for r in rules]
        lines.append(f"| `{c.path}` | " + " | ".join(cells) + f" | **{_mark(c.ok)}** |")
    return "\n".join(lines) + "\n"


def render_ci(result: CiCheckResult) -> str:
    lines = [
        "# CI pipeline static check",
        "",
        f"Run at {_now()}.",
        "",
        "| Check | Result |",
        "|---|---|",
        f"| Workflow file exists | {_mark(result.workflow_exists)} |",
        f"| Job has `timeout-minutes` | {_mark(result.has_timeout)} |",
        f"| No interactive-input step | {_mark(result.no_interactive_markers)} |",
        f"| `claude` invoked with `-p` | {_mark(result.invokes_print_mode)} |",
        f"| `claude` invoked with `--output-format json` | {_mark(result.invokes_json_output)} |",
        f"| Posts to the PR reviews API (inline comments) | {_mark(result.posts_inline_review_comments)} |",
        "",
        f"**Overall: {_mark(result.ok)}**",
    ]
    return "\n".join(lines) + "\n"


def render_review(
    settings: ReviewSettings,
    diff: str,
    findings: list[Finding],
    dedupe_result: DedupeResult,
) -> str:
    changed_lines = len(diff.splitlines())
    lines = [
        "# Review run",
        "",
        f"Run at {_now()}.",
        f"Mode : {settings.mode} ({settings.mode_reason})",
        f"Diff : {changed_lines} line(s)",
        "",
        f"Findings this run : {len(findings)}",
        f"New (would post)  : {len(dedupe_result.to_post)}",
        f"Already flagged   : {len(dedupe_result.skipped)}",
        "",
    ]
    if dedupe_result.to_post:
        lines += ["## New findings", "", "| File | Line | Category | Severity | Message |", "|---|---|---|---|---|"]
        for f in dedupe_result.to_post:
            lines.append(f"| `{f.path}` | {f.line} | {f.category} | {f.severity} | {f.message} |")
        lines.append("")
    if dedupe_result.skipped:
        lines += ["## Skipped (already flagged in a prior run)", "", "| File | Line | Message |", "|---|---|---|"]
        for f in dedupe_result.skipped:
            lines.append(f"| `{f.path}` | {f.line} | {f.message} |")
        lines.append("")
    return "\n".join(lines) + "\n"


def render_trace(
    settings: ReviewSettings,
    findings: list[Finding],
    dedupe_result: DedupeResult,
) -> dict:
    return {
        "mode": settings.mode,
        "modeReason": settings.mode_reason,
        "findings": [asdict(f) for f in findings],
        "toPost": [asdict(f) for f in dedupe_result.to_post],
        "skipped": [asdict(f) for f in dedupe_result.skipped],
    }
