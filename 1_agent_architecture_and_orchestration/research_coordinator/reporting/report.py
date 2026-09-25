"""Report rendering and run trace.

The bibliography is built from the structured findings, not from the
coordinator's prose. That is the point: attribution is guaranteed by the
program, so a synthesis step cannot quietly drop it.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from ..orchestration.orchestrator import RunReport

MOCK_BANNER = (
    "> **Mock mode.** No valid `ANTHROPIC_API_KEY` was available, so this run used "
    "the offline backend and a fixed synthetic source index. Sources marked "
    "`[SYNTHETIC]` or pointing at `example.invalid` are fabricated fixtures and "
    "must not be cited as real.\n"
)


def render_markdown(report: RunReport) -> str:
    parts: list[str] = []
    if report.mode == "mock":
        parts.append(MOCK_BANNER)

    parts.append(report.report_markdown.strip())
    parts.append(_sources_section(report))
    parts.append(_provenance_section(report))
    return "\n\n".join(p for p in parts if p).strip() + "\n"


def _sources_section(report: RunReport) -> str:
    if not report.sources:
        return "## Sources\n\n_No sources were returned._"
    lines = ["## Sources", ""]
    for source in report.sources:
        lines.append(
            f"- **[{source.id}]** {source.title} - `{source.locator}` "
            f"({source.type}, {source.date}) - via {source.specialist} "
            f"on \"{source.subtask}\""
        )
    return "\n".join(lines)


def _provenance_section(report: RunReport) -> str:
    lines = ["## Run provenance", ""]
    lines.append(f"- Mode: **{report.mode}** ({report.mode_reason})")
    lines.append(f"- Execution: {'parallel' if report.parallel else 'serial'}")
    lines.append(
        f"- Wall clock: {report.total_elapsed_s:.2f}s; summed subagent time: "
        f"{report.subagent_cpu_s:.2f}s (overlap factor {report.parallel_speedup:.2f}x)"
    )
    lines.append(f"- Coordinator turns: {report.coordinator_turns}")
    lines.append(f"- Tokens: {report.input_tokens} in / {report.output_tokens} out")

    for index, titles in enumerate(report.rounds, start=1):
        lines.append(f"- Round {index}: {', '.join(titles)}")

    for run in report.runs:
        if run.status == "ok":
            lines.append(
                f"  - {run.task_id} `{run.subagent}` \"{run.title}\": "
                f"{len(run.findings)} finding(s), {run.elapsed_s:.2f}s, "
                f"{len(run.tool_calls)} tool call(s)"
            )
        else:
            error = run.error or {}
            lines.append(
                f"  - {run.task_id} `{run.subagent}` \"{run.title}\": FAILED "
                f"({error.get('category')}, retryable={error.get('retryable')}, "
                f"attempts={run.attempts}) - {error.get('message')}"
            )

    if report.refused_tool_calls:
        lines.append(
            f"- Out-of-allowlist tool calls refused: {', '.join(report.refused_tool_calls)}"
        )
    return "\n".join(lines)


def render_trace(report: RunReport) -> str:
    """Full machine-readable trace of the run."""
    payload: dict[str, Any] = {
        "topic": report.topic,
        "mode": report.mode,
        "modeReason": report.mode_reason,
        "parallel": report.parallel,
        "wallClockSeconds": round(report.total_elapsed_s, 3),
        "summedSubagentSeconds": round(report.subagent_cpu_s, 3),
        "overlapFactor": round(report.parallel_speedup, 3),
        "coordinatorTurns": report.coordinator_turns,
        "tokens": {"input": report.input_tokens, "output": report.output_tokens},
        "rounds": report.rounds,
        "refusedToolCalls": report.refused_tool_calls,
        "sources": [asdict(s) for s in report.sources],
        "subagentRuns": [asdict(r) for r in report.runs],
        "reportMarkdown": report.report_markdown,
    }
    return json.dumps(payload, indent=2)
