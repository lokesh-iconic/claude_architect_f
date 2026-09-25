"""A small gitignore-style glob matcher, hand-rolled rather than a new
dependency (`**` means zero or more path segments, matched by trying every
split point -- a single translated regex gets the zero-segment case wrong
because it forces a literal separator around `**`).
"""

from __future__ import annotations

import fnmatch


def _match_segments(pattern: list[str], path: list[str]) -> bool:
    if not pattern:
        return not path
    head, rest = pattern[0], pattern[1:]
    if head == "**":
        if not rest:
            return True
        return any(_match_segments(rest, path[i:]) for i in range(len(path) + 1))
    if not path:
        return False
    if not fnmatch.fnmatchcase(path[0], head):
        return False
    return _match_segments(rest, path[1:])


def glob_match(pattern: str, path: str) -> bool:
    return _match_segments(pattern.split("/"), path.split("/"))
