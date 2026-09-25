# Structured Invoice Extraction Pipeline

## Problem statement

A client needs structured line-item data pulled out of unstructured documents
(invoices, credit notes, receipts and similar), with validated totals, and
anything uncertain sent to a human for review. Build an extraction pipeline
that does this.

What it has to prove:

- Structured output enforced with `tool_use` and a JSON schema, not just by
  asking for it in the prompt
- Few-shot prompting that copes with ambiguous or unusual input formats
- Validation, retry, and feedback loops that improve extraction quality
- A batch strategy that fits the latency and cost constraints

The document type is **invoices and their relatives**: 12 varied samples in
[`corpus/`](invoice_extractor/corpus/), each with a hand-labelled ground truth.
With no API key the whole pipeline still runs, on a mock backend that goes
through the same validation, retry, batching, and routing code.

---

## Quick start

From this directory:

```bash
uv sync
uv run python main.py all
```

That runs the synchronous extraction over the corpus, then a 120-document batch
run, and writes everything into [`output/`](.):

```
  wrote output/extract-<timestamp>.md
  wrote output/extract-<timestamp>.json
  wrote output/review-queue-extract-<timestamp>.jsonl
  wrote output/batch-<timestamp>.md
  wrote output/batch-<timestamp>.json
  wrote output/review-queue-batch-<timestamp>.jsonl
```

Without a valid key the run prints `Mode: mock (no ANTHROPIC_API_KEY ...)`, and
every report opens with a banner saying so.

---

## How it works

```
 document ──▶ strict, forced tool call ──▶ validate ──┬─ clean ─────────────▶ confidence gate ─▶ accepted
 (+ few-shot    record_extraction          format     │                                   │
  in system)                               grounding  ├─ retryable ─▶ feedback turn ──┐   └─▶ review queue
                                           arithmetic │   (failed extraction +        │
                                           required   │    the specific errors)       │
                                                      │        ▲                      │
                                                      │        └──── re-validate ◀────┘
                                                      └─ not retryable / unchanged repeat ─▶ review queue
```

### The schema is the contract

[`schema.py`](invoice_extractor/schema.py) defines one tool, `record_extraction`,
with `strict: true` and `tool_choice: {"type": "tool"}`. The API then guarantees
the output matches the schema, whatever the prompt says. Every property is
`required`, and any field a document might not have is typed
`["string", "null"]`. That makes "absent" a legal answer the model has to give
on purpose, not a key it leaves out and a parser later fills with a default.
Each header field is `{value, evidence, confidence}`, and each line item has
its own evidence and confidence.

`document_type` and each line's `category` are enums that end in `other`, and
each has a nullable `*_detail` free-text field. The validator requires the
detail whenever `other` is used, so a keg deposit comes back as
`category: "other", category_detail: "returnable keg deposit"`. That keeps the
enum closed without losing information about lines that fit none of the
categories.

### Validation sorts every issue into retryable or not

[`validation.py`](invoice_extractor/validation.py) checks four things.

| Kind | Examples | Retryable? |
|---|---|---|
| format | non-ISO date, `$` instead of `USD`, `other` without a detail | yes |
| grounding | the evidence quote isn't in the source (whitespace-normalised), or the number isn't in its own quote | yes: the fix is either the right quote or null |
| arithmetic | line items don't sum to the subtotal, subtotal + tax ≠ total, qty × price ≠ amount | yes, once (see below) |
| absent | a field needed for posting (`vendor_name`, `invoice_number`, `currency`, `total`) is null | **no**: goes straight to review |

The grounding check is what catches fabrication. A made-up due date needs a
made-up quote to support it, and a made-up quote isn't in the document.

### The retry loop

[`pipeline.py`](invoice_extractor/pipeline.py). If an attempt has retryable
issues, the next request contains the document, then the failed extraction as
the model's own `tool_use` turn, then a `tool_result` with `is_error: true`
listing each specific error with its code, field, figures and remediation. For
example: *"4 line items sum to 4837.00 but the subtotal implies 5102.00
(difference +265.00)"*.

Two design decisions matter here.

- **Absent-field issues are left out of the feedback.** Telling the model
  "invoice_number is missing" is an invitation to supply one. Those issues go
  to review and are never sent back to the model.
- **An unchanged repeat stops the loop.** The pipeline can't tell a model slip
  from a document that genuinely doesn't add up without asking once. The
  arithmetic feedback says *"if the document itself does not add up, return
  the figures exactly as printed"*. If the next attempt returns exactly the
  same failing figures, the source is inconsistent, and further retries would
  only spend money. The document goes to review as
  `persisted_after_feedback`.

### Few-shot examples

[`few_shot.py`](invoice_extractor/few_shot.py) holds three examples, rendered
into the system prompt. They were picked for the cases the rules handle worst:

1. A credit memo: negative signs, plus a PO and due date that are absent and
   must come back null rather than be borrowed from the invoice it references.
2. A German invoice: `1.780,00` number format, `DD.MM.YYYY` dates, and a line
   that has to use the `other` + detail pattern.
3. An informal email: no number, no date, no table, and "dollars" as the
   currency, so most header fields are null and the rest carry honestly low
   confidence.

None of them is in the evaluation corpus. A test runs each example through the
pipeline's own validator against its own document, so an example can't teach
something the validator would reject.

### Batch path

[`batch.py`](invoice_extractor/batch.py) sends each document as one Message
Batches request, with `custom_id` set to the doc id. The request body is the
same `build_params()` dict the synchronous path sends. Results come back in
any order and are matched to documents by `custom_id` only. After each round,
every document is in exactly one bucket:

| Bucket | Cause | Next round |
|---|---|---|
| finished | accepted, or routed to review | not resubmitted |
| transient | `errored` (not a request error), `expired`, `canceled`, missing from results | resubmitted with the same request |
| validation retry | succeeded, but failed a retryable check | resubmitted with the feedback turn appended |
| rejected | `errored` / `invalid_request` | not resubmitted (the same body would fail again); goes to review |

The next round is built from the two resubmit buckets and nothing else. Polling
has a deadline (`IE_BATCH_POLL_TIMEOUT`). When it's reached, the run fails with
a structured error that gives the batch id and says how to fetch the results
later, rather than hanging.

**When to use which path.** The synchronous path is for latency-sensitive work,
such as a single upload a user is waiting on. Batches cost 50% less but can
take up to 24h, so they suit nightly or backfill runs of 100+ documents where
nobody is waiting. The system prompt and few-shot block are identical on every
request and carry `cache_control`, so they're cacheable on both paths.

### Confidence routing

Any header field or line item with confidence below the threshold (default
0.8, `--threshold`) sends the document to review. Unresolved issues do too. The
review queue is a JSONL file under `output/`, one row per document, with the
reasons, the low-confidence fields, the unresolved issues, and the extraction
for the reviewer to start from.

---

## Self-check

The three questions from the brief. Every figure below comes from a mock-mode
run (`uv run python main.py all`).

| # | Question | Answer | How to verify |
|---|---|---|---|
| 1 | When a field is genuinely missing from the source, does the pipeline return null, or fabricate a plausible value? | **Null, and this is enforced by the program.** The schema makes null a required, legal answer, and the grounding check rejects any value whose quote isn't in the document. Corpus: all **22 of 22** absent header fields came back null. The mock's one scripted fabrication (a due date on `inv_002`, with a made-up quote) was flagged `evidence_not_in_source` and corrected to null on attempt 2. **0 fabrications in the final output.** Batch: 108/108 absent fields null, 2 first-attempt fabrications caught, 0 in the final output. **Caveat:** in mock mode the fabrications are scripted, so this shows the pipeline catches fabrication, not how often Claude fabricates. TODO: live-mode fabrication rate. Requires a live-mode run. | `uv run python main.py extract`, then read *Accuracy against ground truth*; `uv run pytest -k fabricat` |
| 2 | Does the retry loop tell a fixable formatting error apart from information that isn't in the source (where retrying won't help)? | **Yes.** Absent data gets **zero retries**: `inv_006` (a receipt with no invoice number) makes 1 call and goes to review as `absent_from_source`. Fixable errors are corrected by feedback: `inv_002` (grounding), `inv_005` (dropped line), and `inv_008` (`other` with no detail) are each accepted on attempt 2. A source that really doesn't add up (`inv_007`: its lines total 1,150 but its subtotal says 1,200) gets **one** retry, comes back unchanged, and stops, even with `--max-attempts 5`. **Caveat:** an arithmetic mismatch always costs that one retry, because only the repeat shows whether the model or the document is wrong. | `uv run pytest -k "absent or inconsistency or fixable"` |
| 3 | Does the batch job resubmit only the documents that actually failed, not the whole set? | **Yes.** Round 1 submitted 120 documents. **Round 2 submitted 18**: 9 transient failures (5 errored, 4 expired) plus 9 validation retries. **Round 3 submitted 1.** Each round's submitted set is checked to equal the previous round's failed-and-retryable set. The 2 `invalid_request` rows are never resubmitted and go to review. A test returns results in reverse order to confirm the join is by `custom_id`, not by position. **Caveat:** the mock batch client injects failures deterministically (about 6% errored, 3% expired, 1% rejected). TODO: a live Batches run. Requires a live-mode run. | `uv run python main.py batch`, then read *Resubmission check*; `uv run pytest -k batch` |

---

## Commands

All commands run from this directory.

| Command | What it does |
|---|---|
| `uv run python main.py all` | Synchronous extraction over the corpus, then the batch run (default) |
| `uv run python main.py extract` | Corpus only: validation-retry loop, accuracy, review queue |
| `uv run python main.py batch` | 120 synthetic documents through the batch path |
| `uv run python main.py schema` | Write the exact tool definition sent to the API |
| `... --mode mock` / `--mode live` | Force the backend; `live` fails loudly without a working key |
| `... --threshold 0.7` | Confidence below this routes to review |
| `... --max-attempts 5` | Validation-retry budget per document, counting the first attempt |
| `... --batch-size 300` | Synthetic documents in the batch run |
| `... --corpus ./my_invoices` | Extract your own `*.txt` files (accuracy is scored only where ground truth exists) |
| `... --print` | Also echo reports to the terminal |
| `... --quiet` | Suppress progress output |
| `uv run pytest 4_prompt_engineering_and_structured_output/tests` | Run the 38 tests (from the repo root) |

---

## Live vs mock mode

Mode is resolved once at startup by [`settings.py`](invoice_extractor/settings.py):

1. `--mode mock` always gives mock.
2. With no `ANTHROPIC_API_KEY`, the mode is mock.
3. If a key is present, it's validated with `GET /v1/models/{id}` (zero tokens).
   A 401, 403 or 404, or a connection failure, falls back to mock and prints
   the reason.
4. `--mode live` turns each of those fallbacks into a hard error.

| | live | mock |
|---|---|---|
| Extraction | Claude, via strict forced tool call | hand-labelled ground truth |
| First-attempt defects | whatever the model does | scripted per document (`mock_fault` in `ground_truth.json`) |
| Confidence | model-reported | 0.95, or 0.6 for fields labelled ambiguous |
| Validation, retry, routing, evaluation | identical | identical |
| Batches | `client.messages.batches.*` | in-memory client with deterministic failure injection, results shuffled |

**How the mock is kept honest.** A document with a scripted defect keeps coming
back wrong until the request contains a `tool_result` error naming the matching
issue code. A retry that just re-asks, without sending the specific error, never
gets a clean answer. A test checks this, so the retry tests can't pass on a
mock that fixes itself.

Live calls use `claude-opus-5` with adaptive thinking (the model default) at
effort `medium`, and server-side refusal fallback (`fallbacks: "default"`) on
the synchronous path only, because the Batches API rejects it. On models that
reject forced `tool_choice` (`claude-opus-5-5`, `claude-fable-5-1`), the
request uses `auto`, and a turn without the tool call counts as a retryable
issue.

Configuration lives in the project `.env`: `IE_MODEL`, `IE_EFFORT`,
`IE_CONFIDENCE_THRESHOLD`, `IE_MAX_ATTEMPTS`, `IE_REQUEST_TIMEOUT`,
`IE_REFUSAL_FALLBACK`, `IE_BATCH_POLL_INTERVAL`, `IE_BATCH_POLL_TIMEOUT`, and
`IE_BATCH_MAX_ROUNDS`.

---

## Layout

| File | Role |
|---|---|
| [`main.py`](main.py) | CLI; writes every run into `output/` |
| [`settings.py`](invoice_extractor/settings.py) | `.env` loading, live/mock resolution, key validation |
| [`schema.py`](invoice_extractor/schema.py) | The strict extraction tool, enums, `tool_choice` per model |
| [`prompts.py`](invoice_extractor/prompts.py) | System prompt, few-shot rendering, retry feedback turn |
| [`few_shot.py`](invoice_extractor/few_shot.py) | The three few-shot examples and why each was chosen |
| [`validation.py`](invoice_extractor/validation.py) | Format, grounding, arithmetic, and required-field checks |
| [`pipeline.py`](invoice_extractor/pipeline.py) | The retry loop (`step`), stop rules, confidence routing |
| [`batch.py`](invoice_extractor/batch.py) | Batches client (live and mock), rounds, failed-only resubmission |
| [`backend.py`](invoice_extractor/backend.py) | `LiveBackend` (Messages API) and `MockBackend` |
| [`models.py`](invoice_extractor/models.py) | Dataclasses for extractions, attempts, results |
| [`errors.py`](invoice_extractor/errors.py) | `ExtractionError` and `ValidationIssue` (category, `isRetryable`, remediation) |
| [`corpus.py`](invoice_extractor/corpus.py) | Corpus + ground truth loading, synthetic batch generator |
| [`evaluate.py`](invoice_extractor/evaluate.py) | Field-level scoring; fabrication counted separately |
| [`report.py`](invoice_extractor/report.py) | Markdown reports, JSON traces, review-queue JSONL |
| [`corpus/`](invoice_extractor/corpus/) | 12 sample documents and `ground_truth.json` |
| [`tests/`](tests/) | 38 tests, none needing a key or the network |
| `output/` | Reports, traces, and review queues, written at runtime |

---

## Known limits

- Live mode has not been verified end to end yet. The request shape follows
  the Anthropic Python SDK docs for 1.x (strict tools, `output_config.effort`,
  `messages.batches.*`). The live classes are imported lazily and aren't
  exercised by any test, since tests never need a key or the network.
- The model reports its own confidence scores, and nothing calibrates them. The
  threshold is only as good as that self-report, plus the program's own
  signals (grounding and arithmetic failures). Calibrating against reviewer
  decisions would need data this module doesn't collect. TODO.
- Grounding checks that a quote is in the document. It doesn't check that the
  quote supports the value, apart from numbers and identifiers. A correct quote
  paired with a wrongly converted date (e.g. `03/04/2026` read in the wrong
  locale) passes. Low confidence is the only defence there.
- When the model returns null for a required field that *is* in the document,
  it looks exactly like a genuinely absent field. The pipeline routes it to
  review either way, so it's caught, but at the cost of human time rather than
  a retry.
- The corpus is plain text. PDFs or scans would need a document content block
  or an OCR step first. `inv_009` imitates OCR noise, but it's still text.
- The batch run has no resume command. If polling hits its deadline, the batch
  id is in the error, but continuing the run means fetching the results by
  hand.
