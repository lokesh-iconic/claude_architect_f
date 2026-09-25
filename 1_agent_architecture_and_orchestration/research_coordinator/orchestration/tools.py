"""Tool definitions and client-side executors.

Each subagent gets only the tools its role justifies; `agents.py` owns that
allowlist and `loop.py` refuses any call outside it. Tool failures are raised
as `ToolError`, which carries the structured metadata the coordinator needs to
decide between retrying, explaining, and moving on.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

MAX_DOC_CHARS = 8000
SNIPPET_CHARS = 320


class ToolError(Exception):
    """A tool failure the agent is expected to reason about."""

    def __init__(
        self,
        message: str,
        *,
        category: str = "internal",
        retryable: bool = False,
        attempted: str = "",
        partial: Any = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.category = category  # transient | validation | permission | internal
        self.retryable = retryable
        self.attempted = attempted
        self.partial = partial

    def to_payload(self) -> str:
        return json.dumps(
            {
                "error": True,
                "errorCategory": self.category,
                "isRetryable": self.retryable,
                "description": self.message,
                "attempted": self.attempted,
                "partialResults": self.partial,
            },
            indent=2,
        )


@dataclass
class ToolContext:
    corpus_dir: Path
    mode: str


Executor = Callable[[dict[str, Any], ToolContext], str]


# --------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------

TASK_TOOL: dict[str, Any] = {
    "name": "Task",
    "description": (
        "Delegate one self-contained research subtask to a specialist subagent and "
        "wait for its findings. The subagent starts with an EMPTY context: it cannot "
        "see this conversation, the original topic, or any other subagent's work, so "
        "the brief must restate everything it needs. Emit several Task calls in a "
        "single response to run subagents in parallel."
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "subagent": {
                "type": "string",
                "enum": ["web_researcher", "document_analyst"],
                "description": (
                    "web_researcher searches the open web and cites URLs. "
                    "document_analyst reads only the local corpus and cites file paths."
                ),
            },
            "title": {
                "type": "string",
                "description": "Short label for this subtask, e.g. 'Labour market effects'.",
            },
            "brief": {
                "type": "string",
                "description": (
                    "The complete standalone instruction. Include the overall research "
                    "topic, the specific angle to cover, and what a good answer contains."
                ),
            },
        },
        "required": ["subagent", "title", "brief"],
        "additionalProperties": False,
    },
}

SUBMIT_FINDINGS_TOOL: dict[str, Any] = {
    "name": "submit_findings",
    "description": (
        "Return your findings. You MUST call this exactly once, as your final action. "
        "Every claim carries its own source metadata so attribution survives synthesis; "
        "a claim you cannot attribute does not belong here."
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "2-4 sentences answering the brief."},
            "findings": {
                "type": "array",
                "description": "One entry per substantive claim.",
                "items": {
                    "type": "object",
                    "properties": {
                        "claim": {"type": "string"},
                        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                        "source_title": {"type": "string"},
                        "source_locator": {
                            "type": "string",
                            "description": "URL for web sources, file path for documents.",
                        },
                        "source_type": {"type": "string", "enum": ["web", "document"]},
                        "source_date": {
                            "type": "string",
                            "description": "Publication date, or 'unknown'.",
                        },
                    },
                    "required": [
                        "claim",
                        "confidence",
                        "source_title",
                        "source_locator",
                        "source_type",
                        "source_date",
                    ],
                    "additionalProperties": False,
                },
            },
            "gaps": {
                "type": "array",
                "description": "What this brief could not establish. Empty list if none.",
                "items": {"type": "string"},
            },
        },
        "required": ["summary", "findings", "gaps"],
        "additionalProperties": False,
    },
}

LIST_DOCUMENTS_TOOL: dict[str, Any] = {
    "name": "list_documents",
    "description": "List every document in the local corpus with its size. Takes no arguments.",
    "strict": True,
    "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
}

SEARCH_DOCUMENTS_TOOL: dict[str, Any] = {
    "name": "search_documents",
    "description": (
        "Case-insensitive substring search across the corpus. Returns matching file "
        "paths with a short snippet around each hit. Use this to locate relevant "
        "documents before reading them in full."
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "Literal text to look for."}},
        "required": ["query"],
        "additionalProperties": False,
    },
}

READ_DOCUMENT_TOOL: dict[str, Any] = {
    "name": "read_document",
    "description": (
        "Read one document from the corpus by the exact path returned by "
        "list_documents or search_documents. Long files are truncated."
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "Corpus-relative path."}},
        "required": ["path"],
        "additionalProperties": False,
    },
}

# Server-side tool: Anthropic runs it, so there is no executor for it.
WEB_SEARCH_SERVER_TOOL: dict[str, Any] = {
    "type": "web_search_20260209",
    "name": "web_search",
    "max_uses": 6,
}

WEB_SEARCH_MOCK_TOOL: dict[str, Any] = {
    "name": "web_search",
    "description": "Search the web and return titled results with URLs and publication dates.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
        "additionalProperties": False,
    },
}


# --------------------------------------------------------------------------
# Executors
# --------------------------------------------------------------------------


def _corpus_files(ctx: ToolContext) -> list[Path]:
    if not ctx.corpus_dir.is_dir():
        raise ToolError(
            f"corpus directory {ctx.corpus_dir} does not exist",
            category="validation",
            retryable=False,
            attempted=f"listing {ctx.corpus_dir}",
        )
    return sorted(
        p for p in ctx.corpus_dir.rglob("*") if p.is_file() and p.suffix.lower() in {".md", ".txt"}
    )


def _resolve(ctx: ToolContext, raw: str) -> Path:
    """Resolve a corpus-relative path, refusing anything outside the corpus."""
    candidate = (ctx.corpus_dir / raw).resolve()
    root = ctx.corpus_dir.resolve()
    if root not in candidate.parents and candidate != root:
        raise ToolError(
            f"path {raw!r} is outside the corpus directory",
            category="permission",
            retryable=False,
            attempted=f"read_document({raw!r})",
        )
    return candidate


def exec_list_documents(args: dict[str, Any], ctx: ToolContext) -> str:
    files = _corpus_files(ctx)
    if not files:
        raise ToolError(
            f"no .md or .txt documents found in {ctx.corpus_dir}",
            category="validation",
            retryable=False,
            attempted="list_documents()",
        )
    lines = [
        f"{p.relative_to(ctx.corpus_dir).as_posix()}  ({p.stat().st_size} bytes)" for p in files
    ]
    return f"{len(files)} document(s):\n" + "\n".join(lines)


def exec_search_documents(args: dict[str, Any], ctx: ToolContext) -> str:
    query = str(args.get("query", "")).strip()
    if not query:
        raise ToolError(
            "query must be a non-empty string",
            category="validation",
            retryable=False,
            attempted="search_documents()",
        )

    hits: list[str] = []
    for path in _corpus_files(ctx):
        text = path.read_text(encoding="utf-8", errors="replace")
        index = text.lower().find(query.lower())
        if index == -1:
            continue
        start = max(0, index - SNIPPET_CHARS // 2)
        snippet = " ".join(text[start : start + SNIPPET_CHARS].split())
        hits.append(f"{path.relative_to(ctx.corpus_dir).as_posix()}: ...{snippet}...")

    if not hits:
        return f"No matches for {query!r} across {len(_corpus_files(ctx))} document(s)."
    return f"{len(hits)} match(es) for {query!r}:\n" + "\n\n".join(hits)


def exec_read_document(args: dict[str, Any], ctx: ToolContext) -> str:
    raw = str(args.get("path", "")).strip()
    if not raw:
        raise ToolError(
            "path must be a non-empty corpus-relative path",
            category="validation",
            retryable=False,
            attempted="read_document()",
        )
    path = _resolve(ctx, raw)
    if not path.is_file():
        available = ", ".join(
            p.relative_to(ctx.corpus_dir).as_posix() for p in _corpus_files(ctx)
        )
        raise ToolError(
            f"no document at {raw!r}. Available: {available}",
            category="validation",
            retryable=False,
            attempted=f"read_document({raw!r})",
        )
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) > MAX_DOC_CHARS:
        text = text[:MAX_DOC_CHARS] + f"\n\n[truncated at {MAX_DOC_CHARS} characters]"
    return f"# {raw}\n\n{text}"


# A tiny fixed index so mock mode produces stable, obviously-synthetic sources.
MOCK_WEB_INDEX: list[dict[str, str]] = [
    {
        "title": "[SYNTHETIC] Generative tools and studio production pipelines",
        "url": "https://example.invalid/synthetic/studio-pipelines",
        "date": "2025-03-11",
        "snippet": (
            "Surveyed studios report generative tooling absorbing storyboarding and "
            "previsualisation first, with final-frame work still human-led."
        ),
        "tags": "production pipeline studio film animation workflow tool adoption",
    },
    {
        "title": "[SYNTHETIC] Copyright and training data: state of play",
        "url": "https://example.invalid/synthetic/copyright-state-of-play",
        "date": "2025-07-02",
        "snippet": (
            "Litigation has concentrated on whether training constitutes fair use, "
            "while licensing marketplaces for rights-cleared corpora expand in parallel."
        ),
        "tags": "copyright legal licensing rights regulation policy law training data",
    },
    {
        "title": "[SYNTHETIC] Freelance creative labour market indicators",
        "url": "https://example.invalid/synthetic/freelance-labour",
        "date": "2025-01-28",
        "snippet": (
            "Entry-level commissions in illustration and copywriting show the sharpest "
            "contraction; senior and art-directed work is comparatively stable."
        ),
        "tags": "labour jobs employment freelance economic income wages market worker",
    },
    {
        "title": "[SYNTHETIC] Audience attitudes to disclosed AI involvement",
        "url": "https://example.invalid/synthetic/audience-attitudes",
        "date": "2025-05-19",
        "snippet": (
            "Disclosure reduces perceived authenticity for narrative work but has "
            "little measured effect on functional or background content."
        ),
        "tags": "audience public perception ethics culture authenticity social attitude",
    },
    {
        "title": "[SYNTHETIC] Tooling economics and cost per finished minute",
        "url": "https://example.invalid/synthetic/tooling-economics",
        "date": "2025-09-04",
        "snippet": (
            "Cost per finished minute falls most where iteration count is high; "
            "review and rights-clearance overhead offsets part of the saving."
        ),
        "tags": "economic cost budget technical infrastructure compute efficiency",
    },
]


def exec_mock_web_search(args: dict[str, Any], ctx: ToolContext) -> str:
    query = str(args.get("query", "")).strip()
    if not query:
        raise ToolError(
            "query must be a non-empty string",
            category="validation",
            retryable=False,
            attempted="web_search()",
        )

    terms = {t for t in query.lower().replace(",", " ").split() if len(t) > 3}
    scored = []
    for entry in MOCK_WEB_INDEX:
        haystack = f"{entry['tags']} {entry['title']} {entry['snippet']}".lower()
        score = sum(1 for t in terms if t in haystack)
        scored.append((score, entry))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    chosen = [entry for score, entry in scored if score > 0][:3] or [MOCK_WEB_INDEX[0]]

    lines = [f"{len(chosen)} result(s) for {query!r} (mock index, synthetic sources):"]
    for entry in chosen:
        lines.append(
            f"- {entry['title']}\n  url: {entry['url']}\n  date: {entry['date']}\n"
            f"  snippet: {entry['snippet']}"
        )
    return "\n".join(lines)


EXECUTORS: dict[str, Executor] = {
    "list_documents": exec_list_documents,
    "search_documents": exec_search_documents,
    "read_document": exec_read_document,
    "web_search": exec_mock_web_search,
}
