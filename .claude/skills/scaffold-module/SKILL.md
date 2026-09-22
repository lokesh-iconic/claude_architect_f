---
name: scaffold-module
description: Scaffold a new numbered problem-statement module by copying this repo's structural pattern (README, main.py CLI, mode-resolution settings.py, tests/). Use when the user wants to start a new N_topic_name/ module.
context: fork
---

# Scaffold a new module

This repo's modules (`1_agent_architecture_and_orchestration/`,
`2_tool_design_and_MCP_integration/`) share one shape. This skill runs in a
forked context because reading through both existing modules to extract that
shape is several file reads that would otherwise clutter the main
conversation — only the finished scaffold should show up there.

Given a topic name and a short description of what the new module should
build:

1. Read `1_agent_architecture_and_orchestration/README.md` and
   `2_tool_design_and_MCP_integration/README.md` in full, plus each module's
   `main.py`, `*/settings.py` and one `tests/*.py` file, to internalize the
   pattern: Problem statement / Quick start / How it works / Self-check table
   / Commands / Live vs mock mode / Layout / Known limits sections in the
   README; a `main.py` with subcommands that write timestamped reports into a
   git-ignored `output/`; a `settings.py` resolving `mock`/`live`/`auto` at
   startup; a `tests/` suite with no live-network requirement.
2. Pick the next unused top-level number for the new module directory.
3. Create the new module directory with:
   - `README.md` following the same section order, with the Problem
     statement and self-check rows drawn from what the user described (leave
     TODO markers where a real answer requires code that doesn't exist yet —
     never fabricate a measurement).
   - `main.py` with an `argparse` CLI, an `all` subcommand, `--print` and
     `--quiet` flags, writing to `output/`.
   - A `settings.py` implementing the same `mock`/`live`/`auto` resolution.
   - `tests/conftest.py` and at least one real test file.
4. Add the new module's `tests/` path to root `pyproject.toml`'s
   `[tool.pytest.ini_options] testpaths`, and add its row to the root
   `README.md` module table.
5. Report back to the main conversation only: the new module's path, the
   files created, and the one or two design decisions that most needed a
   judgment call (e.g. what the mock backend should fabricate) — not the
   full text of every file.
