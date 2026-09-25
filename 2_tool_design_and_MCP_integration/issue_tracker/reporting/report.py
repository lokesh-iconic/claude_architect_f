"""Markdown rendering for every harness."""

from __future__ import annotations

import json
from typing import Any

from ..evaluation.resource_eval import QuestionResult
from ..evaluation.selection import UNDECIDED, VersionResult

MOCK_NOTE = (
    "> **Offline proxy.** No valid `ANTHROPIC_API_KEY` was available, so tool "
    "selection was scored by the description-discriminability proxy rather "
    "than by Claude. The proxy measures whether the descriptions contain the "
    "vocabulary needed to tell the tools apart; it is not evidence about how "
    "the model behaves. Re-run with a key for a real measurement.\n"
)


def render_selection(results: dict[str, VersionResult], mode: str) -> str:
    lines = ["# Tool-selection evaluation", ""]
    if mode == "mock":
        lines += [MOCK_NOTE, ""]

    versions = list(results)
    lines += [
        "## Accuracy by description generation",
        "",
        "| Descriptions | Overall | Overlapping pair only |",
        "|---|---|---|",
    ]
    for version in versions:
        result = results[version]
        lines.append(
            f"| {version} | {result.accuracy:.0%} | {result.ambiguous_accuracy:.0%} |"
        )

    lines += ["", "## Per case", "", "| Case | Expected | " + " | ".join(versions) + " |"]
    lines.append("|---|---|" + "---|" * len(versions))
    for index, row in enumerate(results[versions[0]].results):
        cells = []
        for version in versions:
            r = results[version].results[index]
            picked = ", ".join(sorted(set(r.picks)))
            mark = "PASS" if r.accuracy == 1.0 else "FAIL"
            cells.append(f"{mark} ({picked})" if mark == "FAIL" else mark)
        flag = " *(overlap)*" if row.case.ambiguous else ""
        lines.append(
            f"| `{row.case.id}`{flag} | `{row.case.expected}` | " + " | ".join(cells) + " |"
        )

    for version in versions:
        failures = results[version].failures
        if not failures:
            continue
        lines += ["", f"### What {version} got wrong", ""]
        for row in failures:
            picked = ", ".join(
                f"`{name}` x{count}" for name, count in sorted(row.confusions.items())
            )
            picked = picked.replace(f"`{UNDECIDED}`", "*no discriminating vocabulary*")
            lines.append(
                f"- `{row.case.id}` -- {row.case.why} Expected "
                f"`{row.case.expected}`, got {picked}."
            )
    return "\n".join(lines) + "\n"


def render_resources(rows: list[QuestionResult], summary: dict[str, Any]) -> str:
    lines = [
        "# Do resources reduce exploratory tool calls?",
        "",
        f"> **Method.** {summary['method']}",
        "",
        "| Question | Calls without resources | With `issues://catalog` | Saved |",
        "|---|---|---|---|",
    ]
    for row in rows:
        suffix = "" if row.satisfied_without else " (still incomplete)"
        lines.append(
            f"| {row.question.text} | {row.calls_without}{suffix} | "
            f"{row.calls_with} | {row.saved} |"
        )
    lines += [
        "",
        f"**Total: {summary['toolCallsWithoutResources']} tool calls without "
        f"resources, {summary['toolCallsWithResources']} with them "
        f"({summary['callsAvoided']} avoided).**",
        "",
    ]
    unanswerable = summary["unanswerableWithoutResources"]
    if unanswerable:
        lines += [
            "Some questions are not merely slower without the resource, they are "
            "unanswerable by probing: "
            + ", ".join(f"`{q}`" for q in unanswerable)
            + ". Closed issues outside the current sprint never surface through "
            "the tools, so an agent cannot enumerate them by searching.",
            "",
        ]
    return "\n".join(lines)


def render_errors(probes: list[Any]) -> str:
    lines = [
        "# Structured tool failures",
        "",
        "Every row is a real call over stdio to a separately spawned server.",
        "",
        "| Scenario | isError | errorCategory | isRetryable | Agent's next move |",
        "|---|---|---|---|---|",
    ]
    for probe in probes:
        if probe.payload is None:
            lines.append(
                f"| {probe.label} | {probe.is_error} | *(unstructured)* | ? | "
                "cannot tell - this is the failure mode to avoid |"
            )
            continue
        payload = probe.payload
        action = (
            "retry once, then escalate"
            if payload["isRetryable"]
            else (
                "escalate to a human"
                if payload["errorCategory"] == "permission"
                else "fix the arguments and call again"
            )
        )
        lines.append(
            f"| {probe.label} | {probe.is_error} | `{payload['errorCategory']}` | "
            f"{payload['isRetryable']} | {action} |"
        )

    lines += ["", "## Full payloads", ""]
    for probe in probes:
        lines += [
            f"### {probe.label}",
            "",
            f"`{probe.tool}({json.dumps(probe.args)})`",
            "",
            "```json",
            json.dumps(probe.payload, indent=2) if probe.payload else probe.raw,
            "```",
            "",
        ]
    return "\n".join(lines)


def render_config(summary: dict[str, Any], capabilities: Any | None) -> str:
    lines = ["# MCP configuration check", ""]
    lines += [
        "## Servers by scope",
        "",
        "| Scope | Server | Command | Spawnable |",
        "|---|---|---|---|",
    ]
    for server in summary["servers"]:
        mark = "yes" if server["spawnable"] else "**NO**"
        lines.append(
            f"| {server['scope']} | `{server['name']}` | `{server['command']}` | {mark} |"
        )

    if summary["unspawnableServers"]:
        lines += [
            "",
            "### These servers cannot start",
            "",
            "A client spawning them gets a bare FileNotFoundError and reports "
            "the server as failed, with no indication of why.",
            "",
        ]
        for bad in summary["unspawnableServers"]:
            lines.append(f"- `{bad['name']}` ({bad['scope']}): {bad['hint']}")

    lines += ["", "## Credential expansion", "", "| Variable | Status |", "|---|---|"]
    seen: set[str] = set()
    for server in summary["servers"]:
        for var in server["env"]:
            if var["name"] in seen:
                continue
            seen.add(var["name"])
            lines.append(f"| `{var['name']}` | {var['status']} |")

    if summary["inlinedSecrets"]:
        lines += [
            "",
            "**Inlined secrets found (these must use `${VAR}` expansion):** "
            + ", ".join(f"`{s}`" for s in summary["inlinedSecrets"]),
        ]
    else:
        lines += ["", "No credential is written literally into `.mcp.json`."]

    lines += [
        "",
        "## Adding the personal server at user scope",
        "",
        "```bash",
        summary["registerUserScope"],
        "```",
        "",
        summary["confirmBothScopes"],
        "",
    ]

    if capabilities is not None:
        lines += [
            "## Live handshake",
            "",
            f"Connected over stdio. Tools: {len(capabilities.tools)}, "
            f"resources: {len(capabilities.resources)}.",
            "",
            "| Tool | Required args | All args |",
            "|---|---|---|",
        ]
        for tool in capabilities.tools:
            required = ", ".join(f"`{r}`" for r in tool["required"]) or "none"
            every = ", ".join(f"`{p}`" for p in tool["properties"])
            lines.append(f"| `{tool['name']}` | {required} | {every} |")
        lines += ["", "| Resource | Purpose |", "|---|---|"]
        for resource in capabilities.resources:
            first = resource["description"].split(".")[0]
            lines.append(f"| `{resource['uri']}` | {first}. |")
        lines.append("")
    return "\n".join(lines)
