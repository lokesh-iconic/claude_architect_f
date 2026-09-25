"""Detect an explicit request for a human.

This is the programmatic backstop behind the prompt's escalation criteria.
When it fires, the agent layer blocks every tool except `escalate_to_human`
for that turn, and nudges once if the model tries to finish without
escalating. It is deliberately narrow: it needs a request verb plus a
human noun, so "the person at your store told me..." does not fire, and
frustration alone ("this is the third time I'm asking!") does not either --
the prompt handles judgement calls; this only catches the unambiguous case.
"""

from __future__ import annotations

import re

_HUMAN = r"(?:real\s+|actual\s+|live\s+)?(?:human(?:\s+being)?|person|agent|representative|rep|manager|supervisor|someone)"

_PATTERNS = [
    re.compile(rf"\b(?:speak|talk|chat)\s+(?:to|with)\s+(?:a\s+|an\s+|your\s+|some\s+)?{_HUMAN}\b", re.I),
    re.compile(rf"\b(?:get|give|transfer|connect|put)\s+me\s+(?:to\s+|through\s+to\s+|with\s+)?(?:a\s+|an\s+)?{_HUMAN}\b", re.I),
    re.compile(rf"\b(?:i\s+want|i\s+need|i'd\s+like|i\s+would\s+like)\s+(?:a\s+|an\s+|to\s+speak\s+to\s+a\s+)?{_HUMAN}\b", re.I),
    re.compile(r"\b(?:escalate\s+(?:this|me)|human\s+please|a\s+human,?\s+please)\b", re.I),
]

_NEGATION = re.compile(r"\b(?:don't|do\s+not|no\s+need\s+to|never|not)\s+(?:\w+\s+){0,3}$", re.I)


def explicit_human_request(text: str) -> bool:
    for pattern in _PATTERNS:
        for match in pattern.finditer(text):
            if not _NEGATION.search(text[: match.start()]):
                return True
    return False
