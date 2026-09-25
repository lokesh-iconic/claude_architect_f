# Architecture plan: support resolution agent (capstone)

This was written before the agentic loop, as the design pass for a
multi-file, architecturally significant change. Plan mode is meant for
exactly this kind of change. It records what was decided and why, so the
code can be checked against it. Plan mode itself is a Claude Code session
feature, and a repository can't prove it was used. This file is the
artifact that pass produces.

## Starting point

Module 5 already has a support agent with case facts, trimming, a refund
prerequisite, an escalation backstop and structured timeouts. Module 4 has
strict tool schemas, few-shot examples and a validation-retry loop. The
capstone combines them into one agent. The main risk is that the pieces
contradict each other. For example, a validation-retry loop that also
retries a refund the identity gate should have blocked, or a
schema-validated refund that bypasses the audit trail.

## Decisions

1. **One call path.** Every tool call, whether from the in-process agent,
   an MCP client or the system itself, goes through `ToolSession.call`. It
   runs checks in a fixed order, and each check can stop the call before
   the handler runs:
   1. unknown tool → `validation`
   2. `process_refund` without a verified `customer_id` → `prerequisite`
   3. schema check of the full contract (types, enums, patterns, lengths)
      → `validation` with an issue list, retryable within a budget
   4. semantic check of an action record against the case facts (amount ≤
      refundable, `refund_type` consistent, handoff `customer_id` equals the
      verified customer, no boilerplate root cause) → same error shape
   5. handler with a deadline → `transient` timeout with `partialResults`
   6. policy and ownership in the handler → `policy` / `permission`
   7. trim → extract facts → write a ledger record for action tools

   The identity check runs before the schema check, so an unverified refund
   is always `prerequisite`, however malformed it is. "Refused every time"
   stays a single invariant.

2. **Two schemas per tool.** The full contract lives in `schema.py` and is
   enforced client-side. The API copy is the same schema with the keywords
   strict mode doesn't support (`minLength`, `pattern`, `exclusiveMinimum`)
   removed. That's why the validation-retry loop is needed even with
   `strict: true`. Strict mode guarantees shape. It can't guarantee
   "refund no more than was paid".

3. **Action records.** `process_refund` and `escalate_to_human` are the only
   tools that change money or case state. Their inputs are the record: what
   was done (ids, amount, currency, type) and why (reason code, the
   customer's words, a justification or root cause, and a recommended
   action). A record that passes validation and reaches the handler goes
   into `session.ledger`, whatever the handler then decides. Invariant:
   handler invocations for action tools equal ledger records.

4. **No account-mutation tool.** The brief fixes the tool set at four.
   Account changes (email, address, password) need a security re-check a
   support agent shouldn't own. They go to a person through
   `escalate_to_human` (`policy_gap`), so they still produce a validated
   record. That keeps the tool scope to what the workflow needs.

5. **Loop and stop_reason.** Each value has its own branch: `tool_use`
   runs the tools and continues. `end_turn` and `stop_sequence` finish
   (with one escalation nudge if one is owed). `pause_turn` replays and
   continues. `max_tokens` retries once without running a possibly
   truncated tool call. `refusal` and unknown values stop, and the system
   files a handoff. A turn that can't finish (refusal, round cap, retry
   budget spent on a handoff) is handed off by the program through the
   same validator. The customer is never told "a colleague will pick
   this up" unless a ticket exists.

6. **Handoff protocol.** An `escalate_to_human` record needs
   `customer_id` (null only if nobody is verified), `root_cause`,
   `recommended_action`, `customer_request`, `order_ids` and
   `actions_taken`. The session also attaches the case facts and the last
   three errors itself.

7. **Context.** Module 5's design carries over unchanged. The case facts are a
   separate system block, rebuilt from tool results every request. Older
   turns become a summary in the message history. Every request's facts
   block is logged per turn, so drift is measured on what the model
   actually saw.

8. **Few-shot examples.** Three, for the most ambiguous request types. A
   defect refund with a stated amount (against a price-drop request that
   looks the same). A duplicate-charge claim the records don't show. An
   order reference that matches two orders. The examples use fictional
   ids that aren't in the store, and a test runs each one through the
   same validator the agent faces.

## Files

| New / changed | Responsibility |
|---|---|
| `schema.py` (new) | Full tool contracts, API-safe copies, the hand-rolled validator |
| `actions.py` (new) | Semantic checks, `ActionRecord`, generic-text detection |
| `few_shot.py` (new) | Three reasoned examples, rendered into the prompt |
| `agent.py` (rewritten) | Loop with per-stop_reason handling, system handoff |
| `session.py` (extended) | Validation step, retry budget, ledger |
| `scenarios.py` (rewritten) | The conversation test set and the per-domain suites |
| `config_check.py` (new) | Checks CLAUDE.md, the slash command, the brief |

## Verification plan

- One scenario per behaviour, run by `main.py scenarios`, which is what
  `/run-scenarios` calls.
- Per-domain suites (`loop`, `tools`, `actions`, `context`, `workflow`),
  plus the refund-gate and ledger fuzz.
- A 22-turn, three-issue conversation where the facts block of every
  request is compared with what turn 2 recorded.
- The module 1–5 test suites still pass unchanged.
