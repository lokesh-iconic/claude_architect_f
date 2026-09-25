"""Batch path: the Message Batches API for large, latency-tolerant runs.

Each document is one request whose `custom_id` is its doc id. Results arrive
in arbitrary order and are joined back by `custom_id` only. After each round
every document lands in exactly one bucket:

- finished         -> accepted or routed to review; never resubmitted
- transient        -> `errored` (non-request error), `expired`, `canceled`, or
                      missing from the results: resubmitted with the same request
- validation retry -> succeeded but failed a retryable check: resubmitted
                      with the failed extraction + specific error appended
- rejected request -> `errored` with `invalid_request`: resending the same body
                      cannot succeed, so it goes to review instead

The next round's request list is built from the resubmit buckets alone, so
only documents that actually failed are sent again.
"""

from __future__ import annotations

import hashlib
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Protocol

from .backend import MockBackend, ModelTurn, turn_from_message
from ..extraction.errors import ExtractionError
from ..extraction.models import Document, DocumentResult
from ..extraction.pipeline import DocState, build_params, fail, finalize, step
from ..config.settings import Settings

CUSTOM_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


@dataclass
class BatchItemResult:
    custom_id: str
    result_type: str  # succeeded | errored | canceled | expired
    turn: ModelTurn | None = None
    error_type: str | None = None

    @property
    def request_rejected(self) -> bool:
        return self.result_type == "errored" and "invalid_request" in (self.error_type or "")


class BatchClient(Protocol):
    def create(self, requests: list[dict[str, Any]]) -> str: ...
    def status(self, batch_id: str) -> str: ...
    def results(self, batch_id: str) -> Iterable[BatchItemResult]: ...


class LiveBatchClient:
    def __init__(self, settings: Settings) -> None:
        import anthropic

        self.client = anthropic.Anthropic(api_key=settings.api_key, timeout=60.0, max_retries=2)

    def create(self, requests: list[dict[str, Any]]) -> str:
        from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
        from anthropic.types.messages.batch_create_params import Request

        batch = self.client.messages.batches.create(requests=[
            Request(custom_id=r["custom_id"], params=MessageCreateParamsNonStreaming(**r["params"]))
            for r in requests
        ])
        return batch.id

    def status(self, batch_id: str) -> str:
        return self.client.messages.batches.retrieve(batch_id).processing_status

    def results(self, batch_id: str) -> Iterable[BatchItemResult]:
        for row in self.client.messages.batches.results(batch_id):
            kind = row.result.type
            if kind == "succeeded":
                yield BatchItemResult(row.custom_id, kind, turn=turn_from_message(row.result.message))
            elif kind == "errored":
                err = row.result.error
                inner = getattr(err, "error", None)
                etype = getattr(inner, "type", None) or getattr(err, "type", None) or "unknown"
                yield BatchItemResult(row.custom_id, kind, error_type=str(etype))
            else:
                yield BatchItemResult(row.custom_id, kind)


def _bucket(custom_id: str, round_no: int) -> int:
    return int(hashlib.sha256(f"{custom_id}|{round_no}".encode()).hexdigest()[:8], 16) % 100


class MockBatchClient:
    """Simulates the Batches API over the mock backend.

    Failure injection is a deterministic hash of (custom_id, round): in round 1
    about 6% of rows error transiently, 3% expire, and 1% are rejected as
    invalid requests; in later rounds 3% error transiently. Results come back
    shuffled, so anything joining by position instead of custom_id breaks.
    """

    def __init__(self, backend: MockBackend) -> None:
        self.backend = backend
        self.submissions: list[list[str]] = []
        self._batches: dict[str, list[dict[str, Any]]] = {}

    def create(self, requests: list[dict[str, Any]]) -> str:
        self.submissions.append([r["custom_id"] for r in requests])
        batch_id = f"msgbatch_mock_{len(self.submissions):02d}"
        self._batches[batch_id] = requests
        return batch_id

    def status(self, batch_id: str) -> str:
        return "ended"

    def results(self, batch_id: str) -> Iterable[BatchItemResult]:
        round_no = int(batch_id.rsplit("_", 1)[1])
        rows: list[BatchItemResult] = []
        for req in self._batches[batch_id]:
            cid, b = req["custom_id"], _bucket(req["custom_id"], round_no)
            if round_no == 1 and b < 6 or round_no > 1 and b < 3:
                rows.append(BatchItemResult(cid, "errored", error_type="api_error"))
            elif round_no == 1 and b < 9:
                rows.append(BatchItemResult(cid, "expired"))
            elif round_no == 1 and b < 10:
                rows.append(BatchItemResult(cid, "errored", error_type="invalid_request"))
            else:
                rows.append(BatchItemResult(cid, "succeeded", turn=self.backend.respond(req["params"])))
        random.Random(batch_id).shuffle(rows)
        return rows


@dataclass
class RoundRecord:
    round: int
    batch_id: str
    submitted: list[str]
    succeeded: int = 0
    errored: int = 0
    expired: int = 0
    canceled: int = 0
    missing: list[str] = field(default_factory=list)
    transient_retry: list[str] = field(default_factory=list)
    validation_retry: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    finished: list[str] = field(default_factory=list)

    @property
    def resubmit(self) -> list[str]:
        return self.transient_retry + self.validation_retry


@dataclass
class BatchRun:
    rounds: list[RoundRecord]
    results: dict[str, DocumentResult]


def wait_for(
    client: BatchClient,
    batch_id: str,
    settings: Settings,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    deadline = clock() + settings.batch_poll_timeout_s
    while True:
        status = client.status(batch_id)
        if status == "ended":
            return
        if clock() >= deadline:
            raise ExtractionError(
                f"batch {batch_id} still {status!r} after {settings.batch_poll_timeout_s:.0f}s",
                category="transient",
                remediation=(
                    "Batches can take up to 24h. Raise IE_BATCH_POLL_TIMEOUT, or fetch results later "
                    f"with client.messages.batches.results({batch_id!r}); they are kept for 29 days."
                ),
            )
        sleep(settings.batch_poll_interval_s)


def run_batch(
    docs: list[Document],
    client: BatchClient,
    settings: Settings,
    *,
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] | None = None,
) -> BatchRun:
    bad = [d.doc_id for d in docs if not CUSTOM_ID_RE.match(d.doc_id)]
    if bad or len({d.doc_id for d in docs}) != len(docs):
        raise ExtractionError(
            f"invalid or duplicate custom_ids: {bad[:5]}", category="configuration",
            remediation="custom_id must be unique and match ^[a-zA-Z0-9_-]{1,64}$.",
        )
    states = {d.doc_id: DocState.start(d) for d in docs}
    pending = [d.doc_id for d in docs]
    rounds: list[RoundRecord] = []

    for round_no in range(1, settings.batch_max_rounds + 1):
        if not pending:
            break
        requests = [{"custom_id": cid, "params": build_params(settings, states[cid].messages)} for cid in pending]
        batch_id = client.create(requests)
        rec = RoundRecord(round_no, batch_id, list(pending))
        if log:
            log(f"  round {round_no}: submitted {len(pending)} request(s) as {batch_id}")
        wait_for(client, batch_id, settings, sleep=sleep)
        by_id = {row.custom_id: row for row in client.results(batch_id)}

        for cid in pending:
            row = by_id.get(cid)
            if row is None:
                rec.missing.append(cid)
                rec.transient_retry.append(cid)
                continue
            if row.result_type == "succeeded":
                rec.succeeded += 1
                assert row.turn is not None
                if step(states[cid], row.turn, settings):
                    rec.validation_retry.append(cid)
                else:
                    rec.finished.append(cid)
            elif row.request_rejected:
                rec.errored += 1
                rec.rejected.append(cid)
                fail(states[cid], ExtractionError(f"batch request rejected ({row.error_type})",
                                                  category="configuration"))
            else:
                rec.errored += row.result_type == "errored"
                rec.expired += row.result_type == "expired"
                rec.canceled += row.result_type == "canceled"
                rec.transient_retry.append(cid)
        rounds.append(rec)
        if log:
            log(f"    {rec.succeeded} succeeded, {len(rec.transient_retry)} transient, "
                f"{len(rec.validation_retry)} validation retry, {len(rec.rejected)} rejected")
        pending = rec.resubmit

    for cid in pending:
        states[cid].stop_note = f"batch_rounds_exhausted after {settings.batch_max_rounds} rounds"
        states[cid].done = True

    return BatchRun(rounds, {cid: finalize(s, settings) for cid, s in states.items()})
