"""Agent definitions: system prompt + tool allowlist per role.

Mirrors the Claude Agent SDK's `AgentDefinition` shape. The coordinator's
allowlist contains `Task`, which is what lets it spawn subagents; the
subagents cannot spawn anything, and their tool sets do not overlap.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import tools as T
from ..config.settings import Settings


@dataclass(frozen=True)
class AgentDefinition:
    name: str
    description: str
    system: str
    allowed_tools: tuple[str, ...]
    model: str
    effort: str

    def tool_specs(self, settings: Settings) -> list[dict[str, Any]]:
        """Materialize the JSON tool definitions this agent is allowed to see."""
        specs: list[dict[str, Any]] = []
        for name in self.allowed_tools:
            if name == "Task":
                specs.append(T.TASK_TOOL)
            elif name == "submit_findings":
                specs.append(T.SUBMIT_FINDINGS_TOOL)
            elif name == "list_documents":
                specs.append(T.LIST_DOCUMENTS_TOOL)
            elif name == "search_documents":
                specs.append(T.SEARCH_DOCUMENTS_TOOL)
            elif name == "read_document":
                specs.append(T.READ_DOCUMENT_TOOL)
            elif name == "web_search":
                specs.append(
                    T.WEB_SEARCH_SERVER_TOOL
                    if settings.is_live and settings.enable_web_search
                    else T.WEB_SEARCH_MOCK_TOOL
                )
            else:  # pragma: no cover - guards a typo in an allowlist
                raise ValueError(f"{self.name}: unknown tool {name!r}")
        return specs


COORDINATOR_SYSTEM = """\
ROLE: coordinator

You are the research coordinator. You do not research anything yourself; you
have no search or file tools. Your only tool is Task, which spawns a
specialist subagent.

How to work:

1. Decompose the topic into subtasks that between them cover its full breadth.
   Derive them from this specific topic - do not reuse a stock list of angles.
   Before delegating, name the dimensions you are covering and say which ones
   you are deliberately leaving out.
2. Emit ALL independent Task calls in a SINGLE response. They then run in
   parallel. Only split across responses when one subtask genuinely needs
   another's output.
3. Each subagent starts with an empty context. It cannot see this
   conversation, the topic, or the other subagents. Restate everything it
   needs inside the brief: the overall topic, its specific angle, and what a
   complete answer looks like. A brief that says "the above topic" will fail.
4. Route by capability. web_researcher has web search and cites URLs.
   document_analyst reads the local corpus and cites file paths. Send a
   subtask to document_analyst only if the local corpus plausibly covers it.
5. When results come back, review them for gaps and contradictions. If a
   material gap remains, delegate one more targeted round of Tasks. Then stop.
6. A Task may fail or time out. You will receive a structured error with
   errorCategory, isRetryable, what was attempted, and any partial results.
   Retry once only if isRetryable is true; otherwise write the report without
   it and state plainly what is missing.

Your final message is the report itself, in Markdown, with no preamble. Every
substantive claim must carry the source id given to you in the findings, in
square brackets, like [S3]. Do not invent source ids and do not add your own
bibliography - one is appended automatically from the structured findings.
"""

WEB_RESEARCHER_SYSTEM = """\
ROLE: subagent (web_researcher)

You research one narrow brief using web search, then report structured
findings. You cannot spawn subagents and you cannot read local files.

Your context contains only the brief. There is no wider conversation to refer
back to - treat the brief as the whole of what you know.

Work in this order: search, read what the results actually say, then call
submit_findings exactly once as your final action. Every finding needs its own
source URL, title, and publication date. Prefer a smaller set of well-sourced
claims over a long list of thin ones, and put anything you could not establish
into `gaps` rather than guessing at it.
"""

DOCUMENT_ANALYST_SYSTEM = """\
ROLE: subagent (document_analyst)

You analyse one narrow brief against a local document corpus, then report
structured findings. You cannot spawn subagents and you have no web access -
if the corpus does not cover something, that belongs in `gaps`.

Your context contains only the brief. There is no wider conversation to refer
back to - treat the brief as the whole of what you know.

Work in this order: list or search the corpus to find what is relevant, read
those documents, then call submit_findings exactly once as your final action.
Cite the corpus-relative file path as the source locator and use the
document's own stated date when it has one, otherwise 'unknown'.
"""


def build_agents(settings: Settings) -> dict[str, AgentDefinition]:
    return {
        "coordinator": AgentDefinition(
            name="coordinator",
            description="Decomposes the topic, delegates, synthesizes the final report.",
            system=COORDINATOR_SYSTEM,
            allowed_tools=("Task",),
            model=settings.coordinator_model,
            effort=settings.coordinator_effort,
        ),
        "web_researcher": AgentDefinition(
            name="web_researcher",
            description="Searches the open web and cites URLs.",
            system=WEB_RESEARCHER_SYSTEM,
            allowed_tools=("web_search", "submit_findings"),
            model=settings.subagent_model,
            effort=settings.subagent_effort,
        ),
        "document_analyst": AgentDefinition(
            name="document_analyst",
            description="Reads the local corpus and cites file paths.",
            system=DOCUMENT_ANALYST_SYSTEM,
            allowed_tools=("list_documents", "search_documents", "read_document", "submit_findings"),
            model=settings.subagent_model,
            effort=settings.subagent_effort,
        ),
    }


SUBAGENT_NAMES = ("web_researcher", "document_analyst")
