"""Deterministic plans for mock mode.

These are templates, not reasoning: mock mode exists to exercise the
orchestration (decomposition -> parallel delegation -> aggregation ->
attribution) without credentials. Real decomposition, which adapts to the
specific topic, only happens in live mode.
"""

from __future__ import annotations

import json
import re
from typing import Any

DIMENSIONS = [
    (
        "web_researcher",
        "Economic and labour-market effects",
        "economic effects, labour market, employment, income and cost structure",
    ),
    (
        "web_researcher",
        "Legal, policy and regulatory position",
        "copyright, licensing, regulation, policy and legal disputes",
    ),
    (
        "document_analyst",
        "Practitioner evidence in the local corpus",
        "how practitioners actually work, adoption in production, workflow changes",
    ),
]

GAP_DIMENSION = (
    "web_researcher",
    "Public attitudes and ethical debate",
    "public perception, audience attitudes, authenticity and ethical objections",
)


def _brief(subagent: str, title: str, angle: str, topic: str) -> dict[str, str]:
    return {
        "subagent": subagent,
        "title": title,
        "brief": (
            f"Overall research topic: {topic}\n\n"
            f"Your angle: {title}. Cover {angle} as they relate to this topic. "
            "You have no other context, so work only from this brief. Establish "
            "concrete, attributable claims and record what you could not "
            "establish in `gaps`. Finish by calling submit_findings once."
        ),
    }


def decompose(topic: str) -> list[dict[str, str]]:
    return [_brief(s, t, a, topic) for s, t, a in DIMENSIONS]


def gap_brief(topic: str) -> dict[str, str]:
    subagent, title, angle = GAP_DIMENSION
    return _brief(subagent, title, angle, topic)


# --------------------------------------------------------------------------
# Subagent findings
# --------------------------------------------------------------------------

WEB_ENTRY = re.compile(
    r"- (?P<title>.+?)\n\s*url: (?P<url>\S+)\n\s*date: (?P<date>\S+)\n\s*snippet: (?P<snippet>.+)"
)


def _tool_result_texts(messages: list[dict[str, Any]]) -> list[str]:
    texts: list[str] = []
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                texts.append(str(block.get("content", "")))
    return texts


def mock_findings(
    brief: str, messages: list[dict[str, Any]], *, has_docs: bool
) -> dict[str, Any]:
    angle = _angle_of(brief)
    texts = _tool_result_texts(messages)
    blob = "\n".join(texts)
    findings: list[dict[str, str]] = []

    if has_docs:
        # read_document returns "# <path>\n\n<body>"; ignore the listing output.
        document = next((t for t in texts if t.startswith("# ")), "")
        path = document.splitlines()[0][2:].strip() if document else None
        body = document.split("\n\n", 1)[-1] if document else ""
        match = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", document)
        date = match.group(1) if match else "unknown"
        for sentence in _sentences(body)[:3]:
            findings.append(
                {
                    "claim": sentence,
                    "confidence": "medium",
                    "source_title": f"Corpus document {path or 'unknown'}",
                    "source_locator": path or "unknown",
                    "source_type": "document",
                    "source_date": date,
                }
            )
    else:
        for match in WEB_ENTRY.finditer(blob):
            findings.append(
                {
                    "claim": match.group("snippet").strip(),
                    "confidence": "medium",
                    "source_title": match.group("title").strip(),
                    "source_locator": match.group("url").strip(),
                    "source_type": "web",
                    "source_date": match.group("date").strip(),
                }
            )

    if not findings:
        findings.append(
            {
                "claim": "No usable source was retrieved for this angle in mock mode.",
                "confidence": "low",
                "source_title": "mock backend",
                "source_locator": "mock://no-results",
                "source_type": "web",
                "source_date": "unknown",
            }
        )

    return {
        "summary": (
            f"{angle}: {len(findings)} attributable claim(s) drawn from the "
            "sources retrieved for this brief."
        ),
        "findings": findings,
        "gaps": ["Mock mode retrieves from a fixed index; quantitative depth is limited."],
    }


def _angle_of(brief: str) -> str:
    for line in brief.splitlines():
        if line.startswith("Your angle:"):
            return line.split(":", 1)[1].split(".")[0].strip()
    return brief.strip()[:60]


def _sentences(text: str) -> list[str]:
    skip = ("#", "Date:", "Source:")
    cleaned = " ".join(
        line
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith(skip)
    )
    parts = [s.strip() for s in re.split(r"(?<=[.!?])\s+", cleaned) if len(s.strip()) > 60]
    return parts


# --------------------------------------------------------------------------
# Coordinator synthesis
# --------------------------------------------------------------------------


def synthesize(topic: str, messages: list[dict[str, Any]]) -> str:
    """Assemble a report from the structured Task results already in context."""
    sections: list[str] = []
    failures: list[str] = []
    all_gaps: list[str] = []

    for text in _tool_result_texts(messages):
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        if data.get("error"):
            failures.append(
                f"- **{data.get('subtask', 'unknown subtask')}** - {data.get('description')} "
                f"(category: {data.get('errorCategory')}, retryable: {data.get('isRetryable')})"
            )
            continue
        if "findings" not in data:
            continue

        lines = [f"## {data.get('subtask', 'Subtask')}", "", data.get("summary", ""), ""]
        for item in data.get("findings", []):
            lines.append(f"- {item['claim']} [{item['sourceId']}]")
        sections.append("\n".join(lines))
        all_gaps.extend(data.get("gaps", []))

    body = "\n\n".join(sections) if sections else "_No findings were returned._"
    report = [f"# {topic}", "", "## Overview", "",
              f"This report draws on {len(sections)} completed subtask(s). "
              "Each claim carries the source id assigned when the finding was returned.",
              "", body]

    if failures:
        report += ["", "## Coverage not obtained", "",
                   "These angles were delegated but produced no findings:", "", *failures]
    if all_gaps:
        report += ["", "## Stated gaps", ""] + [f"- {g}" for g in dict.fromkeys(all_gaps)]

    return "\n".join(report)
