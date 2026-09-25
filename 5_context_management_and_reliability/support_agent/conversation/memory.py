"""Conversation history: recent turns verbatim, older turns summarized.

The last `keep_recent` exchanges are sent verbatim. Anything older is folded
into a narrative summary and dropped. The summary is prepended to the first
retained user message as its own `<conversation_summary>` block, and the case
facts go in a separate system block -- so the two never mix, and nothing the
summarizer does can touch an amount or an id.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol

SUMMARY_OPEN = "<conversation_summary>"
SUMMARY_CLOSE = "</conversation_summary>"
MAX_SUMMARY_LINES = 12


@dataclass
class Exchange:
    turn: int
    customer_text: str
    messages: list[dict[str, Any]]
    reply: str = ""
    tools: list[str] = field(default_factory=list)


class Summarizer(Protocol):
    def summarize(self, previous: str, exchange: Exchange) -> str: ...


@dataclass
class ConversationMemory:
    summarizer: Summarizer
    keep_recent: int = 4
    exchanges: list[Exchange] = field(default_factory=list)
    summary: str = ""
    compacted_turns: int = 0

    def commit(self, exchange: Exchange) -> None:
        self.exchanges.append(exchange)
        while len(self.exchanges) > self.keep_recent:
            oldest = self.exchanges.pop(0)
            self.summary = self.summarizer.summarize(self.summary, oldest)
            self.compacted_turns += 1

    def request_messages(self, current: list[dict[str, Any]]) -> list[dict[str, Any]]:
        messages = [m for ex in self.exchanges for m in ex.messages] + current
        if not self.summary or not messages:
            return messages
        messages = [copy.deepcopy(messages[0]), *messages[1:]]
        first = messages[0]
        body = first["content"]
        blocks = [{"type": "text", "text": body}] if isinstance(body, str) else list(body)
        summary_block = {"type": "text", "text": f"{SUMMARY_OPEN}\n{self.summary}\n{SUMMARY_CLOSE}"}
        first["content"] = [summary_block, *blocks]
        return messages


# --------------------------------------------------------------------------
# Mock summarizer
# --------------------------------------------------------------------------

_AMOUNT = re.compile(r"\$\s?(\d[\d,]*\.\d{2})")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_POSTAL = re.compile(r"\b\d{5}\b")


def paraphrase(text: str) -> str:
    """Rewrite text the way a narrative summary tends to: round, generalise, drop."""
    text = _AMOUNT.sub(lambda m: f"about ${round(float(m.group(1).replace(',', ''))):,}", text)
    text = _EMAIL.sub("their email address", text)
    text = _ISO_DATE.sub(lambda m: f"in {date(int(m.group(1)), int(m.group(2)), 1):%B %Y}", text)
    text = _POSTAL.sub("their postal code", text)
    return " ".join(text.split())


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


class MockSummarizer:
    """Deterministic stand-in for a model-written summary.

    Its lossiness is scripted to imitate what paraphrase does to numbers:
    `$249.99` becomes `about $250`, dates become months, emails disappear.
    That is exactly why case facts must not depend on the summary -- and the
    `--no-case-facts` ablation shows what happens when they do.
    """

    def summarize(self, previous: str, exchange: Exchange) -> str:
        tools = ", ".join(dict.fromkeys(exchange.tools)) or "no tools"
        line = (f'- Turn {exchange.turn}: customer: "{_clip(paraphrase(exchange.customer_text), 90)}" '
                f'Agent ({tools}): "{_clip(paraphrase(exchange.reply), 160)}"')
        lines = [ln for ln in previous.splitlines() if ln.startswith("- Turn")] + [line]
        already = re.match(r"\((\d+) earlier", previous)
        dropped = max(0, len(lines) - MAX_SUMMARY_LINES) + (int(already.group(1)) if already else 0)
        kept = lines[-MAX_SUMMARY_LINES:]
        header = f"({dropped} earlier turn(s) omitted)\n" if dropped > 0 else ""
        return header + "\n".join(kept)
