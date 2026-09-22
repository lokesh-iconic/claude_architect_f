"""Self-check #2: do `.claude/rules/*.md` files load only for matching
files? Verified statically here (frontmatter parses to the intended globs,
and those globs match/don't match a fixed table of real repo paths) --
actual context-loading in a live session still has to be eyeballed, see the
README's known limits.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .globmatch import glob_match

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
RULES_DIR = REPO_ROOT / ".claude" / "rules"

_FRONTMATTER_RE = re.compile(r"\A---\n(?P<body>.*?\n)---\n", re.DOTALL)
_PATHS_RE = re.compile(r'^\s*paths:\s*\[(?P<items>.*)\]\s*$', re.MULTILINE)
_DESC_RE = re.compile(r'^\s*description:\s*(?P<desc>.+?)\s*$', re.MULTILINE)


class RuleParseError(ValueError):
    pass


@dataclass(frozen=True)
class Rule:
    name: str
    description: str
    paths: tuple[str, ...]

    def matches(self, repo_relative_path: str) -> bool:
        return any(glob_match(p, repo_relative_path) for p in self.paths)


def _parse_string_list(raw: str) -> tuple[str, ...]:
    items = []
    for part in raw.split(","):
        part = part.strip().strip('"').strip("'")
        if part:
            items.append(part)
    return tuple(items)


def load_rules(rules_dir: Path = RULES_DIR) -> list[Rule]:
    rules = []
    for path in sorted(rules_dir.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        match = _FRONTMATTER_RE.match(text)
        if not match:
            raise RuleParseError(f"{path}: missing YAML frontmatter")
        body = match.group("body")
        paths_match = _PATHS_RE.search(body)
        if not paths_match:
            raise RuleParseError(f"{path}: frontmatter missing 'paths: [...]'")
        desc_match = _DESC_RE.search(body)
        rules.append(
            Rule(
                name=path.stem,
                description=desc_match.group("desc") if desc_match else "",
                paths=_parse_string_list(paths_match.group("items")),
            )
        )
    return rules


# Real files in this repo, chosen so each rule has at least one file that
# matches only it, and at least one file neither rule should touch.
SAMPLE_CASES: tuple[tuple[str, dict[str, bool]], ...] = (
    (
        "2_tool_design_and_MCP_integration/tests/test_mcp_server.py",
        {"tests": True, "mcp-tools": False},
    ),
    (
        "1_agent_architecture_and_orchestration/tests/test_orchestration.py",
        {"tests": True, "mcp-tools": False},
    ),
    (
        "2_tool_design_and_MCP_integration/issue_tracker/mcp_server.py",
        {"tests": False, "mcp-tools": True},
    ),
    (
        "2_tool_design_and_MCP_integration/issue_tracker/handlers.py",
        {"tests": False, "mcp-tools": True},
    ),
    (
        "1_agent_architecture_and_orchestration/research_coordinator/agents.py",
        {"tests": False, "mcp-tools": False},
    ),
    ("README.md", {"tests": False, "mcp-tools": False}),
)


@dataclass(frozen=True)
class CaseResult:
    path: str
    actual: dict[str, bool] = field(default_factory=dict)
    expected: dict[str, bool] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.actual == self.expected


def check_cases(rules: list[Rule]) -> list[CaseResult]:
    results = []
    for path, expected in SAMPLE_CASES:
        actual = {rule.name: rule.matches(path) for rule in rules}
        # Only compare rules the case table actually has an opinion about.
        actual_subset = {name: actual.get(name, False) for name in expected}
        results.append(CaseResult(path=path, actual=actual_subset, expected=expected))
    return results
