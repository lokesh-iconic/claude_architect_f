# Issue-Tracker MCP Server

## Problem statement

Engineers want an agent that can explore an unfamiliar codebase using Claude
Code's built-in tools alongside a custom MCP server exposing the team's
internal issue tracker. Build the server, integrate it, and prove Claude
reliably picks the right tool even when two tools look similar.

What it has to prove:

- Tool interfaces described precisely enough to avoid selection errors
- Structured, categorized error responses for MCP tools
- MCP servers configured at the correct scope (project vs. personal)
- Tools distributed so an agent isn't given more than it needs

The server is a real MCP server over stdio. The harnesses that measure its
tool design run with no API key; only the live tool-selection measurement
needs one.

---

## Quick start

From this directory:

```bash
uv sync
uv run python main.py all
```

That runs all four harnesses and writes their reports into [`output/`](.):

```
  wrote output/selection-20260921-171859.md
  wrote output/resources-20260921-171859.md
  wrote output/errors-20260921-171913.md
  wrote output/config-20260921-171915.md
```

To use the server from Claude Code, start a session in this directory — the
project-scoped [`.mcp.json`](.mcp.json) is picked up automatically — and run
`/mcp` to confirm it connected.

> **Prerequisite: `uv` must be on your PATH.** `.mcp.json` spawns the server
> with `uv run python server.py`. If `uv` is installed but not on PATH (the
> common case on Windows, where it lands in `%USERPROFILE%\.local\bin`), the
> spawn fails with a bare `FileNotFoundError` and Claude Code reports the
> server as failed with no explanation. `uv run python main.py doctor` checks
> this explicitly and prints the exact fix — run it first.

---

## What the server exposes

Four tools over stdio, backed by a sample tracker of nine issues across three
projects, all linked to real files in [`sample_service/`](sample_service/).

| Tool | Purpose | Not for |
|---|---|---|
| `search_issues` | Full-text over titles, descriptions, labels | Paths, or a key you already have |
| `find_issues_for_path` | Structured lookup on the tracker's path index | Free-text topics |
| `get_issue` | One issue in full, with comments and linked paths | Finding issues |
| `list_sprint_board` | A whole sprint grouped by status | Topic or path filters |

Three resources: `issues://catalog` (the shape of the tracker in one read),
`issues://paths` (exactly the strings `find_issues_for_path` matches), and
`issues://tool-guide`.

### The deliberate overlap

`search_issues` and `find_issues_for_path` both take a string and both return
a list of issues. "Which issues involve the pricing code?" is a plausible
request for either — and picking wrong fails *silently*, because text search
returns a confident, empty-ish result rather than an error. Most issues never
mention their own file paths, so searching `session.py` finds nothing while
two issues are genuinely linked to it.

[`toolspecs.py`](issue_tracker/toolspecs.py) keeps both description
generations side by side with **identical schemas**, so the evaluation
isolates exactly one variable: the prose.

---

## Self-check

| # | Question | Answer | How to verify |
|---|---|---|---|
| 1 | Does the agent reliably choose correctly between the two similar tools, across repeated tries? | **Yes, after two rewrites.** v1 descriptions score **20% overall / 0% on the overlapping pair**; v2 scores **100% / 100%**, and every case is consistent across repeated trials. **Caveat:** offline this is scored by a description-discriminability proxy, not by Claude — see below. | `uv run python main.py selection` |
| 2 | Does a simulated tool failure return structured metadata the agent can act on, rather than a generic error? | **Yes.** All six probed failures come back over stdio with `isError: true` and a JSON payload carrying `errorCategory` (transient/validation/permission), `isRetryable`, `attempted`, and `remediation`. Getting this right required a specific fix: a plain raised exception is swallowed by the SDK into the string `"Error executing tool <name>"`, destroying the payload. Only the SDK's own `ToolError` passes its message through. | `uv run python main.py errors` |
| 3 | If you used MCP resources, do they measurably reduce exploratory tool calls? | **Yes — 10 tool calls to 0** across four questions. One question (`projects`) is not merely slower without the resource but *unanswerable* by probing: a closed issue outside the current sprint never surfaces through any tool. **Caveat:** this is a call-count analysis of the information architecture, not a measurement of model behaviour — the probe plan is authored, though both conditions run the same plan against the real handlers. | `uv run python main.py resources` |

### Why the tool-selection caveat matters

Without an API key the harness scores each tool by how *discriminating* its
description is for a given prompt: prompt terms are weighted by how few tools
mention them, so a word in every description counts for nothing and a word
unique to one decides the match. That measures whether the descriptions carry
the vocabulary to tell the tools apart — a real property, and the one v1
lacks. It is **not** evidence about how Claude behaves. With a key,
`--mode live` asks the model directly, five trials per case, with
`tool_choice: any` so the measurement is routing rather than whether it
answers in prose.

### The two rewrites

The eval drove both edits, and each is a generalisable lesson:

1. **A negative boundary that quotes the sibling's vocabulary backfires.**
   `find_issues_for_path` originally ended with *"Do NOT use this for
   free-text topics like 'security bugs'"* — which put "security bugs" into
   the path tool's description and diluted the only words identifying
   `search_issues`. The prompt *"Show me the open security bugs"* then routed
   to the wrong tool. Boundaries now name the *category*, not the sibling's
   example terms.
2. **Vague example queries are worse than none.** `search_issues` listed
   *"anything about rate limiting"*. The opener "anything" appears in requests
   of every kind, so it pulled unrelated prompts toward that tool and tied
   `path-dir` into an undecidable draw. Example queries now lead with the
   distinguishing noun.

---

## Commands

All commands run from this directory.

| Command | What it does |
|---|---|
| `uv run python main.py all` | Run every harness (default) |
| `uv run python main.py selection` | Tool-selection accuracy, v1 vs v2 |
| `uv run python main.py resources` | Exploratory-call reduction from resources |
| `uv run python main.py errors` | Six real failures over stdio, with payloads |
| `uv run python main.py doctor` | Config scopes, credential expansion, live handshake |
| `... --mode live --trials 5` | Score selection with Claude instead of the proxy |
| `... --print` | Also echo reports to the terminal |
| `... --quiet` | Suppress progress output |
| `uv run python main.py doctor --no-connect` | Config check without spawning the server |
| `uv run pytest 2_tool_design_and_MCP_integration/tests` | Run the 27 tests (from the repo root) |

`uv run python server.py` starts the MCP server on stdio. It speaks JSON-RPC,
so running it by hand just looks like a hang — that command is for MCP
clients, and `doctor` is the way to check it by hand.

---

## Configuration scopes

**Project scope** — [`.mcp.json`](.mcp.json), committed, shared with everyone
who opens this directory in Claude Code. Credentials arrive by expansion, never
as literals:

```json
"env": {
  "TRACKER_API_TOKEN": "${TRACKER_API_TOKEN}",
  "TRACKER_BASE_URL": "${TRACKER_BASE_URL:-https://tracker.internal.example}"
}
```

`doctor` also resolves each server's `command` the way a spawning client
would, and reports `Spawnable: NO` with the reason when it cannot be found —
an unspawnable config is the failure this module most easily ships unnoticed,
because every in-process test still passes.

`${VAR}` is required; `${VAR:-default}` falls back. `TRACKER_API_TOKEN` is the
only variable with no default, so `doctor` reports it as blocking when unset —
the sample data needs no real credential, but the config models a server that
would. A test asserts no `*_TOKEN`/`*_KEY`/`*_SECRET` value is ever written
literally into the committed file.

**User scope** — a personal server belongs in your own settings, not in a file
everyone inherits. [`user_scope.example.json`](user_scope.example.json) shows
the shape, and [`personal_scratchpad.py`](personal_scratchpad.py) is the
server itself — a cross-project note store, deliberately a different *kind*
of tool from the tracker. Register it with:

```bash
claude mcp add --scope user scratchpad -- uv run python personal_scratchpad.py
```

Then run `/mcp` in a session started here: both the project-scoped
`issue-tracker` and the user-scoped `scratchpad` are listed together, which is
the confirmation the brief asks for.

**Distributing tools so an agent isn't given more than it needs:** the tracker
server exposes only these four read tools — no write, no admin, no
cross-project bulk export. An agent exploring a codebase gets Claude Code's
built-in Grep/Read *plus* these; nothing else is on the table.

---

## Exploring the codebase incrementally

The workflow the brief asks you to practise, against
[`sample_service/`](sample_service/) — find entry points first, follow imports
from there, rather than reading every file up front:

1. `find_issues_for_path` with `sample_service/checkout` — three issues, one
   of them `CHK-104`.
2. `get_issue` on `CHK-104` — the comment names `apply_discounts` as the root
   cause.
3. **Grep** for `apply_discounts` — two hits: its definition in `pricing.py`
   and its caller in `cart.py`.
4. **Read** only `pricing.py`, and only then follow the import into `cart.py`.

Four steps reach the defect having read two files out of eleven. Starting from
`issues://catalog` instead of probing saves the discovery calls entirely.

---

## Layout

| File | Role |
|---|---|
| [`server.py`](server.py) | stdio entry point; the command `.mcp.json` runs |
| [`main.py`](main.py) | CLI for the four harnesses; writes into `output/` |
| [`.mcp.json`](.mcp.json) | Project-scoped server config with env expansion |
| [`user_scope.example.json`](user_scope.example.json) | The personal server, for user scope |
| [`personal_scratchpad.py`](personal_scratchpad.py) | The user-scope server itself: a cross-project note store |
| [`toolspecs.py`](issue_tracker/toolspecs.py) | v1 and v2 descriptions, shared schemas |
| [`handlers.py`](issue_tracker/handlers.py) | Tool implementations, transport-agnostic |
| [`mcp_server.py`](issue_tracker/mcp_server.py) | MCP wiring: `Annotated` args, `ToolError` |
| [`errors.py`](issue_tracker/errors.py) | `TrackerError`, categories, payload parsing |
| [`resources.py`](issue_tracker/resources.py) | The three MCP resources |
| [`selection.py`](issue_tracker/selection.py) | Tool-selection eval, live and proxy |
| [`resource_eval.py`](issue_tracker/resource_eval.py) | Exploratory-call analysis |
| [`config_check.py`](issue_tracker/config_check.py) | Scope and credential-expansion checks |
| [`probe_client.py`](issue_tracker/probe_client.py) | Real MCP client used by harnesses and tests |
| [`data.py`](issue_tracker/data.py) / [`store.py`](issue_tracker/store.py) | Sample dataset and queries |
| [`sample_service/`](sample_service/) | The codebase the issues point at |
| [`tests/`](tests/) | 27 tests, most talking to a real server process |
| `output/` | Reports, written at runtime (e.g. `output/selection-20260921-171859.md`) |

---

## Notes on the MCP SDK

Built against `mcp` 2.2.0, which renamed `FastMCP` to `MCPServer`. Two
behaviours were established by probing the SDK rather than assumed, and both
shaped the code:

- **Only `ToolError` preserves an error message.** Any other exception becomes
  `"Error executing tool <name>"` and the structured payload is lost. The
  message arrives prefixed, so [`parse_error_payload`](issue_tracker/errors.py)
  reads from the first brace.
- **Schemas are generated from the function signature**, so field
  descriptions only exist if arguments are `Annotated[..., Field(...)]`.
  Constraints declared there (`ge`, `le`, `pattern`) are enforced by the SDK
  before the handler runs — a test asserts an out-of-range `limit` is rejected
  at the schema layer. There is no way to supply a hand-written input schema,
  so the published schemas carry pydantic's `title` keys and no
  `additionalProperties: false`.

---

## Known limits

- Offline tool selection uses the discriminability proxy described above, not
  Claude. The v1→v2 result is real but is a measurement of the descriptions,
  not of model behaviour.
- The resource benefit is a call-count analysis with an authored probe plan,
  not observed agent behaviour.
- Live mode is unexercised end to end: no API key was available here. The call
  shape is verified against `anthropic` 1.7.0, but no real selection trial has
  run.
- The tracker is an in-memory fixture. There is no write path, no pagination,
  and no real upstream — `TRACKER_SIMULATE` stands in for upstream failure.
- The user-scope server (`personal_scratchpad.py`) is implemented and tested,
  but *registering* it is a Claude Code action, so "both scopes visible in
  one `/mcp` listing" is the one confirmation this module cannot assert in a
  test. You have to run it and look.
