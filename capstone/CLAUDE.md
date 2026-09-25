# Support resolution agent: how the system works

This is the project CLAUDE.md for the capstone agent. It layers on the root
[`../CLAUDE.md`](../CLAUDE.md), which covers team standards, live/mock mode
and secrets. Read this file first: it should explain the system without
the code. `main.py workflow` checks that every tool, record field,
escalation reason and error field named in the code also appears here.

## Architecture

A customer-support agent for returns, billing questions and account
problems. It works through four tools, backed by a synthetic order and
refund store.

```
customer turn ──▶ agent.py  (rebuilds the request every turn, branches on stop_reason)
                    │  system[0] static prompt: rules, escalation criteria, few-shots   (cached)
                    │  system[1] <case_facts>: ids, amounts, dates, refunds, tickets    (from tool results)
                    │  messages  <conversation_summary> + last 4 turns verbatim + this turn
                    ▼ tool_use
                 session.py  ToolSession.call -- the ONLY path to a handler (agent, MCP, or system)
                    1 scope      tool outside the four            -> validation / tool_out_of_scope
                    2 identity   process_refund, customer unverified -> prerequisite / identity_not_verified
                    3 contract   full schema + case-fact checks   -> validation / invalid_action_record (retry budget)
                    4 handler    deadline, transient retry budget -> transient / timeout + partialResults
                    5 policy     window, auto-approval limit, ownership -> policy / permission
                    6 trim -> extract case facts -> ledger record (money and case-state tools only)
```

- **Loop** (`agent.py`). `tool_use` runs the tools and continues.
  `end_turn`/`stop_sequence` is the reply. `pause_turn` replays and
  continues. `max_tokens` retries once without running the truncated call.
  `refusal`, an unknown stop reason, or the tool-round cap hands off. A
  hand-off is always a real ticket, filed by the program through the same
  validator (`authored_by: system`) if the model didn't file one.
- **Case facts** (`facts.py`) come only from successful tool results, never
  from customer text or model output. Summarization (`memory.py`) never
  touches them.
- **Ledger** (`session.ledger`). Every `process_refund` or
  `escalate_to_human` call that passed validation and reached its handler
  gets an `ActionRecord`, whether or not the handler then executed it.
- **MCP** (`server.py`, `.mcp.json`). The same four tools over stdio, one
  `ToolSession` per server process, so every gate above holds for any client.

## Tool contracts

The source of truth is `resolution_agent/tools/schema.py`. The API gets the same
schemas with `strict: true`, minus the constraints strict mode can't carry
(`minLength`, `pattern`, `exclusiveMinimum`). Those are enforced client-side
before the handler runs.

| Tool | Changes state? | Input | Returns (trimmed) |
|---|---|---|---|
| `get_customer` | no | `email`, `postal_code` | `verified: true` + `customer_id`, `name`, `email`, `tier`, `order_ids`; or `verified: false` with no id |
| `lookup_order` | no | `order_id` | status, dates, `total`, `shipping`, items, `refunded_amount`, `refundable_until`, latest scan |
| `process_refund` | **money** | the refund record below | `refund_id`, amount, status, settlement ETA |
| `escalate_to_human` | **case** | the handoff record below | `ticket_id`, queue, estimated wait |

**Refund record** (`process_refund`): `customer_id` (verified this
session), `order_id` (already fetched), `amount` (at most total minus
already refunded), `currency`, `refund_type` (`full` only for the whole
remainder, else `partial`), `reason_code` (enum), `customer_statement` (the
customer's words), `justification` (why this amount, citing the facts).

**Handoff record** (`escalate_to_human`): `reason_category`, `customer_id`
(the verified id, or null if nobody is verified), `identity_verified`,
`order_ids`, `customer_request`, `root_cause`, `recommended_action`,
`actions_taken`. `root_cause` and `recommended_action` are rejected if
they're boilerplate ("please review", "look into this", under four words).
The session also attaches `case_facts` and the last three error payloads.

**Errors.** Every failure is JSON with `errorCategory`, `isRetryable` and
`description`, plus `failureType`, `attempted`, usually `remediation`, and
`partialResults` when work finished before the failure. Categories:

| errorCategory | Meaning | Retryable |
|---|---|---|
| `validation` | bad input or action record; nothing ran | yes within the budget (3 attempts, or stop at once if the same issues repeat), otherwise no |
| `prerequisite` | refund before identity is verified | no |
| `permission` | the order belongs to someone else | no |
| `policy` | outside the 30-day window, above the 500.00 auto-approval limit, not delivered | no -- escalate |
| `transient` | timeout; carries `partialResults`, `completedSteps`, `incompleteStep` | once |
| `escalation` | the customer asked for a human; every other tool is blocked this turn | no |
| `internal` | a handler bug, still in the same shape | no |

**Scope.** The agent has these four tools and nothing else. There is no
account-change tool. Email, address and password changes need identity
re-verification by staff, so they are handed off.

## Escalation policy

Escalate (`escalate_to_human`) when any of these applies. Each criterion
has a few-shot example in `prompts.py`:

| reason_category | When | Enforced in code? |
|---|---|---|
| `customer_request` | the customer explicitly asks for a person | **yes**: detector in `escalation.py` blocks other tools, nudges once, then the program files the handoff |
| `policy_gap` | the policy or tools don't cover it: price adjustments, compensation, goodwill, any account change | prompt |
| `policy_limit` | a tool returned `errorCategory: policy` and the customer still wants it | prompt; the rejection is in the attached errors |
| `unable_to_progress` | a dependency failed after one retry, records contradict the customer, or the turn can't finish | prompt, plus program handoff on refusal, round cap, or an exhausted handoff budget |

Frustration alone is **not** a reason to escalate. Mentioning a person
("the person at your store said...") is not a request for one. Ambiguous
references ("the kettle" when there are two) get a clarifying question, not
a guess and not a handoff.

## Working in this module

- Never call a handler except through `ToolSession.call`. That's where the
  identity gate, validation, the ledger and the deadlines live.
- A new or changed field goes in `schema.py` first, then the semantic check
  in `actions.py` if it depends on the case facts, then this file. `main.py
  workflow` fails if this file falls behind the code.
- New behaviour gets a scenario in `scenarios.py`, so `/run-scenarios`
  covers it, and a test in `tests/`.
- The mock backend (`mock_agent.py`) is stateless: it may only read the
  request it receives. Don't give it memory. That's what makes the drift
  checks measure context management rather than the mock.
- Few-shot examples use fictional ids (`C-20xx`, `ORD-80xx`) that must stay
  out of `data.py`, and each one has to pass the validator.
