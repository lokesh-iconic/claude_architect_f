# claude_architect_foundations — team standards

This repo is a set of solved exercises, one per problem statement, sharing a
single `uv` project. These rules apply everywhere; a subdirectory may add
more with its own `CLAUDE.md` (directory scope) or `.claude/rules/*.md`
(path scope) — both layer on top of this file, they don't replace it.

## Structure

- One numbered top-level folder per problem statement
  (`1_agent_architecture_and_orchestration/`, `2_tool_design_and_MCP_integration/`,
  `3_claude_code_configuration_and_workflows/`, ...), plus `capstone/`. Each
  is self-contained: its own `README.md`, its own `main.py` CLI, its own
  `tests/`.
- Each module's code is one package split into subpackages by
  responsibility, never a flat folder of modules. Always `config/` (holds
  `settings.py`, with mode resolution) and `reporting/`; then whichever of
  these apply: `backends/` (live and mock model backends), `tools/` (tool
  handlers, schemas, MCP server), `evaluation/` (the scripted runs and checks
  behind the README self-check table), plus a domain subpackage named for
  what it does (`orchestration/`, `conversation/`, `extraction/`,
  `pipeline/`, ...). Subpackage `__init__.py` files stay empty apart from a
  docstring, so the import graph doesn't change when files move. Each README
  has a *Folder structure* tree; update it when you add a file.
- A module's `main.py` writes every run's output into that module's own
  `output/` (git-ignored). Never write run artifacts anywhere else.
- Root `pyproject.toml` is shared: one venv, one `uv sync`, one
  `[tool.pytest.ini_options] testpaths` list. When you add a module with
  tests, add its `tests/` directory to that list.

## Live/mock mode

Every module that can call a live API (Anthropic, `claude` CLI, GitHub) must
resolve a `mock` / `live` / `auto` mode at startup, in a `settings.py` (or
equivalent): `auto` falls back to `mock` when credentials or the CLI aren't
available and prints why; `--mode live` turns that fallback into a hard
error instead of silently downgrading. Never let a module hang waiting on
input or a network call with no timeout.

## Code style

- Prefer `dataclasses` over bare dicts for anything with a fixed shape.
- Raise a specific, structured error type at the boundary that can fail
  (see `issue_tracker.core.errors.ToolError` for the pattern: category,
  `isRetryable`, remediation) rather than a bare `Exception`/`ValueError`.
- No new third-party dependency without discussion first — the dependency
  list is deliberately short (`anthropic`, `mcp`, `python-dotenv`, `pytest`).
  Prefer a small hand-rolled implementation (see `config_check.py`'s
  `${VAR}` expansion) over pulling in a library for one use.
- Don't add comments that restate the code; add them only for a non-obvious
  constraint or workaround.

## Tests

- Tests must never require a live API key or network access to pass. Mock
  the backend, don't skip the test.
- Prefer one test per claim a module's README self-check table makes, so the
  table stays honest as the code changes.

## Secrets

- Credentials arrive by environment-variable expansion (`${VAR}`,
  `${VAR:-default}`) in any committed config, never as literal values.
- Never write PII or secrets (API keys, tokens, credentials, SSNs, or similar)
  into a committed file, a report under `output/`, or a CI log.
