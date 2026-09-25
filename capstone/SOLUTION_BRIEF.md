# Solution brief: customer support resolution agent

**For:** engineering lead, customer support platform · **Status:** all behaviour verified offline on
synthetic data; live-model rates not yet measured (see the open risk)

## What it does

It resolves returns, billing questions and account requests end to end, through four tools:
`get_customer`, `lookup_order`, `process_refund` and `escalate_to_human`. It hands off to a person
when it shouldn't decide. Every refund and every handoff leaves an audited, schema-validated record.

## Architecture

```
 customer ─▶ agent loop ─── request rebuilt every turn: cached rules · <case_facts> · summary + last 4 turns
                │ tool call
                ▼
          ToolSession.call   one path for every caller (agent, MCP client, the system itself)
            identity gate ─▶ schema + case-fact validation ─▶ deadline ─▶ policy ─▶ ledger record
                │ JSON errors: errorCategory · isRetryable · description · partialResults
                ▼
          order / refund / ticketing systems      (exposed to other clients as an MCP server)
```

## Key design tradeoffs

| Decision | Why | Cost we accepted |
|---|---|---|
| Rules that move money are **code, not prompt**. A refund needs a verified customer and a validated record before anything runs | "Refuses every time" has to hold even when the model slips. 2,000 random call sequences: 0 bypasses | Legitimate edge cases (a trusted agent-assisted refund, say) need a code change, not a prompt tweak |
| **Two schemas per tool.** Strict mode for shape; amounts, formats and lengths checked by us | Strict mode can't express "no more than was paid". A bad record comes back with every problem listed, and nothing has run | A small hand-rolled validator to maintain, instead of a dependency |
| **Case facts separate from history.** Ids and amounts come from tool results only; older turns are summarized | Summaries paraphrase numbers. In a 22-turn, three-issue test the turn-2 facts were unchanged in all 20 later requests. With the facts block removed, the same run lost the account email and the refund reference | The facts block grows with the case; it isn't pruned yet |
| **Four tools, no account-change tool** | Email and password changes need a security re-check a support agent shouldn't own | Those requests always cost a human touch |
| **The program files the handoff** when the model can't (refusal, retry budget spent, request for a human ignored) | The customer is never told "a colleague will pick this up" unless a ticket exists | A system-written handoff is less specific than a good model-written one |

## Escalation policy

The agent escalates when one of four conditions holds:

1. The customer asks for a person. This is enforced in code: every other tool is blocked that turn.
2. The request is outside policy: price adjustments, compensation, any account change.
3. A tool rejects it on policy grounds (outside the 30-day window, over 500.00) and the customer
   still wants it.
4. It can't make progress: a dependency failed twice, or the records contradict the customer, such
   as a duplicate charge the order system doesn't show.

It doesn't escalate for frustration alone. Every ticket carries the verified customer id, a specific
root cause and a recommended next action ("check the processor for a second capture; if found,
refund the duplicate"). The system attaches the case facts and the last errors.

## Open risk before production

**A refund that times out may still have gone through.** Today a timed-out `process_refund` is
reported as retryable. That's safe against our test double, which never commits after a timeout,
but a real payment API can commit and then time out. A retry would then refund twice. Before go-live:

- Send the ledger's action id to the payment provider as an idempotency key.
- On a refund timeout, check the refund's status before allowing any retry.
- Add a contract test against the provider's sandbox.

Also outstanding: a live-model evaluation of the scenario set. Behaviour is verified against a
rule-based stand-in, so it shows the controls work, not how often the model picks the right action.
