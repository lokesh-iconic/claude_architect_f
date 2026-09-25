# Capstone: Production-Grade Customer Support Resolution Agent

## Problem statement

A customer-support resolution agent that handles high-ambiguity requests
(returns, billing disputes, account issues) through MCP tools, aims for
first-contact resolution, and knows when to escalate. The five domain
assignments each built one capability in isolation. This capstone builds
the same agent as one system, delivered the way a real engagement needs:

- configured through a proper Claude Code workflow
- every customer-facing action schema-validated
- backed by a one-page solution brief a client could be handed

The four tools (`get_customer`, `lookup_order`, `process_refund`,
`escalate_to_human`) are a real MCP server over stdio
([`server.py`](server.py)), backed by a synthetic store of three customers
and six orders. With no API key the whole agent still runs, on a rule-based
mock backend. The mock goes through the same loop, gates, validation,
ledger, case facts and error handling.

Deliverables:

| | |
|---|---|
| Project CLAUDE.md (architecture, tool contracts, escalation policy) | [`CLAUDE.md`](CLAUDE.md) |
| Slash command that runs the conversation set | [`../.claude/commands/run-scenarios.md`](../.claude/commands/run-scenarios.md) (`/run-scenarios`) |
| Architecture plan, written before the loop | [`PLAN.md`](PLAN.md) |
| One-page solution design brief | [`SOLUTION_BRIEF.md`](SOLUTION_BRIEF.md) |

---

## Quick start

From this directory:

```bash
uv sync
uv run python main.py all
```

That runs the conversation set and the five per-domain suites, and writes a
report and a trace for each into [`output/`](.):

```
  wrote output/scenarios-<timestamp>.md
  wrote output/loop-<timestamp>.md
  wrote output/tools-<timestamp>.md
  wrote output/actions-<timestamp>.md
  wrote output/context-<timestamp>.md
  wrote output/workflow-<timestamp>.md
  ...
```

In Claude Code, from the repo root:

```
/run-scenarios                       # every scenario, PASS/FAIL per scenario
/run-scenarios --only tool_timeout   # just one
/mcp                                 # from this directory: the support-resolution server connects
```

---

## How to run: mock and live

Every command below runs from this directory. One-time setup, from the repo root:

```bash
uv sync
cp .env.example .env      # only needed for live mode; .env is git-ignored
```

### Mock mode (offline, no API key)

```bash
uv run python main.py all --mode mock
uv run python main.py scenarios --mode mock --only long_conversation_three_issues
```

The agent is the stateless rule-based stand-in in `backends/mock_agent.py`. The
loop, identity gate, record validation, ledger, case facts and handoffs are
the real code. From Claude Code at the repo root, `/run-scenarios` runs the
same conversation set and reports PASS/FAIL per scenario.

### Live mode (real Claude API)

1. Add your key to the repo-root `.env`: `ANTHROPIC_API_KEY=<your key>`. Never put it in a committed file.
2. Run:

```bash
uv run python main.py scenarios --mode live      # the conversation set against Claude
uv run python main.py all --mode live            # plus every per-domain suite
```

Or, in Claude Code: `/run-scenarios --mode live`. Agent turns call
`claude-opus-5` (`RA_MODEL`) with `strict` tools and adaptive thinking. The
stop_reason matrix, fuzzes, adversarial backends and the `workflow` checks
exercise the program directly, so they behave the same in both modes. To use
the tools from Claude Code, start a session in this directory.
[`.mcp.json`](.mcp.json) registers the `support-resolution` server.

### Auto mode (the default)

Leave `--mode` off and the module picks for you: live if `ANTHROPIC_API_KEY` is
set and passes a zero-token `GET /v1/models/{id}` check, otherwise mock. The
first line of output always says which mode ran and why.

### Tests

```bash
uv run pytest capstone/tests      # from the repo root; always offline, no key needed
```

---

## How it works

```
 customer turn
      │
      ▼
 agent.py ── request rebuilt every turn ───────────────────────────────────────────────────────────
 │ system[0]  static prompt: rules, action-record rules, escalation criteria, 3 worked examples (cached)
 │ system[1]  <case_facts>: ids, amounts, dates, refunds, tickets          (extracted from tool results)
 │ messages   <conversation_summary> + last 4 turns verbatim + this turn
 └─ stop_reason: tool_use → run tools, continue │ end_turn/stop_sequence → reply (nudge once if a
    requested handoff is missing) │ pause_turn → replay, continue │ max_tokens → discard, retry once
    │ refusal / unknown / round cap → the program files a handoff
      │ tool_use
      ▼
 session.py  ToolSession.call ── the only path to a handler: agent, MCP client, or the system
   1. scope        tool outside the four                        → validation / tool_out_of_scope
   2. identity     process_refund, customer not verified        → prerequisite / identity_not_verified
   3. contract     full schema + case-fact checks on the record → validation / invalid_action_record
                   (every issue listed; retryable ×3, or stop at once if the same issues come back)
   4. handler      deadline; transient retry budget             → transient / timeout + partialResults
   5. policy       window, auto-approval limit, ownership       → policy / permission
   6. trim → extract case facts → ledger record for process_refund / escalate_to_human
```

### Every customer-facing action is a validated record

[`schema.py`](resolution_agent/tools/schema.py), [`actions.py`](resolution_agent/tools/actions.py).
`process_refund` and `escalate_to_human` are the only tools that move money
or change a case. Their input is the record of what was done and why:

- **Refund record:** amount, `refund_type`, `reason_code`, the customer's
  words, and a justification citing the facts.
- **Handoff record:** `customer_id`, `root_cause`, `recommended_action`,
  `customer_request`, `order_ids` and `actions_taken`.

The API receives these schemas with `strict: true`. Strict mode doesn't
accept `minLength`, `pattern` or `exclusiveMinimum`, so those keywords are
stripped from the API copy and enforced client-side instead. The case-fact
checks run next, before the handler:

- the order was fetched in this conversation
- the amount is no more than the refundable remainder
- `refund_type` matches the amount
- the handoff's `customer_id` is the verified customer (or null if nobody
  is)
- the root cause and next step aren't boilerplate like "please review"

A rejected record comes back as one `validation` error listing every issue.
Nothing has run at that point, so the model corrects the record and calls
again. That's the validation-retry loop.

A record that passed and reached its handler goes into `session.ledger`,
along with any rejected attempts before it, whatever the handler then
decided. "Handler runs equal ledger records" is checked across the scenario
set and under a fuzz.

### The loop, and the handoff protocol

[`agent.py`](resolution_agent/conversation/agent.py). Each `stop_reason` has its own
branch. A truncated `max_tokens` response is discarded unexecuted, since
it may hold half a refund, and retried once. A turn that can't finish
(refusal, unknown stop reason, round cap, the model ignoring a request for a
human after the nudge, handoff validation exhausted) is handed off by the
program through the same validator (`authored_by: system`). The customer is
never told a colleague will pick it up unless a ticket exists. Every ticket
also carries the case facts and the last three error payloads, attached by
the session.

### Case facts, escalation, trimming

Carried over from module 5 and wired into the new loop.
[`facts.py`](resolution_agent/conversation/facts.py) extracts ids, amounts and dates from
successful tool results only. They go into their own system block, which
summarization ([`memory.py`](resolution_agent/conversation/memory.py)) never touches.

[`escalation.py`](resolution_agent/conversation/escalation.py) detects an explicit
request for a human. When it fires, every other tool is blocked for that
turn. [`trimming.py`](resolution_agent/tools/trimming.py) cuts verbose upstream
records down to an allowlist of fields.

### Few-shot examples for the most ambiguous requests

[`few_shot.py`](resolution_agent/conversation/few_shot.py) has three examples, each with
its reasoning:

1. **Defect refund with an amount stated.** Contrasted with a price-drop
   request that looks the same but is a policy gap.
2. **A duplicate charge the order record doesn't show.** No refund. The
   handoff says what to check at the payment processor.
3. **One reference matching two orders.** A clarifying question, no action.

The examples use fictional ids that aren't in the data store, and each one
passes the agent's own validator.

### Build steps and where each lives

| Domain | Build step | Where |
|---|---|---|
| D3 | Project CLAUDE.md: architecture, tool contracts, escalation policy | [`CLAUDE.md`](CLAUDE.md); `main.py workflow` cross-checks it against the code |
| D3 | Slash command running the conversation set, PASS/FAIL per scenario | [`/run-scenarios`](../.claude/commands/run-scenarios.md) → `main.py scenarios` |
| D3 | Plan the architecture before the loop | [`PLAN.md`](PLAN.md) |
| D2 | Four MCP tools with descriptions that separate neighbours | [`schema.py`](resolution_agent/tools/schema.py) `DESCRIPTIONS`, [`mcp_server.py`](resolution_agent/tools/mcp_server.py) |
| D2 | Structured errors: `errorCategory`, `isRetryable`, `description` | [`errors.py`](resolution_agent/tools/errors.py), every path in [`session.py`](resolution_agent/tools/session.py) |
| D2 | Tools scoped to the workflow | four tools only; `tool_out_of_scope`; one server in [`.mcp.json`](.mcp.json) |
| D1 | Agentic loop with correct `stop_reason` handling | `SupportAgent.handle` in [`agent.py`](resolution_agent/conversation/agent.py) |
| D1 | `process_refund` blocked in code until `get_customer` verified the id | `ToolSession._require_verified` |
| D1 | Structured handoff: customer id, root cause, recommended action | `ESCALATE_TO_HUMAN` schema + `check_handoff` + `SupportAgent.system_handoff` |
| D4 | JSON schema (via tool_use) for every money or account-state action | `PROCESS_REFUND`, `ESCALATE_TO_HUMAN`, `api_tool_specs()` |
| D4 | Few-shot examples for the most ambiguous request types | [`few_shot.py`](resolution_agent/conversation/few_shot.py), rendered into [`prompts.py`](resolution_agent/conversation/prompts.py) |
| D4 | Validation-retry before the tool call, not after | `ToolSession._check_contract` + retry budget |
| D5 | Case facts in a persistent block separate from the summary | [`facts.py`](resolution_agent/conversation/facts.py), `SupportAgent.build_request` |
| D5 | Escalation criteria with few-shots; explicit request honoured immediately | `SYSTEM_PROMPT`; gate and backstop in `agent.py` |
| D5 | 20+ turns, three unrelated issues, no drift from turn 2 to turn 20 | `THREE_ISSUES` + `facts_drift` in [`scenarios.py`](resolution_agent/evaluation/scenarios.py) |

---

## Self-check

The five questions from the brief. Every figure comes from a mock-mode run
(`uv run python main.py all`).

| # | Question | Answer | How to verify |
|---|---|---|---|
| 1 | Does the finished agent still pass every individual-domain check from the five domain assignments, or did integrating them break something? | **Yes, all pass.** Every behaviour the module 5 self-check covered, plus the D1, D2 and D4 build steps, now run against the *integrated* agent: 15 conversation scenarios (29 checks) and 38 suite checks, **0 failures**. Integration did break two things along the way, and both were caught by these checks. (a) Schema validation initially hid case-fact problems behind the first missing field, so a malformed refund came back with 1 issue instead of 2. Fixed: every issue is reported in one round trip. (b) A handoff test sent "get me a human", which the escalation gate correctly treated as an explicit request, blocking verification. The *test* was wrong, not the gate. The module 1–5 test suites were rerun unchanged and all pass (193 tests). The module 1–4 systems themselves (research coordinator, issue tracker, CI review, invoice extractor) are separate products and aren't part of this agent. | `uv run python main.py all`; `uv run pytest` from the repo root |
| 2 | Can someone else open your CLAUDE.md and understand the system without reading the code first? | **It's written for that, and it can't silently fall behind the code.** It has a call-path diagram, a table per tool (input, returns, whether it changes state), the full refund and handoff record fields, the error-category table with retryability, and the escalation policy with which parts are enforced in code. `main.py workflow` checks that it names all **27** tools, record fields, escalation reasons and error fields the code defines. **Caveat:** whether a newcomer actually understands it is a human judgement no check can make. TODO: have a teammate read it cold and note what they had to open the code for. | `uv run python main.py workflow` |
| 3 | Does every refund or account-change action produce a validated, schema-compliant record, with no exceptions? | **Yes, by construction and by measurement.** The ledger is written inside the one call path, after validation and before the handler's result is known, so a handler can't run without a record. Across the 15 scenarios: **17 action handler runs, 17 ledger records, 0 invalid** (10 agent handoffs, 1 system handoff, 4 executed refunds, 2 refunds logged as `not_executed` by policy). Under fuzz: 2,000 random sequences, **135 handler runs = 135 ledger records**, 0 invalid, with **360** malformed refunds rejected before running. Account changes: there is deliberately no account-mutation tool. They go to a person through `escalate_to_human` (`policy_gap`), so they produce a validated handoff record too. | `uv run python main.py actions`; `uv run pytest -k "ledger or no_exceptions"` |
| 4 | Does a 20+ turn, multi-issue conversation hold up without losing or corrupting earlier case facts? | **Yes.** 22 turns, three unrelated issues: a partial refund for a damaged part, a billing question on another order, and an account-email change handed off. 18 turns are compacted into the summary. The facts block of **every** request from turn 3 to 22 (20 requests) carried the turn-2 customer and ORD-5521 facts unchanged. The only change was the legitimate one: `refunded_amount` became 30.0 after the turn-4 refund. At turns 20–22 the agent states **$249.99**, **2026-09-02**, the exact email and **RF-9001 / $30.00**, without re-fetching the order. The same run **without** the facts block loses the email and the refund reference. Trimming keeps **1,531 of 11,562** raw tool-output chars (13.2%). **Caveat:** mock mode. The summarizer's lossiness is scripted, so this shows the facts block works, not how much a real model drifts without it. TODO: live-mode drift rate with and without case facts. Requires a live-mode run. | `uv run python main.py context`; `uv run pytest -k "drift or probes"` |
| 5 | Would you actually hand your one-page brief to a client, as-is? | **Yes, with the status line it carries.** [`SOLUTION_BRIEF.md`](SOLUTION_BRIEF.md) is 652 words and covers the architecture diagram, five tradeoffs with the cost accepted for each, the escalation policy, and one open risk with concrete mitigations: a timed-out refund may still have committed upstream, so retrying could double-refund, and the fix is idempotency keys plus a status check. Every figure in it comes from these reports. The one claim that turned out to be wrong in a draft ("about $250") was corrected to what the ablation actually showed. It says plainly that live-model rates are not yet measured. | `uv run python main.py workflow` (length and sections); read it |

The domain build steps are checked the same way:

- `main.py loop`: every stop_reason branch, the reckless-refund backend, a
  2,000-sequence refund-gate fuzz (4,527 refund attempts, 117 reached the
  handler, all verified, 0 bypasses), and every ticket's handoff record.
- `main.py tools`: scope, description boundaries, near-miss tool selection,
  one error payload per category, and the same gate, validation and timeout
  over MCP stdio.

---

## Commands

All commands run from this directory.

| Command | What it does |
|---|---|
| `uv run python main.py all` | Everything below (default) |
| `uv run python main.py scenarios` | The test conversation set, PASS/FAIL per scenario. What `/run-scenarios` runs |
| `... scenarios --only NAME` | One scenario (repeatable). Names: `refund_happy_path`, `refund_before_verification`, `explicit_human_first_message`, `explicit_human_mid_conversation`, `escalation_backstop`, `frustration_is_not_escalation`, `partial_refund_for_damage`, `price_drop_policy_gap`, `duplicate_charge_dispute`, `ambiguous_order_reference`, `policy_rejections`, `account_change_to_human`, `three_concerns_one_message`, `tool_timeout`, `long_conversation_three_issues` |
| `uv run python main.py loop` | D1: stop_reason matrix, refund gate and fuzz, handoff protocol |
| `uv run python main.py tools` | D2: scope, descriptions, selection, error payloads, MCP stdio |
| `uv run python main.py actions` | D4: strict/API schemas, few-shots, validation-retry, ledger invariant |
| `uv run python main.py context` | D5: 22-turn drift table, ablation, trimming |
| `uv run python main.py workflow` | D3: CLAUDE.md vs code, slash command, plan, brief, repo wiring |
| `... --mode mock` / `--mode live` | Force the backend; `live` fails loudly without a working key |
| `... --keep-recent 2` / `--tool-timeout 1.5` / `--fuzz 10000` | Verbatim window, per-tool deadline, fuzz size |
| `... --print` / `--quiet` | Echo reports / suppress progress |
| `uv run pytest capstone/tests` | Run the 76 tests (from the repo root) |

`uv run python server.py` starts the MCP server on stdio. It speaks
JSON-RPC, so running it by hand looks like a hang. `SUPPORT_SIMULATE_TIMEOUT`,
`SUPPORT_SIMULATE_TIMES` and `SUPPORT_TOOL_TIMEOUT` inject timeouts for any
MCP client.

---

## Live vs mock mode

Mode is resolved once at startup by [`settings.py`](resolution_agent/config/settings.py):
`--mode mock` always gives mock. `auto` validates `ANTHROPIC_API_KEY` with a
zero-token `GET /v1/models/{id}`, and falls back to mock with the reason
printed if there is no key or the check fails. `--mode live` turns every
fallback into a hard error.

| | live | mock |
|---|---|---|
| Agent turns | `claude-opus-5`, adaptive thinking, effort `medium`, `strict` tools, server-side refusal fallback | rule-based agent in [`mock_agent.py`](resolution_agent/backends/mock_agent.py) |
| Summary of older turns | Claude, low effort | deterministic paraphrase (rounds amounts, months for dates, drops emails) |
| Loop, gates, validation, ledger, case facts, trimming, deadlines, handoffs | identical | identical |
| Adversarial backends, stop_reason matrix, fuzzes | scripted in both modes | scripted in both modes |

**How the mock is kept honest.** It's stateless: every decision comes from
the request dict alone. If a fact isn't in the request, the mock doesn't
know it. That's why the ablation measures context management, not the
mock. It also isn't trusted for the "every time" claims. Those come from
backends that break the rules on purpose, and from fuzzing the call path
directly.

Configuration lives in the project `.env`: `RA_MODEL`, `RA_EFFORT`,
`RA_REQUEST_TIMEOUT`, `RA_REFUSAL_FALLBACK`, `RA_TOOL_TIMEOUT`,
`RA_KEEP_RECENT_TURNS`, `RA_MAX_TOOL_ROUNDS`, `RA_MAX_ACTION_ATTEMPTS`.

---

## Folder structure

```text
capstone/
├── CLAUDE.md                    Project CLAUDE.md: architecture, tool contracts, escalation policy
├── PLAN.md                      The architecture plan written before the loop
├── SOLUTION_BRIEF.md            One-page client brief
├── main.py                      CLI; writes every run into output/
├── server.py                    stdio entry point; the command .mcp.json runs
├── .mcp.json                    Project-scoped server config
├── resolution_agent/
│   ├── config/
│   │   └── settings.py          .env loading, live/mock resolution, key validation
│   ├── conversation/
│   │   ├── agent.py             The loop: stop_reason branches, escalation gate, system handoff
│   │   ├── prompts.py           System prompt, escalation criteria, harness notices
│   │   ├── few_shot.py          Three reasoned examples for the most ambiguous requests
│   │   ├── escalation.py        Explicit-human-request detector
│   │   ├── memory.py            Verbatim window, summary, the mock summarizer
│   │   └── facts.py             The case-facts block
│   ├── tools/
│   │   ├── session.py           The one call path: scope, identity, contract, deadline, ledger
│   │   ├── schema.py            Tool contracts, API-safe copies, the validator
│   │   ├── actions.py           Case-fact checks on action records; ActionRecord
│   │   ├── handlers.py          The four tools and fault injection
│   │   ├── trimming.py          Per-tool output allowlists
│   │   ├── errors.py            SupportToolError
│   │   ├── data.py              Synthetic customers, orders and refund policy
│   │   └── mcp_server.py        MCP wiring: Annotated args, ToolError
│   ├── backends/
│   │   ├── backend.py           LiveBackend, LiveSummarizer, MockBackend
│   │   ├── mock_agent.py        The stateless rule-based agent
│   │   └── adversarial.py       Rule-breaking and stop_reason backends
│   ├── evaluation/
│   │   ├── scenarios.py         The test conversation set (what /run-scenarios runs)
│   │   ├── suites.py            Per-domain suites: loop, tools, actions, context
│   │   ├── enforcement.py       Refund-gate and ledger fuzz
│   │   ├── config_check.py      Workflow checks: CLAUDE.md vs code, slash command, brief
│   │   ├── probe_client.py      Real MCP stdio client for the checks
│   │   └── results.py           Check / Transcript / SuiteResult types
│   └── reporting/
│       └── report.py            Markdown reports and JSON traces
├── tests/                       76 tests, none needing a key or the network
└── output/                      Reports and traces, written at runtime (git-ignored)
```

| Folder | Responsibility |
|---|---|
| [`resolution_agent/config/`](resolution_agent/config/) | Settings and live/mock mode resolution |
| [`resolution_agent/conversation/`](resolution_agent/conversation/) | The loop and what it carries between turns: prompt, few-shots, memory, case facts |
| [`resolution_agent/tools/`](resolution_agent/tools/) | The one call path, tool contracts, action-record validation, the MCP server |
| [`resolution_agent/backends/`](resolution_agent/backends/) | Live and mock model backends, and adversarial backends |
| [`resolution_agent/evaluation/`](resolution_agent/evaluation/) | Scenario set, per-domain suites, fuzzes, workflow checks |
| [`resolution_agent/reporting/`](resolution_agent/reporting/) | Report and trace rendering |

The `/run-scenarios` slash command lives at
[`../.claude/commands/run-scenarios.md`](../.claude/commands/run-scenarios.md),
because Claude Code only discovers commands at the repo root.

---

## Known limits

- **Live mode has not been run end to end.** The request shape follows the
  Anthropic Python SDK 1.x (adaptive thinking, `output_config.effort`,
  `strict` tools, the refusal-fallback beta), but no test exercises it,
  since tests never need a key. Every "how often does Claude…" question is
  open. TODO: live-mode results for the scenario set. Requires a live-mode
  run.
- **Plan mode can't be evidenced by a repository.** `PLAN.md` is the
  artifact of the design pass; the repo can't prove the session that
  produced it was in plan mode.
- **A timed-out refund is reported as retryable.** That's safe against the
  test double, which never commits after a timeout, but not against a real
  payment API. This is the open risk in the brief. The fix is an
  idempotency key (the ledger's action id) plus a status check before any
  retry.
- The generic-text detector for handoff fields is a pattern list plus a
  four-word minimum. It stops "please review", not a fluent but useless
  sentence.
- Several scenario checks score prose with substring markers (`249.99`,
  `RF-9001`, "which would you prefer"). A correct live answer phrased
  differently would be marked as a fail. The tool-call and ledger checks
  don't have this problem.
- Mock decomposition and intent detection are regexes. They're scripted
  so the harness can be exercised, and aren't evidence of how the model
  reads a request.
- `lookup_order` doesn't check ownership (carried over from module 5), so an
  unverified caller can read an order's status and total. Refunds are gated,
  reads aren't.
- The summary is prepended to the first retained message, so older messages
  change as the window slides. Models that enforce append-only history for
  replayed thinking blocks would need the summary moved into its own
  message. TODO.
