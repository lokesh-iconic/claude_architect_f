---
description: Cross-check a module's README self-check table against its actual test/CLI output
argument-hint: [module-directory]
---

Verify that `$ARGUMENTS`'s README self-check table still matches reality.

1. Read `$ARGUMENTS/README.md` and list every row of its `## Self-check`
   table: the claim, and the "How to verify" command it names.
2. Run that command for each row (e.g. `uv run pytest -k ...`, or
   `uv run python $ARGUMENTS/main.py <subcommand>`), from the repo root.
3. For each row, report PASS (the command's output supports the claim as
   written), MISMATCH (the output contradicts the claim — quote the
   discrepancy), or BLOCKED (the command couldn't be run — say why, e.g.
   missing `ANTHROPIC_API_KEY` for a live-only claim).
4. Finish with a one-line verdict per row in a small table, and call out
   anything in "Known limits" that the current output no longer matches
   (e.g. a limit that's since been fixed, or a new gap not yet documented).

Do not modify any file — this is a read-only audit. If you find a real
discrepancy the user should fix, say so explicitly rather than silently
"fixing" the README or the code.
