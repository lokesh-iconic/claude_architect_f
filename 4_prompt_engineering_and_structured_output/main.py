"""CLI for the structured-extraction pipeline.

    uv run python main.py all

Every run writes its reports, traces, and review queue into ./output/, which
is git-ignored.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = MODULE_DIR / "output"

sys.path.insert(0, str(MODULE_DIR))

from invoice_extractor.reporting import report  # noqa: E402
from invoice_extractor.backends.backend import MockBackend, make_backend  # noqa: E402
from invoice_extractor.backends.batch import LiveBatchClient, MockBatchClient, run_batch  # noqa: E402
from invoice_extractor.evaluation.corpus import load_corpus, synthesize  # noqa: E402
from invoice_extractor.extraction.errors import ExtractionError  # noqa: E402
from invoice_extractor.evaluation.evaluate import evaluate  # noqa: E402
from invoice_extractor.extraction.pipeline import extract_all  # noqa: E402
from invoice_extractor.extraction.schema import extraction_tool  # noqa: E402
from invoice_extractor.config.settings import Settings, load_settings  # noqa: E402


class Writer:
    def __init__(self, quiet: bool, output_dir: Path = OUTPUT_DIR) -> None:
        self.quiet = quiet
        self.output_dir = output_dir
        self.written: list[Path] = []

    def write(self, name: str, suffix: str, content: str) -> Path:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        path = self.output_dir / f"{name}{suffix}"
        path.write_text(content, encoding="utf-8")
        self.written.append(path)
        if not self.quiet:
            print(f"  wrote {path.relative_to(self.output_dir.parent)}")
        return path


def cmd_extract(args: argparse.Namespace, settings: Settings, out: Writer) -> str:
    docs, truth = load_corpus(settings.corpus_dir)
    if not docs:
        raise SystemExit(f"no *.txt documents in {settings.corpus_dir}")
    backend = make_backend(settings, truth)

    def progress(result):
        if not args.quiet:
            print(f"  {result.doc_id:<36} attempts={result.attempt_count} {result.status}")

    results = extract_all(docs, backend, settings, progress)
    summary = evaluate(results, truth)
    markdown = report.render_extract(results, summary, settings)
    out.write("extract", ".md", markdown)
    out.write("extract", ".json", report.trace_json(results, summary))
    out.write("review-queue-extract", ".jsonl", report.review_queue_jsonl(results))
    return markdown


def cmd_batch(args: argparse.Namespace, settings: Settings, out: Writer) -> str:
    docs, truth = synthesize(args.batch_size)
    if settings.is_live:
        client = LiveBatchClient(settings)
    else:
        client = MockBatchClient(MockBackend(truth))
    log = None if args.quiet else print
    run = run_batch(docs, client, settings, log=log)
    results = list(run.results.values())
    summary = evaluate(results, truth)
    markdown = report.render_batch(run, summary, settings)
    rounds = [
        {"round": r.round, "batch_id": r.batch_id, "submitted": r.submitted,
         "transient_retry": r.transient_retry, "validation_retry": r.validation_retry,
         "rejected": r.rejected}
        for r in run.rounds
    ]
    out.write("batch", ".md", markdown)
    out.write("batch", ".json", report.trace_json(results, summary, {"rounds": rounds}))
    out.write("review-queue-batch", ".jsonl", report.review_queue_jsonl(results))
    return markdown


def cmd_schema(args: argparse.Namespace, settings: Settings, out: Writer) -> str:
    content = json.dumps(extraction_tool(), indent=2)
    out.write("schema", ".json", content)
    return content


COMMANDS = {"extract": cmd_extract, "batch": cmd_batch, "schema": cmd_schema}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="invoice-extractor",
        description="Structured invoice extraction: strict tool schema, validation-retry, batches, review routing.",
    )
    parser.add_argument("command", nargs="?", default="all", choices=["all", *COMMANDS],
                        help="Which step to run. Default: all (extract, then batch).")
    parser.add_argument("--mode", choices=["auto", "live", "mock"], default="auto",
                        help="auto (default) uses the API key if it validates, otherwise mock.")
    parser.add_argument("--corpus", type=Path, help="Directory of *.txt documents (default: bundled corpus).")
    parser.add_argument("--threshold", type=float, help="Confidence below this routes to review (default 0.8).")
    parser.add_argument("--max-attempts", type=int, help="Validation-retry budget per document (default 3).")
    parser.add_argument("--batch-size", type=int, default=120, help="Synthetic documents in the batch run.")
    parser.add_argument("--print", dest="echo", action="store_true", help="Also print reports.")
    parser.add_argument("--quiet", action="store_true", help="Suppress progress output.")
    return parser


def run(args: argparse.Namespace, output_dir: Path = OUTPUT_DIR) -> int:
    settings = load_settings(requested_mode=args.mode)
    if args.corpus:
        settings.corpus_dir = args.corpus
    if args.threshold is not None:
        settings.confidence_threshold = args.threshold
    if args.max_attempts is not None:
        settings.max_attempts = args.max_attempts
    if not args.quiet:
        print(f"Mode: {settings.mode} ({settings.mode_reason})")

    out = Writer(args.quiet, output_dir)
    chosen = ["extract", "batch"] if args.command == "all" else [args.command]
    for name in chosen:
        if not args.quiet:
            print(f"\n== {name}")
        try:
            content = COMMANDS[name](args, settings, out)
        except ExtractionError as exc:
            print(json.dumps(exc.to_payload(), indent=2), file=sys.stderr)
            return 1
        if args.echo:
            print("\n" + content)

    if not args.quiet:
        print(f"\n{len(out.written)} file(s) in {output_dir.relative_to(output_dir.parent)}/")
    return 0


def main() -> int:
    args = build_parser().parse_args()
    try:
        return run(args)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
