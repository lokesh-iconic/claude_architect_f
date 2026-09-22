# Directory-scope additions for this module

On top of the root `CLAUDE.md`, these apply only while working inside
`3_claude_code_configuration_and_workflows/`:

- This module edits files outside its own directory (`../CLAUDE.md`,
  `../.claude/`, `../.github/workflows/`, `../pyproject.toml`, `../README.md`)
  because Claude Code and GitHub Actions only discover config at those fixed
  locations. Before touching any of those, check whether the change is
  actually about *this* module or would affect modules 1/2 too — root
  `.claude/rules/*.md` in particular are repo-wide, not module-scoped.
- The `review/` package must stay offline-testable exactly like modules 1/2:
  a `MockRunner`/`LiveRunner` pair for the `claude` CLI call, and a
  transport-injected `GithubReviewClient` so no test needs `gh` or network.
- Fingerprinting (`review/findings.py`) intentionally excludes line numbers.
  Don't "fix" a dedup test failure by adding line back in — that's the
  regression this module exists to prevent.
