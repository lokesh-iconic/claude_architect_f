# Claude Code Configuration and Workflows

## Problem statement

A team needs Claude Code configured consistently across every developer's
machine, plus a working automated code-review step inside CI/CD that
doesn't hang, doesn't duplicate comments across runs, and gives actionable
feedback.

What it has to prove:

- The CLAUDE.md hierarchy configured correctly across user, project, and
  directory scope
- Path-specific rules and a custom Skill used appropriately
- Judgment about when plan mode earns its keep versus direct execution
- Claude Code run non-interactively inside an automated pipeline

Unlike modules 1 and 2, most of what this proves isn't Python code — it's
real Claude Code configuration, which only takes effect from the places
Claude Code and GitHub Actions actually look. So the config itself
(`CLAUDE.md`, `.claude/`, `.github/workflows/`) lives at the **repo root**,
not in this folder. This folder holds the write-up and the CI review logic,
and offline-verifies the root-level config the same way modules 1/2
offline-verify their claims.

---

## Quick start

From the repo root:

```bash
uv sync
uv run python 3_claude_code_configuration_and_workflows/main.py all
```

That runs all four checks and writes reports into this module's
[`output/`](.):

```
  wrote output/hierarchy.md
  wrote output/rules.md
  wrote output/ci.md
  wrote output/review.md
```

To see the config live in Claude Code, start a session at the repo root and:

```
/memory              # confirms CLAUDE.md is loaded (project + directory scope)
/verify-module 2_tool_design_and_MCP_integration
```

---

## How to run: mock and live

Every command below runs from the **repo root**. One-time setup, from the repo root:

```bash
uv sync
cp .env.example .env      # only needed for live mode; .env is git-ignored
```

### Mock mode (offline, no API key)

```bash
uv run python 3_claude_code_configuration_and_workflows/main.py all --mode mock
uv run python 3_claude_code_configuration_and_workflows/main.py review --mode mock   # run again: same findings, now skipped
```

Checks 1–3 (`hierarchy-check`, `rules-check`, `ci-check`) are static and never
call a model in either mode. `review` in mock mode uses `MockRunner`, which
produces `[SYNTHETIC]` findings from `TODO`/`FIXME` lines in the diff, and a
local dedup state file (`output/.review-state.json`).

### Live mode (real `claude` CLI)

Live review shells out to Claude Code in print mode (`claude -p`), not to the
Python SDK:

1. Install the CLI: `npm install -g @anthropic-ai/claude-code`, and make sure
   `claude` is on your PATH (or set `CLAUDE_CODE_BIN`).
2. Export a credential in your shell: `ANTHROPIC_API_KEY` or
   `CLAUDE_CODE_OAUTH_TOKEN`. This module reads the environment directly; it
   doesn't load `.env`.
3. Run:

```bash
# Review your branch locally; dedup against output/.review-state.json, nothing posted
uv run python 3_claude_code_configuration_and_workflows/main.py review --mode live --base origin/master

# Review a PR: dedup against its existing comments and post new ones (needs an authenticated `gh`)
uv run python 3_claude_code_configuration_and_workflows/main.py review --mode live --base origin/master --repo owner/name --pr 123

# Compute findings without saving state or posting
uv run python 3_claude_code_configuration_and_workflows/main.py review --mode live --dry-run
```

In CI, [`claude-review.yml`](../.github/workflows/claude-review.yml) runs the
same command with `--mode auto`, using the `ANTHROPIC_API_KEY` repository
secret.

### Auto mode (the default)

Leave `--mode` off: live if the `claude` binary is on PATH **and** a credential
is set, otherwise mock, with the reason printed.

### Tests

```bash
uv run pytest 3_claude_code_configuration_and_workflows/tests      # always offline
uv run python main.py 3      # also from the repo root: the root runner, with a summary
```

---

## What's configured

| Artifact | Where | Scope |
|---|---|---|
| Team standards | [`../CLAUDE.md`](../CLAUDE.md) | Project — applies repo-wide |
| Module-specific additions | [`CLAUDE.md`](CLAUDE.md) | Directory — applies only inside this folder |
| Personal preferences template | [`templates/user_claude_md.example`](templates/user_claude_md.example) | User — copy to `~/.claude/CLAUDE.md` yourself; can't be committed, it's per-machine |
| Test conventions | [`../.claude/rules/tests.md`](../.claude/rules/tests.md) | `paths: ["**/tests/**/*.py"]` |
| MCP tool conventions | [`../.claude/rules/mcp-tools.md`](../.claude/rules/mcp-tools.md) | `paths: ["**/issue_tracker/**/*.py"]` |
| `/verify-module` slash command | [`../.claude/commands/verify-module.md`](../.claude/commands/verify-module.md) | Project-scoped |
| `scaffold-module` Skill | [`../.claude/skills/scaffold-module/SKILL.md`](../.claude/skills/scaffold-module/SKILL.md) | `context: fork` |
| PR review workflow | [`../.github/workflows/claude-review.yml`](../.github/workflows/claude-review.yml) | Runs `claude -p` non-interactively, posts inline PR comments |

### Why these two rule files don't overlap

`tests.md` matches anything under a `tests/` directory; `mcp-tools.md`
matches anything under `issue_tracker/`. Those are disjoint in this repo, so
every sample file below is owned by exactly one rule, or neither — a real,
checkable version of "loads only for matching files, stays silent
otherwise":

| File | `tests` | `mcp-tools` |
|---|---|---|
| `2_tool_design_and_MCP_integration/tests/test_mcp_server.py` | Y | . |
| `1_agent_architecture_and_orchestration/tests/test_orchestration.py` | Y | . |
| `2_tool_design_and_MCP_integration/issue_tracker/tools/mcp_server.py` | . | Y |
| `2_tool_design_and_MCP_integration/issue_tracker/tools/handlers.py` | . | Y |
| `1_agent_architecture_and_orchestration/research_coordinator/orchestration/agents.py` | . | . |
| `README.md` | . | . |

### The `scaffold-module` Skill and `context: fork`

Scaffolding a new module means reading both existing modules in full to
copy their structure — several file reads that would otherwise sit in the
main conversation for no reason once the scaffold exists. `context: fork`
runs that exploration in an isolated context and only the finished file
list comes back to the main conversation.

### The CI pipeline and comment dedup

[`claude-review.yml`](../.github/workflows/claude-review.yml) runs on every
`pull_request` (opened/synchronize/reopened), with `timeout-minutes: 15` and
no interactive step — `claude -p` is Claude Code's non-interactive print
mode, so there is no prompt to hang on in the first place. It shells out to
[`review/clients/claude_cli.py`](review/clients/claude_cli.py)'s `LiveRunner`, which invokes:

```
claude -p "<prompt + diff>" --output-format json --model <model>
```

and parses the JSON findings back out
([`review/pipeline/findings.py`](review/pipeline/findings.py)). Each finding is fingerprinted
from its **file path, category, and normalized message — not its line
number** ([`review/pipeline/findings.py`](review/pipeline/findings.py)`:Finding.fingerprint`),
so a later commit that shifts line numbers doesn't make an unresolved
finding look new. Before posting, [`review/pipeline/dedupe.py`](review/pipeline/dedupe.py)
drops any finding whose fingerprint is already present in a hidden HTML
marker (`<!-- claude-review:<fp> -->`) on an existing PR review comment,
fetched via `gh api repos/{repo}/pulls/{pr}/comments`. Only what's left gets
posted, in one batched review via
`gh api repos/{repo}/pulls/{pr}/reviews` — real inline PR comments, not a
single summary comment.

Locally (no GitHub, no `claude` binary needed), the same dedup logic runs
against a JSON file standing in for "what the prior run already posted":

```bash
uv run python 3_claude_code_configuration_and_workflows/main.py review --mode mock
uv run python 3_claude_code_configuration_and_workflows/main.py review --mode mock   # run again: same findings, now skipped
```

---

## Self-check

| # | Question | Answer | How to verify |
|---|---|---|---|
| 1 | Does a completely fresh clone pick up CLAUDE.md without any manual step? | **Yes**, for project and directory scope — both are ordinary committed files at the paths Claude Code auto-discovers (repo root, and this folder). User scope genuinely can't be a repo file; a template plus copy instructions is the honest equivalent, same pattern module 2 used for its personal MCP server. | `uv run python main.py hierarchy-check` — checks existence, and that git actually tracks the file (not just present in this working tree) |
| 2 | Do path-scoped rules load only when you edit a matching file, and stay silent otherwise? | **Yes, statically verified.** `tests.md` and `mcp-tools.md` target disjoint directories in this repo; a 6-file matrix (3 pairs: one file each rule should own alone, one file neither should touch) matches exactly as designed. **Caveat:** this checks the frontmatter's glob logic, not that a live Claude Code session actually activates only that rule's text in context — that still has to be eyeballed with `/memory` in a real session. | `uv run python main.py rules-check`, or `uv run pytest -k rules_check` |
| 3 | Does the CI step complete without hanging on interactive input? | **Yes, by construction.** `claude -p` is Claude Code's print mode — there is no prompt loop to hang on — and the job also carries `timeout-minutes: 15` as a backstop. Checked statically: the workflow has no interactive-input step, and `claude_cli.py`'s subprocess call really does pass `-p` and `--output-format json`. **Caveat:** this hasn't run on a live GitHub Actions runner in this environment — see Known limits. | `uv run python main.py ci-check`, or `uv run pytest -k ci_check` |
| 4 | Does the second CI run avoid re-posting comments on issues already flagged in the first? | **Yes.** Fingerprinting excludes line number by design, so a finding that merely shifted lines still dedupes. A test simulates run 1 (one TODO) then run 2 (same TODO plus a genuinely new one, with a line inserted above both) and asserts only the new one is posted. | `uv run pytest -k dedupe`, or run `main.py review --mode mock` twice locally and diff the two reports |

---

## Plan mode vs. direct execution — a real comparison, not a simulated one

The brief asks to run one well-scoped single-file change directly and one
multi-file architectural change in plan mode, and note where plan mode
changed the outcome. Both happened in this session, on this module, so this
is a candid account of this session's own work rather than a staged demo.

**Plan mode — this module's build.** Spans the repo root (`CLAUDE.md`, two
rules files, a command, a forked Skill, a CI workflow) and a new ~15-file
package here, none of which existed before. Planning surfaced a decision
that direct execution would likely have gotten wrong on the first pass: the
config artifacts had to live at the repo root, not inside this module's
folder, because that's the only place a fresh clone's Claude Code session
and GitHub Actions actually read from — self-contained-per-module (the
pattern modules 1/2 use) doesn't work for config whose entire point is to be
discovered ambiently. Plan mode is also where the fingerprint-excludes-line
design got decided before any dedup code existed, rather than being
discovered via a failing test after the fact.

**Direct execution — the follow-up in this same session.** After the
plan-mode build landed, adding a `--version` flag to
[`main.py`](main.py)'s `argparse` parser (`parser.add_argument("--version",
action="version", ...)`) was done directly, no plan mode. One file, one
line, no design question to weigh — `argparse`'s built-in `action="version"`
is the only reasonable way to do it. Entering plan mode for this would have
cost more than the change itself; the outcome would have been identical
either way, which is exactly the signal that plan mode wasn't earning its
keep here.

---

## Commands

All commands run from the repo root.

| Command | What it does |
|---|---|
| `uv run python 3_claude_code_configuration_and_workflows/main.py all` | Run all four checks (mock mode, offline) |
| `... hierarchy-check` | Self-check #1 |
| `... rules-check` | Self-check #2 |
| `... ci-check` | Self-check #3 |
| `... review --mode mock` | Self-check #4, offline, using a local dedup state file |
| `... review --mode live --repo owner/name --pr 123` | Real run: `claude -p`, dedup against the PR's actual comments |
| `... --print` | Also echo reports to the terminal |
| `... --quiet` | Suppress progress output |
| `uv run pytest 3_claude_code_configuration_and_workflows/tests` | Run the 53 tests |

Full option list: `uv run python 3_claude_code_configuration_and_workflows/main.py --help`

---

## Live vs mock mode

Resolved once at startup by [`review/config/settings.py`](review/config/settings.py), same
shape as modules 1/2 but checking for the `claude` **CLI binary** plus
`ANTHROPIC_API_KEY`/`CLAUDE_CODE_OAUTH_TOKEN`, not the `anthropic` Python
SDK:

1. `--mode mock` → mock, always.
2. `claude` not on PATH, or no credential env var → mock, reason printed.
3. Both present → live: `claude -p ... --output-format json` is actually
   invoked.
4. `--mode live` turns step 2's fallback into a hard error.

Mock findings are tagged `[SYNTHETIC]` and come from a deterministic
heuristic (an added `TODO`/`FIXME` line in the diff) — enough to exercise
parsing, fingerprinting, and dedup end to end without a real call, never to
be mistaken for a real review.

---

## Folder structure

```text
3_claude_code_configuration_and_workflows/
├── main.py                      CLI for the four checks; writes into output/
├── CLAUDE.md                    Directory-scope additions for this module
├── review/
│   ├── config/
│   │   └── settings.py          Live/mock resolution for the claude CLI
│   ├── checks/
│   │   ├── hierarchy_check.py   Self-check 1: CLAUDE.md files exist and are git-tracked
│   │   ├── rules_check.py       Self-check 2: rule frontmatter + glob matrix
│   │   ├── ci_check.py          Self-check 3: static workflow inspection
│   │   └── globmatch.py         Hand-rolled **-aware glob matcher
│   ├── pipeline/
│   │   ├── diffing.py           Git diff access
│   │   ├── findings.py          Finding, JSON parsing, fingerprinting
│   │   ├── dedupe.py            Cross-run dedup (self-check 4)
│   │   └── state.py             Local stand-in for a prior run's posted findings
│   ├── clients/
│   │   ├── claude_cli.py        LiveRunner (real `claude -p`) and MockRunner
│   │   └── github_client.py     `gh api` wrapper, injectable transport
│   └── reporting/
│       └── report.py            Markdown report rendering
├── templates/
│   └── user_claude_md.example   User-scope CLAUDE.md template
├── tests/                       53 tests, one per claim this README makes
└── output/                      Reports and the local review state, written at runtime (git-ignored)
```

| Folder | Responsibility |
|---|---|
| [`review/config/`](review/config/) | Settings and live/mock resolution for the `claude` CLI |
| [`review/checks/`](review/checks/) | Offline checks of the Claude Code config and the CI workflow |
| [`review/pipeline/`](review/pipeline/) | Diff, findings, fingerprints, cross-run dedup state |
| [`review/clients/`](review/clients/) | The `claude -p` runner and the GitHub client |
| [`review/reporting/`](review/reporting/) | Report rendering |
| [`templates/`](templates/) | The user-scope CLAUDE.md template |

The Claude Code configuration this module verifies lives at the repo root,
because that's the only place Claude Code and GitHub Actions look for it:

| File | Role |
|---|---|
| [`../CLAUDE.md`](../CLAUDE.md) | Project-scope team standards |
| [`../.claude/rules/`](../.claude/rules/) | Path-scoped rules |
| [`../.claude/commands/verify-module.md`](../.claude/commands/verify-module.md) | The `/verify-module` slash command |
| [`../.claude/skills/scaffold-module/SKILL.md`](../.claude/skills/scaffold-module/SKILL.md) | The forked scaffolding Skill |
| [`../.github/workflows/claude-review.yml`](../.github/workflows/claude-review.yml) | The CI workflow |

---

## Known limits

- **Live mode is unexercised end to end.** No `ANTHROPIC_API_KEY` or `claude`
  binary was available in this environment, and running the workflow live
  requires pushing a branch and opening a PR on the real GitHub repo — a
  visible, shared-state action outside what this task should do without
  separate confirmation. The `-p`/`--output-format json` call shape is
  checked statically (`ci-check`) and unit-tested against a mocked
  `subprocess.run`, but no real `claude -p` call has been made, and the
  workflow has never run on an actual GitHub Actions runner.
- **Rule activation is checked statically, not observed live.** `rules-check`
  proves the frontmatter's globs match the intended files; it can't observe
  whether a real Claude Code session actually loads only that rule's text
  when you open a matching file. That confirmation is manual (`/memory` or
  equivalent).
- **The mock reviewer is a deliberately dumb heuristic** (added TODO/FIXME),
  not a stand-in for what Claude would actually flag. It exists to exercise
  the parse → fingerprint → dedupe → post pipeline offline, same spirit as
  modules 1/2's synthetic sources.
- **Dedup is per-PR, not per-repo.** A finding fixed and reintroduced later
  in a *different* PR is treated as new, which is the correct behavior, not
  a limitation — but worth stating since nothing in the code enforces it
  beyond the fingerprint being scoped to fetched-comments-on-this-PR.
- **`hierarchy-check` reads `tracked=False` until this module is
  `git add`-ed.** That's expected before the commit that ships it, not a
  failure of the check.
