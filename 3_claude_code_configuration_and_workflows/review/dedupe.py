"""The core of self-check #4: don't re-post a finding a prior run already
flagged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .findings import Finding

MARKER_RE = re.compile(r"<!-- claude-review:([0-9a-f]{16}) -->")


@dataclass(frozen=True)
class DedupeResult:
    to_post: list[Finding] = field(default_factory=list)
    skipped: list[Finding] = field(default_factory=list)


def dedupe(findings: list[Finding], prior_fingerprints: set[str]) -> DedupeResult:
    to_post: list[Finding] = []
    skipped: list[Finding] = []
    seen_this_run: set[str] = set()
    for finding in findings:
        fp = finding.fingerprint()
        if fp in prior_fingerprints or fp in seen_this_run:
            skipped.append(finding)
        else:
            to_post.append(finding)
            seen_this_run.add(fp)
    return DedupeResult(to_post=to_post, skipped=skipped)


def extract_fingerprints(comment_bodies: list[str]) -> set[str]:
    """Recover which findings a prior run already posted, from the hidden
    marker each posted comment carries."""
    found: set[str] = set()
    for body in comment_bodies:
        found.update(MARKER_RE.findall(body or ""))
    return found
