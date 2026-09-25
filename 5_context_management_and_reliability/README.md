# Long-Conversation Support Agent with Escalation Logic

## Problem statement

Build a customer-support resolution agent, using MCP tools such as
`get_customer`, `lookup_order`, `process_refund` and `escalate_to_human`,
that stays reliable across long, multi-issue conversations and knows when to
hand off to a person instead of guessing.

What it has to prove:

- Conversation context managed so critical information survives long
  interactions
- Effective escalation and ambiguity-resolution patterns
- Programmatic enforcement where prompt instructions alone aren't reliable
  enough
- Error propagation that gives a coordinator or reviewer enough context to
  recover

The four tools are a real MCP server over stdio ([`server.py`](server.py)),
backed by a synthetic store of three customers and six orders. With no API key
the whole agent still runs, on a rule-based mock backend that goes through the
same context building, gates, trimming and error handling.

---

## Quick start

From this directory:

```bash
uv sync
uv run python main.py all
```

That runs all six suites and writes a report and a trace for each into
[`output/`](.):

```
  wrote output/context-<timestamp>.md
  wrote output/context-<timestamp>.json
  wrote output/escalation-<timestamp>.md
  wrote output/decompose-<timestamp>.md
  wrote output/enforcement-<timestamp>.md
  wrote output/errors-<timestamp>.md
  wrote output/mcp-<timestamp>.md
  ...
```

Each suite prints `PASS`/`FAIL` per check, and the command exits non-zero if
any check fails. Without a valid key the run prints
`Mode: mock (no ANTHROPIC_API_KEY ...)`, and every report opens with a banner
saying so.

To use the tools from Claude Code, start a session in this directory. The
project-scoped [`.mcp.json`](.mcp.json) registers the `support-desk` server,
and `/mcp` confirms it connected.

---

## How it works

```
 customer turn
      │
      ▼
 ┌───────────────────────────── request, rebuilt every turn ─────────────────────────────┐
 │ system[0]  static prompt: rules, escalation criteria, few-shots   (cache_control)     │
 │ system[1]  <case_facts>: ids, amounts, dates, refunds, tickets    (from tool results) │
 │ messages   <conversation_summary> + last N turns verbatim + this turn                 │
 └───────────────────────────────────────────────────────────────────────────────────────┘
      │  tool_use
      ▼
 agent.py  ── explicit "get me a human"? ──▶ block every tool except escalate_to_human; nudge once
      │
      ▼
 session.py (the only path to a handler, in-process or over MCP)
   1. process_refund? ── customer_id not verified by get_customer ──▶ prerequisite error, handler never runs
   2. run handler on a worker thread with a deadline ── overran ──▶ timeout error + partialResults
   3. retry budget: 2nd transient failure of the same call ──▶ isRetryable: false, "escalate"
   4. trim raw record to an allowlist ──▶ tool_result
   5. extract facts from the trimmed result ──▶ <case_facts>
```

### Case facts are never summarized

[`facts.py`](support_agent/facts.py). Transactional facts are extracted **by the
program, from successful tool results**, never from customer prose or model
output. They're rendered as their own system block on every request. Older
turns are folded into a narrative summary ([`memory.py`](support_agent/memory.py)),
but the summary sits in the message history and the facts sit in the system
prompt, so compaction can round, paraphrase or drop what it likes without
touching an amount.

The static prompt comes first and carries `cache_control`. The facts block
comes after it, so a changed fact never invalidates the cached prompt.

### Trimming is an allowlist

[`trimming.py`](support_agent/trimming.py). The upstream records are shaped
like real CRM and order-service responses: scan histories, warehouse bins,
audit logs, device fingerprints, marketing preferences. Each tool has an
explicit projection, so a field the upstream adds later stays out of context
until someone decides it belongs there. An unverified `get_customer` returns
only `verified: false`, with no `customer_id` the model could try to refund
against. A test checks that every field `facts.py` reads survives trimming.

### The refund prerequisite is program state

[`session.py`](support_agent/session.py). `process_refund` is rejected before
its handler runs unless `get_customer` returned `verified: true` for that exact
`customer_id` earlier in the session. The prompt says the same thing, but
nothing depends on the prompt being obeyed. The MCP server holds one
`ToolSession` per process (stdio is one client per process), so the gate holds
for any MCP client, not just this agent.

### Escalation: criteria in the prompt, a backstop in code

[`prompts.py`](support_agent/prompts.py) lists the four criteria
(`customer_request`, `policy_gap`, `policy_limit`, `unable_to_progress`), with
few-shot examples that include the counter-case: frustration alone is **not** a
reason to escalate. [`escalation.py`](support_agent/escalation.py) is the
backstop for the unambiguous case. When the customer explicitly asks for a
human, every other tool is blocked for that turn, and a turn that tries to end
without escalating gets one harness nudge. The detector needs a request verb
plus a human noun, so "the person at your store said..." does not fire it.

The backstop doesn't use forced `tool_choice`. That's rejected on some current
models, and the gate works the same on every model.

### Errors carry what a reviewer needs

Every failure is JSON with `errorCategory`, `failureType`, `isRetryable`,
`attempted` and `remediation`. A timeout also carries `partialResults` (what
the handler finished before the deadline), `completedSteps`, `incompleteStep`
and `attemptNumber`. Every `escalate_to_human` call has the case facts and the
last three error payloads attached by the program, so the human never gets a
bare ticket, however thin the model's summary is.

### Build steps and where each lives

| Build step | Where |
|---|---|
| Case-facts block carried in every turn, separate from summarized history | [`facts.py`](support_agent/facts.py), [`memory.py`](support_agent/memory.py), `SupportAgent.build_request` |
| Trim verbose tool output to relevant fields | [`trimming.py`](support_agent/trimming.py) |
| Block `process_refund` until `get_customer` returned a verified id | `ToolSession._require_verified` in [`session.py`](support_agent/session.py) |
| Escalation criteria with few-shot examples | `SYSTEM_PROMPT` in [`prompts.py`](support_agent/prompts.py); backstop in [`agent.py`](support_agent/agent.py) |
| Multi-concern message test | `decompose` suite, [`test_support_decompose.py`](tests/test_support_decompose.py) |
| Simulated tool timeout, structured context | `errors` and `mcp` suites, [`test_support_errors.py`](tests/test_support_errors.py) |

---

## Self-check

The three questions from the brief. Every figure below comes from a mock-mode
run (`uv run python main.py all`).

| # | Question | Answer | How to verify |
|---|---|---|---|
| 1 | After 15+ conversation turns, does the agent still have the correct order amount and customer details, or has it drifted or summarized them away? | **Correct, and the ablation shows why.** An 18-turn conversation with a 4-turn verbatim window, so 14 turns are compacted into the summary, including the turn that looked `ORD-5521` up. At turns 16-18 the agent states **$249.99** and **2026-09-02** exactly, gives the account email exactly, and refunds exactly **249.99** without re-asking for identity. It never re-fetches the order. The same run **with the case-facts block removed** fails all 3 probes: it says "about $250" and "in September 2026" (the summary's paraphrase), no longer knows the email, and asks the customer to verify again. Trimming keeps **1,726 of 11,270** raw tool-output chars (15.3%). **Caveat:** in mock mode the agent is rule-based and the summarizer's lossiness is scripted to imitate paraphrase, so this shows the facts block works, not how much a real model drifts without it. TODO: live-mode drift rate with and without case facts. Requires a live-mode run. | `uv run python main.py context`, then read *Ablation*; `uv run pytest -k "case_facts or drifts"` |
| 2 | Does it escalate immediately when a customer explicitly asks for a human, without first attempting to solve it anyway? | **Yes: `escalate_to_human` is the first and only tool call**, both as the opening message and mid-conversation, and the `lookup_order` handler runs 0 times. It's also enforced: a scripted backend that ignores the rule and calls `lookup_order` first is blocked (`errorCategory: "escalation"`), gets one nudge, and escalates. The counter-cases hold too. Frustration without a request is answered, not escalated, and "the person at your store said..." is not treated as a request. The other criteria are covered as well: a price adjustment escalates as `policy_gap` with no refund attempted, and a refund above the auto-approval limit escalates as `policy_limit`. An ambiguous "refund the kettle" gets a question naming both kettle orders. **Caveat:** in mock mode, following the prompt is scripted. What's proven is that the backstop catches a model that doesn't follow it. TODO: live-mode rate of escalating before any other tool call. Requires a live-mode run. | `uv run python main.py escalation`; `uv run pytest -k "escalat or detector"` |
| 3 | Does it refuse to process a refund before identity is verified, every time, not just usually? | **Yes, by construction.** The gate is program state checked before the handler runs, so it doesn't depend on the model. **2,000** seeded random call sequences (8,844 calls, 4,420 refund attempts) with right and wrong emails and postal codes and other customers' ids: **4,290 blocked, 130 reached the handler, all 130 for a customer verified earlier in that session, 0 bypasses.** A reckless backend that refunds on every turn is blocked with no verification, with a wrong postal code, and with a *different* customer verified, and allowed only once the right customer is verified. The same gate returns a structured `prerequisite` error over MCP stdio. | `uv run python main.py enforcement`; `uv run pytest -k "gate or fuzz or prerequisite or reckless"` |

The timeout build step is checked the same way (`uv run python main.py errors`).
`lookup_order` times out on the shipment step. The first payload is
`transient` / `timeout` / `isRetryable: true` with
`attempted: "lookup_order(order_id='ORD-5530')"` and the order header in
`partialResults`. The retry times out too, and its payload flips to
`isRetryable: false`. The agent then escalates as `unable_to_progress`, with
both payloads and the case facts in the handoff. The reply tells the customer
what did come back (placed date, total, status) and doesn't invent a tracking
scan. A single timeout is retried once and recovers without escalating.

---

## Commands

All commands run from this directory.

| Command | What it does |
|---|---|
| `uv run python main.py all` | Run every suite (default) |
| `uv run python main.py context` | 18-turn conversation, case-facts ablation, trimming and request-size tables |
| `uv run python main.py escalation` | Explicit requests, the backstop, counter-cases, policy gap/limit, ambiguity |
| `uv run python main.py decompose` | Messages with two or three concerns |
| `uv run python main.py enforcement` | Refund prerequisite: scripted, reckless backend, random-sequence fuzz |
| `uv run python main.py errors` | Tool timeouts, retry budget, escalation handoff |
| `uv run python main.py mcp` | The prerequisite and timeout payloads over a real MCP stdio client |
| `... --mode mock` / `--mode live` | Force the backend; `live` fails loudly without a working key |
| `... --keep-recent 2` | Customer turns kept verbatim before summarizing |
| `... --tool-timeout 1.5` | Per-tool-call deadline, in seconds |
| `... --fuzz 10000` | Random call sequences in the enforcement fuzz |
| `... --print` | Also echo reports to the terminal |
| `... --quiet` | Suppress progress output |
| `uv run pytest 5_context_management_and_reliability/tests` | Run the 57 tests (from the repo root) |

`uv run python server.py` starts the MCP server on stdio. It speaks JSON-RPC,
so running it by hand looks like a hang. Use `main.py mcp` to exercise it.
Set `SUPPORT_SIMULATE_TIMEOUT=lookup_order` (and optionally
`SUPPORT_SIMULATE_TIMES`, `SUPPORT_TOOL_TIMEOUT`) to make it time out for any
MCP client.

---

## Live vs mock mode

Mode is resolved once at startup by [`settings.py`](support_agent/settings.py):

1. `--mode mock` always gives mock.
2. With no `ANTHROPIC_API_KEY`, the mode is mock.
3. If a key is present, it's validated with `GET /v1/models/{id}` (zero tokens).
   A 401, 403 or 404, or a connection failure, falls back to mock and prints
   the reason.
4. `--mode live` turns each of those fallbacks into a hard error.

| | live | mock |
|---|---|---|
| Agent turns | Claude, with the four tools | rule-based agent in [`mock_agent.py`](support_agent/mock_agent.py) |
| Summary of older turns | Claude, at low effort | deterministic paraphrase: rounds amounts, months for dates, drops emails |
| Case facts, trimming, refund gate, deadlines, retry budget, escalation backstop | identical | identical |
| Adversarial backends (reckless refunder, prompt-ignorer) and the fuzz | scripted in both modes | scripted in both modes |

**How the mock is kept honest.** It's stateless. Every decision comes from the
request dict alone: the case-facts block if present, the tool results still in
the verbatim window, and the summary. If a fact isn't in the request, the mock
doesn't know it. A test sends the same turn-16 request with and without the
facts block and checks the amount appears only with it. That's what makes the
ablation a measurement of context management rather than of the mock.

Live calls use `claude-opus-5` with adaptive thinking at effort `medium`, and
server-side refusal fallback (`fallbacks: "default"`).

Configuration lives in the project `.env`: `SA_MODEL`, `SA_EFFORT`,
`SA_REQUEST_TIMEOUT`, `SA_REFUSAL_FALLBACK`, `SA_TOOL_TIMEOUT`,
`SA_KEEP_RECENT_TURNS`, and `SA_MAX_TOOL_ROUNDS`.

---

## Layout

| File | Role |
|---|---|
| [`main.py`](main.py) | CLI; writes every run into `output/` |
| [`server.py`](server.py) | stdio entry point; the command `.mcp.json` runs |
| [`.mcp.json`](.mcp.json) | Project-scoped server config, settings by env expansion |
| [`settings.py`](support_agent/settings.py) | `.env` loading, live/mock resolution, key validation |
| [`agent.py`](support_agent/agent.py) | Per-turn loop, request building, escalation gate and nudge |
| [`session.py`](support_agent/session.py) | Refund prerequisite, deadlines, retry budget, trimming, fact extraction |
| [`facts.py`](support_agent/facts.py) | The case-facts block |
| [`memory.py`](support_agent/memory.py) | Verbatim window, summary, the mock summarizer |
| [`trimming.py`](support_agent/trimming.py) | Per-tool field allowlists, partial-result trimming |
| [`handlers.py`](support_agent/handlers.py) | The four tools, fault injection, progress for partial results |
| [`data.py`](support_agent/data.py) | Synthetic customers and orders, verbose raw records, refund policy |
| [`prompts.py`](support_agent/prompts.py) | System prompt, escalation criteria and few-shots, tool schemas |
| [`escalation.py`](support_agent/escalation.py) | Explicit-human-request detector |
| [`errors.py`](support_agent/errors.py) | `SupportToolError` (category, `isRetryable`, `attempted`, `partialResults`) |
| [`mcp_server.py`](support_agent/mcp_server.py) | MCP wiring: `Annotated` args, `ToolError` |
| [`probe_client.py`](support_agent/probe_client.py) | Real MCP client used by the `mcp` suite and tests |
| [`backend.py`](support_agent/backend.py) | `LiveBackend`, `LiveSummarizer`, `MockBackend` |
| [`mock_agent.py`](support_agent/mock_agent.py) | The stateless rule-based agent |
| [`adversarial.py`](support_agent/adversarial.py) | Backends that break the rules on purpose |
| [`enforcement.py`](support_agent/enforcement.py) | Random-sequence fuzz of the refund prerequisite |
| [`scenarios.py`](support_agent/scenarios.py) | Conversations and the checks run against them |
| [`report.py`](support_agent/report.py) | Markdown reports and JSON traces |
| [`tests/`](tests/) | 57 tests, none needing a key or the network |
| `output/` | Reports and traces, written at runtime (e.g. `output/context-<timestamp>.md`) |

---

## Known limits

- Live mode has not been run end to end yet. The request shape follows the
  Anthropic Python SDK 1.x (adaptive thinking, `output_config.effort`, the
  refusal-fallback beta), but the live classes are imported lazily and no test
  exercises them, since tests never need a key or the network. TODO: live-mode
  results for every self-check row. Requires a live-mode run.
- Several checks score a live model's prose with substring markers (`249.99`,
  `2026-09-02` or `September 2`, order ids). A correct answer phrased some other
  way would be marked as a fail. The tool-call checks (first tool, handler
  invocation counts, refund amounts) don't have this problem.
- The check "probes answered from case facts, not by re-fetching" is strict on
  purpose. A live model that re-fetches the order would fail it, even though
  re-fetching isn't wrong. It would just hide whether the facts block did the
  work.
- Multi-concern decomposition in mock mode is a regex clause splitter. In live
  mode it depends on the prompt, and nothing in code enforces that every
  concern gets answered. The check only verifies it afterwards.
- The explicit-request detector is English-only and deliberately narrow. It
  catches the unambiguous case; anything subtler relies on the prompt.
- The summary is prepended to the first retained message, so older messages
  change as the window slides. Models that enforce append-only history for
  replayed thinking blocks would need the summary moved into a separate
  message instead. TODO.
- A timed-out handler thread is abandoned, not killed. The fault injection
  wakes on cancellation and exits before mutating any state (a test checks a
  timed-out refund moves no money), but a real upstream call would need its
  own cancellation or an idempotency key.
- The data is an in-memory fixture with a fixed policy date, so refund-window
  decisions are reproducible but never change.
