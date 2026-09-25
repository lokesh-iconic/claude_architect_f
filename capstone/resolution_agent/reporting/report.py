"""Markdown reports and JSON traces for each suite."""

from __future__ import annotations

import json
from typing import Any

from ..evaluation.results import SuiteResult, Transcript
from ..config.settings import Settings

TITLES = {
    "scenarios": "Test conversation set: pass/fail per scenario",
    "loop": "D1 - Agentic loop, refund prerequisite, handoff protocol",
    "tools": "D2 - Tool scope, descriptions, structured errors, MCP",
    "actions": "D4 - Schema-validated actions, few-shot examples, validation-retry",
    "context": "D5 - Case facts across a 22-turn, three-issue conversation",
    "workflow": "D3 - Claude Code workflow: CLAUDE.md, slash command, plan, brief",
}


def _banner(settings: Settings) -> str:
    if settings.is_live:
        return f"> Mode: **live** ({settings.mode_reason}). Model: `{settings.model}`, effort `{settings.effort}`."
    return (
        f"> Mode: **mock** ({settings.mode_reason}). The agent is a rule-based stand-in for the model, "
        "so these results show that the harness (case facts, validation, gates, ledger, error payloads) works. "
        "They are not evidence of how Claude behaves. Customer data is synthetic."
    )


def _cell(value: Any) -> str:
    text = json.dumps(value) if isinstance(value, (dict, list)) else str(value)
    return text.replace("|", "\\|").replace("\n", " ")


def _table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "_(none)_"
    cols = list(rows[0])
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    lines += ["| " + " | ".join(_cell(r.get(c, "")) for c in cols) + " |" for r in rows]
    return "\n".join(lines)


def _transcript(t: Transcript) -> str:
    out = [f"### {t.title}", ""]
    if t.note:
        out += [t.note, ""]
    for r in t.records:
        out.append(f"**Turn {r.turn} - customer:** {r.customer_text}")
        out.append("")
        for o in r.outcomes:
            mark = "BLOCKED" if o.blocked else ("error" if o.is_error else "ok")
            detail = o.payload.get("failureType") or ""
            size = f", {o.raw_chars:,} -> {o.trimmed_chars:,} chars" if o.raw_chars else ""
            who = " (filed by the system)" if o.authored_by == "system" else ""
            out.append(f"- `{o.tool}({json.dumps(o.input)})`{who} -> {mark} {detail}{size}".rstrip())
        if r.nudged:
            out.append("- harness nudge: escalation required")
        if r.stop not in ("end_turn", "stop_sequence"):
            out.append(f"- turn ended: `{r.stop}` (stop_reasons: {r.stop_reasons})")
        out += ["", "\n".join(f"> {line}".rstrip() for line in r.reply.splitlines()), ""]
    return "\n".join(out)


def render(suite: SuiteResult, settings: Settings) -> str:
    passed = sum(c.passed for c in suite.checks)
    parts = [
        f"# {TITLES.get(suite.name, suite.name)}",
        "",
        _banner(settings),
        "",
        f"**{passed}/{len(suite.checks)} checks passed.**",
        "",
    ]
    if "scenarios" in suite.tables:
        parts += ["## Scenarios", "", _table(suite.tables["scenarios"]), ""]
    parts += [
        "## Checks",
        "",
        "| Result | Claim | Evidence |",
        "|---|---|---|",
        *[f"| {'PASS' if c.passed else 'FAIL'} | {_cell(c.claim)} | {_cell(c.detail)} |" for c in suite.checks],
        "",
    ]
    for name, rows in suite.tables.items():
        if name != "scenarios":
            parts += [f"## {name.replace('_', ' ').capitalize()}", "", _table(rows), ""]
    for note in suite.notes:
        parts += [note, ""]
    if suite.transcripts:
        parts += ["## Transcripts", ""]
        parts += [_transcript(t) for t in suite.transcripts]
    return "\n".join(parts)


def trace_json(suite: SuiteResult, settings: Settings) -> str:
    return json.dumps({
        "suite": suite.name,
        "mode": settings.mode,
        "checks": [c.__dict__ for c in suite.checks],
        "tables": suite.tables,
        "transcripts": [
            {"title": t.title, "turns": [
                {"turn": r.turn, "customer": r.customer_text, "reply": r.reply, "nudged": r.nudged,
                 "stop": r.stop, "stop_reasons": r.stop_reasons, "request_chars": r.request_chars,
                 "tools": [{"tool": o.tool, "input": o.input, "is_error": o.is_error, "blocked": o.blocked,
                            "authored_by": o.authored_by, "payload": o.payload} for o in r.outcomes]}
                for r in t.records]}
            for t in suite.transcripts
        ],
    }, indent=2, default=str)
