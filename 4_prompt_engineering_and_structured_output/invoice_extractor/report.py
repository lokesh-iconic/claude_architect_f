"""Markdown reports, JSON traces, and the human-review queue."""

from __future__ import annotations

import json
from typing import Any

from .batch import BatchRun
from .evaluate import EvalSummary
from .few_shot import EXAMPLES
from .models import DocumentResult
from .settings import Settings

MOCK_BANNER = (
    "> **Mock mode.** Extractions come from hand-labelled ground truth with scripted "
    "first-attempt defects. This measures the pipeline's validation, retry, batching, and "
    "routing -- not how Claude extracts. Run with `--mode live` for model behaviour.\n"
)


def _config_lines(settings: Settings) -> list[str]:
    return [
        f"- Mode: **{settings.mode}** ({settings.mode_reason})",
        f"- Model: `{settings.model}` (effort `{settings.effort}`)",
        f"- Confidence threshold: {settings.confidence_threshold}",
        f"- Max attempts per document: {settings.max_attempts}",
        f"- Few-shot examples: {len(EXAMPLES)} ({', '.join(e.name for e in EXAMPLES)})",
    ]


def _attempt_codes(result: DocumentResult) -> str:
    parts = []
    for a in result.attempts:
        codes = sorted({i.code for i in a.issues})
        parts.append(f"{a.attempt}: {', '.join(codes) if codes else 'clean'}")
    return "<br>".join(parts)


def _eval_section(summary: EvalSummary) -> list[str]:
    c = summary.counts
    lines = [
        "## Accuracy against ground truth",
        "",
        f"Header fields checked: {summary.fields_checked} across {summary.documents} documents "
        f"({summary.absent_fields} are genuinely absent in the source).",
        "",
        "| Outcome | Count |",
        "|---|---|",
        f"| correct value | {c['correct']} |",
        f"| correctly null (absent in source) | {c['correct_null']} |",
        f"| **fabricated** (absent in source, value returned) | {c['fabricated']} |",
        f"| missed (present, returned null) | {c['missed']} |",
        f"| wrong value | {c['wrong']} |",
        "",
        f"Fabrications on the first attempt: {len(summary.fabricated_first_attempt)} "
        f"{summary.fabricated_first_attempt or ''}".rstrip(),
        f"Fabrications in the final output: {len(summary.fabricated_final)} "
        f"{summary.fabricated_final or ''}".rstrip(),
    ]
    if summary.line_item_count_mismatches:
        lines.append(f"Line-item count mismatches: {summary.line_item_count_mismatches}")
    return lines + [""]


def render_extract(results: list[DocumentResult], summary: EvalSummary, settings: Settings) -> str:
    accepted = [r for r in results if r.status == "accepted"]
    review = [r for r in results if r.status == "review"]
    lines = ["# Extraction run (synchronous, with validation-retry)", ""]
    if not settings.is_live:
        lines += [MOCK_BANNER]
    lines += _config_lines(settings) + [
        "",
        f"**{len(results)} documents: {len(accepted)} accepted, {len(review)} routed to human review, "
        f"{sum(r.attempt_count for r in results)} model calls.**",
        "",
        "## Per document",
        "",
        "| Document | Attempts | Issues per attempt | Outcome | Review reasons |",
        "|---|---|---|---|---|",
    ]
    for r in results:
        reasons = "<br>".join(r.review_reasons) or "-"
        lines.append(f"| `{r.doc_id}` | {r.attempt_count} | {_attempt_codes(r)} | {r.status} | {reasons} |")
    lines += [""] + _eval_section(summary)
    return "\n".join(lines) + "\n"


def render_batch(run: BatchRun, summary: EvalSummary, settings: Settings) -> str:
    results = list(run.results.values())
    accepted = sum(r.status == "accepted" for r in results)
    lines = ["# Batch run (Message Batches API)", ""]
    if not settings.is_live:
        lines += [MOCK_BANNER]
    lines += _config_lines(settings) + [
        f"- Max batch rounds: {settings.batch_max_rounds}",
        "",
        f"**{len(results)} documents: {accepted} accepted, {len(results) - accepted} routed to review.**",
        "",
        "## Rounds",
        "",
        "| Round | Batch | Submitted | Succeeded | Errored | Expired | Transient resubmit | "
        "Validation resubmit | Rejected (not resubmitted) | Finished |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for rec in run.rounds:
        lines.append(
            f"| {rec.round} | `{rec.batch_id}` | {len(rec.submitted)} | {rec.succeeded} | {rec.errored} | "
            f"{rec.expired} | {len(rec.transient_retry)} | {len(rec.validation_retry)} | "
            f"{len(rec.rejected)} | {len(rec.finished)} |"
        )
    lines += ["", "## Resubmission check", ""]
    for prev, nxt in zip(run.rounds, run.rounds[1:]):
        same = set(nxt.submitted) == set(prev.resubmit)
        lines.append(
            f"- Round {nxt.round} submitted {len(nxt.submitted)} of {len(prev.submitted)}; "
            f"equals round {prev.round}'s failed-and-retryable set: **{'yes' if same else 'NO'}**"
        )
    if len(run.rounds) < 2:
        lines.append("- Only one round ran; nothing needed resubmitting.")
    lines += [""] + _eval_section(summary)
    reviews = [r for r in results if r.status == "review"]
    if reviews:
        lines += ["## Routed to review", "", "| Document | Reasons |", "|---|---|"]
        lines += [f"| `{r.doc_id}` | {'<br>'.join(r.review_reasons)} |" for r in reviews]
    return "\n".join(lines) + "\n"


def review_queue_jsonl(results: list[DocumentResult]) -> str:
    rows = []
    for r in results:
        if r.status != "review":
            continue
        rows.append(json.dumps({
            "doc_id": r.doc_id,
            "reasons": r.review_reasons,
            "low_confidence_fields": [{"field": f, "confidence": c} for f, c in r.low_confidence],
            "unresolved_issues": [i.to_payload() for i in r.unresolved],
            "extraction": r.extraction.to_tool_input() if r.extraction else None,
        }, ensure_ascii=False))
    return "\n".join(rows) + ("\n" if rows else "")


def trace_json(results: list[DocumentResult], summary: EvalSummary, extra: dict[str, Any] | None = None) -> str:
    docs = []
    for r in results:
        docs.append({
            "doc_id": r.doc_id,
            "status": r.status,
            "review_reasons": r.review_reasons,
            "attempts": [
                {
                    "attempt": a.attempt,
                    "stop_reason": a.stop_reason,
                    "feedback_sent": a.feedback_sent,
                    "issues": [i.to_payload() for i in a.issues],
                    "extraction": a.extraction,
                }
                for a in r.attempts
            ],
        })
    payload = {"evaluation": summary.to_dict(), "documents": docs}
    if extra:
        payload.update(extra)
    return json.dumps(payload, indent=2, ensure_ascii=False)
