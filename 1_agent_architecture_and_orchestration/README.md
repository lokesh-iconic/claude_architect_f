# Multi-Agent Research Coordinator

## Problem statement

A client wants a system that researches any topic and produces a
comprehensive, cited report. Build a coordinator agent that delegates to at
least two specialized subagents — one that searches the web and one that
analyzes documents — synthesizes their findings, and produces a final report
with preserved source attribution.

What it has to prove:

- An agentic loop that correctly handles `stop_reason` (`tool_use` vs `end_turn`)
- A coordinator–subagent system, including task decomposition and delegation
- Context passed explicitly between agents, since subagents do not inherit
  conversation history
- Programmatic enforcement where prompt instructions alone are not reliable

Run it with no API key and it still works — it falls back to an offline mock
backend that exercises the same orchestration code.

---

## Quick start

From this directory:

```bash
uv sync
uv run python main.py "the impact of AI on creative industries"
```

Every run writes a Markdown report and a JSON trace into [`output/`](.):

```
Report : output/the-impact-of-ai-on-creative-industries.md
Trace  : output/the-impact-of-ai-on-creative-industries.json
```

Without a valid key the run prints `Mode : mock (no ANTHROPIC_API_KEY ...)` and
completes offline. To run against the real API, set `ANTHROPIC_API_KEY` in the
project's `.env` file (one directory up, shared by every module).

---

## How to run: mock and live

Every command below runs from this directory. One-time setup, from the repo root:

```bash
uv sync
cp .env.example .env      # only needed for live mode; .env is git-ignored
```

### Mock mode (offline, no API key)

```bash
uv run python main.py "the impact of AI on creative industries" --mode mock
uv run python main.py "the impact of AI on creative industries" --mode mock --simulate-timeout document_analyst --timeout 2
```

Decomposition and web research are scripted (`backends/mock_plans.py`), and every
web source is marked `[SYNTHETIC]`. The orchestration, timeouts and attribution
are the real code paths, and the document analyst reads the real files in
`research_coordinator/corpus/`.

### Live mode (real Claude API)

1. Add your key to the repo-root `.env`: `ANTHROPIC_API_KEY=<your key>`. Never put it in a committed file.
2. Run:

```bash
uv run python main.py "the impact of AI on creative industries" --mode live
```

The coordinator and every subagent call `claude-opus-5` with adaptive thinking,
so this spends tokens. The web researcher uses Anthropic's server-side
`web_search` tool; set `RC_ENABLE_WEB_SEARCH=false` in `.env` to keep live runs
off the web. `--mode live` fails loudly if the key is missing or rejected,
instead of quietly falling back to mock.

### Auto mode (the default)

Leave `--mode` off and the module picks for you: live if `ANTHROPIC_API_KEY` is
set and passes a zero-token `GET /v1/models/{id}` check, otherwise mock. The
first line of output always says which mode ran and why.

### Tests

```bash
uv run pytest 1_agent_architecture_and_orchestration/tests      # from the repo root; always offline, no key needed
uv run python main.py 1      # also from the repo root: the root runner, with a summary
```

---

## How it works

```
                  ┌─────────────────────────────────────┐
   topic  ───────▶│ coordinator          tools: [Task]  │
                  │  1. decompose into subtasks         │
                  │  2. emit N Task calls in ONE reply  │
                  │  4. review gaps, re-delegate        │
                  │  5. synthesize the report           │
                  └───────┬───────────────────┬─────────┘
                          │ 3. parallel fan-out│
            ┌─────────────▼──────┐   ┌─────────▼──────────────────┐
            │ web_researcher     │   │ document_analyst           │
            │ tools:             │   │ tools:                     │
            │  web_search        │   │  list_documents            │
            │  submit_findings   │   │  search_documents          │
            │                    │   │  read_document             │
            │ fresh context      │   │  submit_findings           │
            └────────────────────┘   └────────────────────────────┘
```

The agentic loop ([`loop.py`](research_coordinator/orchestration/loop.py)) drives every agent:
call the model, branch on `stop_reason`, run the tools it asked for, feed the
results back, repeat. It continues while `stop_reason == "tool_use"` and stops
on `"end_turn"`; `pause_turn`, `max_tokens`, and `refusal` each get their own
branch rather than being lumped into a generic failure.

### Design decisions worth knowing

**Why the Messages API and not the Claude Agent SDK.** The brief names the
Agent SDK, but it also asks for the `stop_reason` loop to be written by hand —
and the Agent SDK's whole job is to hide that loop. This implementation keeps
the SDK's *concepts* explicit (`AgentDefinition`, a tool allowlist per agent, a
`Task` tool that spawns subagents) while owning the loop, so the `tool_use` /
`end_turn` branch is real code you can read and test. It also removes any
dependency on the Claude Code CLI, which is what makes offline mock mode
possible.

**Subagents inherit nothing.** Each `Task` call starts a fresh message list
containing only its brief. A brief shorter than 40 characters is rejected
outright, because "research the above" is the failure mode that context
isolation invites.

**Parallelism comes from batching, not from threads.** When the coordinator
emits several `tool_use` blocks in one response, the loop runs them with
`asyncio.gather` and returns every `tool_result` in a *single* user message —
splitting them across messages is what teaches a model to stop making parallel
calls.

**Attribution is enforced by the program, not by the prompt.** Subagents must
finish by calling `submit_findings`, a strict-schema tool where every claim
carries its own source title, locator, type, and date. The orchestrator assigns
each distinct locator a stable `[S#]` id and hands those ids to the coordinator
to cite. The bibliography in the final report is generated from that table, so
a synthesis step cannot drop attribution even if the model forgets to.

**A subagent that fails does not sink the run.** Every `Task` has a deadline.
On failure the coordinator receives structured JSON — `errorCategory`,
`isRetryable`, `attempted`, `partialResults` — retries once if the failure says
it is retryable, and otherwise writes the report with a "Coverage not obtained"
section naming what is missing.

---

## Self-check

The four questions the brief asks you to verify, and where the answer comes
from. Every measurement below is from a mock-mode run on this machine.

| # | Question | Answer | How to verify |
|---|---|---|---|
| 1 | Does the decomposition cover the whole topic, or does it quietly narrow to one angle? | **Inspectable, with a caveat.** Every delegated subtask is printed as it is issued and recorded per round under `## Run provenance` and in the trace's `rounds` array — so the coordinator's own subtask list is auditable rather than hidden. A default run covers four angles across two rounds (economic/labour, legal/policy, practitioner evidence, then public attitudes as the gap round). **Caveat:** in mock mode those angles are templated. Genuinely topic-adaptive decomposition only happens in live mode. | `uv run python main.py "TOPIC"` then read the `Round 1:` / `Round 2:` lines, or `rounds` in the JSON trace |
| 2 | Are subagents actually running in parallel — by wall clock, not just correctness? | **Yes.** Identical plan, two execution modes: **1.16s parallel vs 1.97s serial**, overlap factor 1.66x vs 0.96x. Overlap is summed subagent time ÷ wall clock, so anything above 1.0 is real concurrency. | Run once with `--serial` and once without, then compare the `Wall clock` line in each report's `## Run provenance` |
| 3 | Does the final report preserve source attribution for every claim, after synthesis? | **Yes, structurally.** Attribution does not depend on the model remembering to cite. Each finding arrives through the strict-schema `submit_findings` tool with its own source metadata; the orchestrator assigns `[S#]` ids and generates the `## Sources` section from that table. A test asserts every claim's `source_id` resolves to a registered source, and that one locator never gets two ids. | `uv run pytest -k attribution`, or read `## Sources` in any report |
| 4 | On a subagent timeout, does the coordinator get structured error context — and can it still produce a usable report? | **Yes.** The coordinator receives JSON with `errorCategory: "transient"`, `isRetryable`, `attempted`, and `partialResults` listing the tool calls that completed before the deadline — not a generic failure string. The run continues, the report still cites the subtasks that succeeded, and a `## Coverage not obtained` section names the lost angle. | `uv run python main.py "TOPIC" --simulate-timeout document_analyst --timeout 2` |

---

## Commands

All commands run from this directory.

| Command | What it does |
|---|---|
| `uv run python main.py "TOPIC"` | Research a topic (auto mode) |
| `... "TOPIC" --print` | Also echo the report to the terminal |
| `... "TOPIC" --mode mock` | Force the offline backend |
| `... "TOPIC" --mode live` | Require a working key; fail loudly if absent |
| `... "TOPIC" --serial` | Run subtasks one at a time, for timing comparison |
| `... "TOPIC" --simulate-timeout document_analyst` | Hang that subagent to exercise the failure path |
| `... "TOPIC" --timeout 30` | Per-subagent deadline in seconds |
| `... "TOPIC" --max-rounds 2` | Cap the coordinator's delegation rounds |
| `... "TOPIC" --corpus ./my_docs` | Point the document analyst at your own files |
| `... "TOPIC" --out r.md --trace r.json` | Override the default `output/` paths |
| `... "TOPIC" --quiet` | Suppress progress output |
| `uv run pytest` | Run the test suite (18 tests) |

Full option list: `uv run python main.py --help`

---

## Live vs mock mode

Mode is resolved once at startup by [`settings.py`](research_coordinator/config/settings.py):

1. `--mode mock` → mock, always.
2. No `ANTHROPIC_API_KEY` → mock.
3. Key present → validated with `GET /v1/models/{id}`, which authenticates and
   confirms the model id **without spending a token**. A 401, 403, 404, or a
   connection failure falls back to mock with the reason printed.
4. `--mode live` turns every one of those fallbacks into a hard error instead,
   so a scripted run can't silently produce mock output.

| | live | mock |
|---|---|---|
| Decomposition | model-generated, adapts to the topic | templated dimensions |
| Web research | Anthropic's server-side `web_search` tool, real URLs | fixed synthetic index |
| Document analysis | real files in the corpus | real files in the corpus |
| Orchestration, timeouts, attribution | identical | identical |

**Mock sources are fabricated.** They are titled `[SYNTHETIC]` and point at
`example.invalid`, and every mock report carries a banner saying so. They exist
to prove the attribution plumbing works, and must never be cited as real.

### Configuration

Set in the project `.env`: `RC_MODEL_COORDINATOR`, `RC_MODEL_SUBAGENT` (both
`claude-opus-5` by default), `RC_EFFORT_COORDINATOR` / `RC_EFFORT_SUBAGENT`,
`RC_ENABLE_WEB_SEARCH`, `RC_SUBAGENT_TIMEOUT`, `RC_MAX_ROUNDS`.

Live runs use adaptive thinking on both roles, with higher effort on the
coordinator (the synthesis and gap-review work) than on the subagents.

---

## Folder structure

```text
1_agent_architecture_and_orchestration/
├── main.py                      CLI: research a topic, write report + trace to output/
├── research_coordinator/
│   ├── config/
│   │   └── settings.py          .env loading, live/mock resolution, key validation
│   ├── orchestration/
│   │   ├── orchestrator.py      The Task tool: spawning, timeouts, retries, source registry
│   │   ├── loop.py              The agentic loop, stop_reason handling, allowlist enforcement
│   │   ├── agents.py            AgentDefinitions: system prompt + tool allowlist per role
│   │   └── tools.py             Tool schemas, executors, structured ToolError
│   ├── backends/
│   │   ├── backend.py           LiveBackend (Messages API) and MockBackend
│   │   └── mock_plans.py        Deterministic plans used only in mock mode
│   ├── reporting/
│   │   └── report.py            Markdown report, generated bibliography, JSON trace
│   └── corpus/                  Sample documents for the document analyst
├── tests/                       18 tests, one per claim this README makes
└── output/                      Reports and traces, written at runtime (git-ignored)
```

| Folder | Responsibility |
|---|---|
| [`research_coordinator/config/`](research_coordinator/config/) | Settings and live/mock mode resolution |
| [`research_coordinator/orchestration/`](research_coordinator/orchestration/) | Coordinator, subagent loop, agent definitions, tools |
| [`research_coordinator/backends/`](research_coordinator/backends/) | Live Claude backend and the offline mock |
| [`research_coordinator/reporting/`](research_coordinator/reporting/) | Report and trace rendering |
| [`research_coordinator/corpus/`](research_coordinator/corpus/) | Input documents for the document analyst |

---

## Known limits

- Mock-mode decomposition is templated. Genuine topic-adaptive decomposition —
  the thing self-check 1 asks you to inspect for narrowing — only happens in
  live mode.
- Refinement is one extra round, capped by `RC_MAX_ROUNDS`. There is no
  convergence test; the coordinator decides once whether a gap is worth a
  second pass.
- A timed-out subagent is not retried. The deadline is treated as a deliberate
  budget, and retrying a hang spends it twice for the same likely outcome.
- Subagents never see each other's findings, so a contradiction between two of
  them is resolved only at synthesis, by the coordinator.
- Live mode is unexercised end to end: no API key was available in this
  environment. Its call shape is verified against `anthropic` 1.7.0, but the
  first real API call has not been made.
