"""Sample issue-tracker dataset.

Stands in for an internal tracker. Issues reference real paths inside
`sample_service/`, so an agent can cross-reference the tracker against the
codebase with Grep and Read.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

PROJECTS: dict[str, dict[str, str]] = {
    "CHK": {"name": "Checkout", "lead": "priya", "repo_path": "sample_service/checkout"},
    "AUTH": {"name": "Identity", "lead": "marcus", "repo_path": "sample_service/auth"},
    "PLAT": {"name": "Platform", "lead": "dana", "repo_path": "sample_service/platform"},
}

STATUSES = ("open", "in_progress", "in_review", "closed")
PRIORITIES = ("P0", "P1", "P2", "P3")
CURRENT_SPRINT = "2026-S18"


@dataclass(frozen=True)
class Issue:
    key: str
    project: str
    title: str
    description: str
    status: str
    priority: str
    assignee: str
    reporter: str
    sprint: str | None
    labels: tuple[str, ...]
    paths: tuple[str, ...]
    created: str
    updated: str
    comments: tuple[dict[str, str], ...] = field(default=())

    def summary(self) -> dict[str, Any]:
        """The compact shape used in list/search results."""
        return {
            "key": self.key,
            "project": self.project,
            "title": self.title,
            "status": self.status,
            "priority": self.priority,
            "assignee": self.assignee,
            "updated": self.updated,
        }

    def detail(self) -> dict[str, Any]:
        """The full shape, returned only by get_issue."""
        return {
            **self.summary(),
            "description": self.description,
            "reporter": self.reporter,
            "sprint": self.sprint,
            "labels": list(self.labels),
            "linkedPaths": list(self.paths),
            "created": self.created,
            "comments": [dict(c) for c in self.comments],
        }


ISSUES: tuple[Issue, ...] = (
    Issue(
        key="CHK-104",
        project="CHK",
        title="Discount codes stack when they should be exclusive",
        description=(
            "Applying two promotional codes in the same cart multiplies the "
            "discount instead of rejecting the second code. Reproduced on "
            "staging with SAVE10 and WELCOME15."
        ),
        status="in_progress",
        priority="P1",
        assignee="priya",
        reporter="sam",
        sprint=CURRENT_SPRINT,
        labels=("bug", "revenue"),
        paths=("sample_service/checkout/pricing.py", "sample_service/checkout/cart.py"),
        created="2026-04-02",
        updated="2026-05-11",
        comments=(
            {"author": "priya", "date": "2026-05-11", "body": "Root cause is in apply_discounts; it sums rather than picks the best code."},
        ),
    ),
    Issue(
        key="CHK-118",
        project="CHK",
        title="Cart totals drift by one cent on split payments",
        description=(
            "Rounding is applied per payment method rather than to the order "
            "total, so a split payment can be off by a cent."
        ),
        status="open",
        priority="P2",
        assignee="unassigned",
        reporter="priya",
        sprint=CURRENT_SPRINT,
        labels=("bug", "accounting"),
        paths=("sample_service/checkout/pricing.py",),
        created="2026-04-28",
        updated="2026-05-06",
    ),
    Issue(
        key="CHK-121",
        project="CHK",
        title="Add idempotency keys to order submission",
        description=(
            "A double-click on submit creates two orders. Accept a client "
            "supplied idempotency key and de-duplicate on it."
        ),
        status="in_review",
        priority="P1",
        assignee="lee",
        reporter="dana",
        sprint=CURRENT_SPRINT,
        labels=("reliability",),
        paths=("sample_service/checkout/orders.py",),
        created="2026-05-01",
        updated="2026-05-12",
    ),
    Issue(
        key="AUTH-77",
        project="AUTH",
        title="Session tokens survive a password change",
        description=(
            "Changing a password does not invalidate existing sessions, so a "
            "stolen token keeps working. Security review flagged this as P0."
        ),
        status="in_progress",
        priority="P0",
        assignee="marcus",
        reporter="security-review",
        sprint=CURRENT_SPRINT,
        labels=("security", "bug"),
        paths=("sample_service/auth/session.py", "sample_service/auth/passwords.py"),
        created="2026-03-19",
        updated="2026-05-12",
        comments=(
            {"author": "marcus", "date": "2026-05-12", "body": "Need a token version column before this can ship."},
        ),
    ),
    Issue(
        key="AUTH-83",
        project="AUTH",
        title="Rate limit failed login attempts per account",
        description=(
            "Only per-IP limiting exists today, so a distributed attempt "
            "against one account is not slowed down at all."
        ),
        status="open",
        priority="P1",
        assignee="unassigned",
        reporter="marcus",
        sprint=None,
        labels=("security",),
        paths=("sample_service/auth/session.py",),
        created="2026-04-11",
        updated="2026-04-30",
    ),
    Issue(
        key="AUTH-90",
        project="AUTH",
        title="Password reset emails render the raw template on mobile",
        description="The HTML part is missing a doctype, so some clients show the source.",
        status="closed",
        priority="P3",
        assignee="marcus",
        reporter="support",
        sprint=None,
        labels=("bug", "email"),
        paths=("sample_service/auth/passwords.py",),
        created="2026-02-08",
        updated="2026-03-02",
    ),
    Issue(
        key="PLAT-31",
        project="PLAT",
        title="Retry wrapper swallows the original exception",
        description=(
            "When all retries are exhausted the wrapper raises a generic "
            "RetryError, discarding the underlying cause and its traceback."
        ),
        status="open",
        priority="P1",
        assignee="dana",
        reporter="lee",
        sprint=CURRENT_SPRINT,
        labels=("observability",),
        paths=("sample_service/platform/retry.py",),
        created="2026-04-22",
        updated="2026-05-09",
    ),
    Issue(
        key="PLAT-44",
        project="PLAT",
        title="Structured logs lose the request id inside background tasks",
        description="The contextvar is not copied when a task is scheduled.",
        status="in_review",
        priority="P2",
        assignee="dana",
        reporter="priya",
        sprint=CURRENT_SPRINT,
        labels=("observability",),
        paths=("sample_service/platform/logging_setup.py",),
        created="2026-04-25",
        updated="2026-05-10",
    ),
    Issue(
        key="PLAT-52",
        project="PLAT",
        title="Drop Python 3.9 from the support matrix",
        description="CI still builds 3.9; nothing deployed runs it any more.",
        status="closed",
        priority="P3",
        assignee="lee",
        reporter="dana",
        sprint=None,
        labels=("chore",),
        paths=(),
        created="2026-01-14",
        updated="2026-02-20",
    ),
)

# Issues the caller is not allowed to read, to exercise the permission path.
RESTRICTED_KEYS = frozenset({"AUTH-99"})
