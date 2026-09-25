"""The Claude Code workflow checks: CLAUDE.md, the slash command, the plan, the brief.

These are documents, so the checks cross-reference them against the code:
CLAUDE.md must name every tool, every action-record field, every escalation
reason and every error field the code actually defines. A contract change
that isn't documented then fails here instead of silently drifting.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from ..tools.handlers import ESCALATION_REASONS
from .results import Check, SuiteResult
from ..tools.schema import ESCALATE_TO_HUMAN, PROCESS_REFUND, SCHEMAS
from ..config.settings import MODULE_DIR, PROJECT_ROOT

MODULE = MODULE_DIR.name
COMMAND = PROJECT_ROOT / ".claude" / "commands" / "run-scenarios.md"
BRIEF_MAX_WORDS = 800  # roughly one printed page with a small diagram


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _headings(text: str) -> list[str]:
    return [h.strip().lower() for h in re.findall(r"^#{1,3}\s+(.+)$", text, re.M)]


def _missing(text: str, terms: list[str]) -> list[str]:
    return [t for t in terms if t not in text]


def run_workflow() -> SuiteResult:
    suite = SuiteResult("workflow")

    claude_md = _read(MODULE_DIR / "CLAUDE.md")
    heads = _headings(claude_md)
    need = ["architecture", "tool contracts", "escalation policy"]
    suite.checks.append(Check(
        "The project CLAUDE.md has Architecture, Tool contracts and Escalation policy sections",
        all(any(n in h for h in heads) for n in need), f"headings: {heads}"))
    terms = (list(SCHEMAS) + list(PROCESS_REFUND["properties"]) + list(ESCALATE_TO_HUMAN["properties"])
             + list(ESCALATION_REASONS) + ["errorCategory", "isRetryable", "description"])
    missing = _missing(claude_md, terms)
    suite.checks.append(Check(
        "CLAUDE.md names every tool, action-record field, escalation reason and error field the code defines",
        bool(claude_md) and not missing, f"missing: {missing or 'none'} ({len(terms)} terms checked)"))

    command = _read(COMMAND)
    front = command.split("---")[1] if command.startswith("---") else ""
    suite.checks.append(Check(
        "The /run-scenarios slash command exists, is described, and runs this module's scenario set",
        "description:" in front and f"{MODULE}/main.py scenarios" in command and "$ARGUMENTS" in command,
        f"{COMMAND.relative_to(PROJECT_ROOT)}: {'found' if command else 'missing'}"))

    plan = _read(MODULE_DIR / "PLAN.md")
    suite.checks.append(Check(
        "The architecture plan exists and covers the loop, the call path and the verification plan",
        all(s in plan for s in ("## Decisions", "stop_reason", "ToolSession.call", "## Verification plan")),
        f"PLAN.md: {len(plan.split())} words"))

    brief = _read(MODULE_DIR / "SOLUTION_BRIEF.md")
    words = len(re.sub(r"[`|#*\-─│┌┐└┘├┤▼►]", " ", brief).split())
    bheads = _headings(brief)
    wanted = ["architecture", "tradeoff", "escalation policy", "open risk"]
    suite.checks.append(Check(
        f"The solution brief fits on one page (<= {BRIEF_MAX_WORDS} words) and has architecture, tradeoffs, "
        "escalation policy and an open risk",
        bool(brief) and words <= BRIEF_MAX_WORDS and all(any(w in h for h in bheads) for w in wanted),
        f"{words} words; headings: {bheads}"))

    pyproject = tomllib.loads(_read(PROJECT_ROOT / "pyproject.toml") or "")
    testpaths = pyproject.get("tool", {}).get("pytest", {}).get("ini_options", {}).get("testpaths", [])
    readme = _read(PROJECT_ROOT / "README.md")
    suite.checks.append(Check(
        "The module is wired into the repo: root pytest testpaths and the root README table",
        f"{MODULE}/tests" in testpaths and f"[{MODULE}/]" in readme, f"testpaths: {testpaths}"))
    return suite
