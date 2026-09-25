# claude_architect_foundations

Each solved exercise gets its own folder with its own README covering what it
builds, how to run it, and how to verify it.

| Module | Builds |
|---|---|
| [1_agent_architecture_and_orchestration/](1_agent_architecture_and_orchestration/) | A multi-agent research coordinator: decomposition, parallel delegation to specialist subagents, cited synthesis |
| [2_tool_design_and_MCP_integration/](2_tool_design_and_MCP_integration/) | An issue-tracker MCP server: overlapping-tool disambiguation, structured errors, resources, project/user scopes |
| [3_claude_code_configuration_and_workflows/](3_claude_code_configuration_and_workflows/) | Team Claude Code configuration (CLAUDE.md hierarchy, path-scoped rules, a slash command, a forked Skill) and a CI review pipeline with cross-run comment dedup |
| [4_prompt_engineering_and_structured_output/](4_prompt_engineering_and_structured_output/) | A structured invoice-extraction pipeline: strict tool schema with nullable fields, few-shot prompting, validation-retry with error feedback, Message Batches with failed-only resubmission, confidence-based review routing |
| [5_context_management_and_reliability/](5_context_management_and_reliability/) | A long-conversation customer-support agent over MCP tools: a persistent case-facts block kept apart from the summarized history, trimmed tool outputs, a programmatic refund prerequisite, escalation criteria with a backstop, structured timeout errors with partial results |
| [capstone/](capstone/) | Capstone: the support agent built as one system. Every refund and handoff is a schema-validated, ledgered action record, with validation-retry before any tool runs. Explicit stop_reason handling, a structured handoff protocol, a 22-turn drift test, a `/run-scenarios` slash command, a project CLAUDE.md and a one-page solution brief |

## Setup

One [uv](https://docs.astral.sh/uv/) project shared by every module.

```bash
uv sync
cp .env.example .env    # then add ANTHROPIC_API_KEY, or leave it empty
```

Then run a module from its own directory:

```bash
cd 1_agent_architecture_and_orchestration
uv run python main.py "your topic"
```

Without a valid `ANTHROPIC_API_KEY`, modules fall back to an offline mock mode
so they still run end to end. Each module writes its results to its own
`output/` folder under a fixed name (e.g. `output/context.md`); each run overwrites the previous one.

## Mock or live

Every module takes `--mode mock | live | auto`:

| Mode | What it does | Needs |
|---|---|---|
| `--mode mock` | Fully offline: a scripted stand-in replaces the model; everything else is the real code | nothing |
| `--mode live` | Calls the real API, and fails loudly instead of falling back | `ANTHROPIC_API_KEY` in `.env` (module 3: the `claude` CLI plus a credential exported in your shell) |
| *(omitted)* = `auto` | Live if the key validates, otherwise mock; the first output line says which | — |

Each module's README has a **How to run: mock and live** section with the
exact commands, and says which parts actually call the model.

## Running every test suite

Tests never need a key or the network. From the repo root:

```bash
uv run python main.py                 # all five domains, then the capstone, with a summary table
uv run python main.py 1 2 3 4 5       # the five domains only
uv run python main.py 3 capstone      # any subset
uv run python main.py -k escalat      # pass a pytest -k expression through
uv run python main.py 5 -v            # stream pytest's verbose output
uv run python main.py --fail-fast     # stop at the first module that doesn't pass
```

Each module runs in its own pytest process, with a per-module time limit
(`--timeout`, default 900 s). The command exits non-zero if any module fails,
errors, times out, or collects no tests. `uv run pytest` also works, and runs
everything in one process.

## Module layout

Every module has the same shape, so you can find things in one you haven't
opened before:

```text
<module>/
├── README.md          what it builds, how to run it (mock and live), self-check, folder structure
├── main.py            the CLI; writes reports into output/ under fixed names
├── <package>/
│   ├── config/        settings.py: .env loading and mock/live/auto resolution
│   ├── <domain>/      the core logic (orchestration/, conversation/, extraction/, pipeline/, ...)
│   ├── tools/         tool handlers, schemas and the MCP server (where the module has tools)
│   ├── backends/      the live Claude backend and the offline mock (where the module calls a model)
│   ├── evaluation/    the scripted runs and checks behind the README's self-check table
│   └── reporting/     Markdown reports and JSON traces
├── tests/             offline test suite
└── output/            run artifacts (git-ignored)
```

## Claude Code configuration

This repo also configures Claude Code itself for anyone working in it: root
[`CLAUDE.md`](CLAUDE.md), path-scoped rules in [`.claude/rules/`](.claude/rules/),
project slash commands in [`.claude/commands/`](.claude/commands/) (`/verify-module`, and
`/run-scenarios` for the capstone agent), a forked
Skill in [`.claude/skills/`](.claude/skills/), and a PR review workflow in
[`.github/workflows/claude-review.yml`](.github/workflows/claude-review.yml).
All of it is picked up automatically by a fresh clone — see
[3_claude_code_configuration_and_workflows/](3_claude_code_configuration_and_workflows/)
for what it does and how it's verified.
