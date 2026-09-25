---
description: Run the capstone support agent's test conversation set and report pass/fail per scenario
argument-hint: [--mode mock|live|auto] [--only scenario_name ...]
allowed-tools: Bash(uv run python capstone/main.py scenarios:*)
---

Run the support resolution agent's test conversation set, from the repo root:

```
uv run python capstone/main.py scenarios $ARGUMENTS
```

With no arguments this runs every scenario in `auto` mode: live if
`ANTHROPIC_API_KEY` validates, otherwise the offline mock. The first line of
output says which mode ran.

Then report:

1. The mode line, verbatim.
2. A table with one row per scenario: `scenario | domain | PASS/FAIL | checks`.
   For every FAIL, quote the failing claim and its evidence from the newest
   `capstone/output/scenarios-*.md`.
3. One line with the totals, and the path of the report that was written.

If the mode is mock, say that the results show the harness works, not how
Claude behaves. Do not modify any file, and do not re-run failing scenarios
hoping for a different result. If a scenario fails, say so and point at the
transcript section of the report for that scenario.
