"""CLI entry point for the multi-agent research coordinator.

    uv run python main.py "your topic"

Every run writes its report and trace into ./output/, which is git-ignored.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
import time
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = MODULE_DIR / "output"

sys.path.insert(0, str(MODULE_DIR))

from research_coordinator import (  # noqa: E402
    Orchestrator,
    load_settings,
    render_markdown,
    render_trace,
)

DEFAULT_TOPIC = "the impact of AI on creative industries"


def slugify(topic: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", topic.lower()).strip("-")
    return (slug[:50].rstrip("-") or "report")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="research-coordinator",
        description="Coordinator agent that delegates research to specialist subagents.",
    )
    parser.add_argument("topic", nargs="?", default=DEFAULT_TOPIC, help="Topic to research.")
    parser.add_argument(
        "--mode",
        choices=["auto", "live", "mock"],
        default="auto",
        help="auto (default) uses the API key if it validates, otherwise falls back to mock.",
    )
    parser.add_argument(
        "--serial",
        action="store_true",
        help="Run delegated subtasks one at a time, for timing comparison.",
    )
    parser.add_argument("--corpus", type=Path, help="Directory the document_analyst may read.")
    parser.add_argument("--timeout", type=float, help="Per-subagent deadline in seconds.")
    parser.add_argument("--max-rounds", type=int, help="Cap on coordinator delegation rounds.")
    parser.add_argument(
        "--simulate-timeout",
        metavar="SUBAGENT_OR_TITLE",
        help="Force this subagent (e.g. document_analyst) to blow its deadline.",
    )
    parser.add_argument(
        "--out", type=Path, help="Override the report path (default: ./output/<topic>-<stamp>.md)."
    )
    parser.add_argument(
        "--trace", type=Path, help="Override the trace path (default: ./output/<topic>-<stamp>.json)."
    )
    parser.add_argument("--print", dest="echo", action="store_true", help="Also print the report.")
    parser.add_argument("--quiet", action="store_true", help="Suppress progress output.")
    return parser


async def run(args: argparse.Namespace) -> int:
    settings = load_settings(requested_mode=args.mode, corpus_dir=args.corpus)
    if args.timeout is not None:
        settings.subagent_timeout_s = args.timeout
    if args.max_rounds is not None:
        settings.max_rounds = args.max_rounds

    if not args.quiet:
        print(f"Mode      : {settings.mode} ({settings.mode_reason})")
        print(f"Models    : coordinator={settings.coordinator_model} subagent={settings.subagent_model}")
        print(f"Corpus    : {settings.corpus_dir}")
        print(f"Execution : {'serial' if args.serial else 'parallel'}")
        print(f"Topic     : {args.topic}")

    orchestrator = Orchestrator(
        settings,
        parallel=not args.serial,
        simulate_timeout=args.simulate_timeout,
        verbose=not args.quiet,
    )
    report = await orchestrator.research(args.topic)
    markdown = render_markdown(report)

    # Every run is persisted, so a report is never lost to a scrolled-away
    # terminal. output/ is git-ignored.
    stamp = time.strftime("%Y%m%d-%H%M%S")
    basename = f"{slugify(args.topic)}-{stamp}"
    report_path = args.out or OUTPUT_DIR / f"{basename}.md"
    trace_path = args.trace or OUTPUT_DIR / f"{basename}.json"

    for path, content in ((report_path, markdown), (trace_path, render_trace(report))):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    if args.echo:
        print("\n" + "=" * 72 + "\n")
        print(markdown)

    if not args.quiet:
        print(f"\nReport : {report_path}")
        print(f"Trace  : {trace_path}")
        print(
            f"\nWall clock {report.total_elapsed_s:.2f}s | summed subagent time "
            f"{report.subagent_cpu_s:.2f}s | overlap {report.parallel_speedup:.2f}x"
        )
        if report.failed_runs:
            print(f"{len(report.failed_runs)} subtask(s) failed; the report says which.")

    # A run that produced a report is a success even if a subtask failed --
    # partial results are the designed outcome, not an error.
    return 0 if report.sources or report.report_markdown else 1


def main() -> int:
    args = build_parser().parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
