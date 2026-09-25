"""CLI for the long-conversation support agent.

    uv run python main.py all

Every run writes a Markdown report and a JSON trace per suite into ./output/,
which is git-ignored.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = MODULE_DIR / "output"

sys.path.insert(0, str(MODULE_DIR))

from support_agent.reporting import report  # noqa: E402
from support_agent.backends.backend import make_backend  # noqa: E402
from support_agent.tools.errors import SupportToolError  # noqa: E402
from support_agent.evaluation.scenarios import SUITES, SuiteResult, agent_factory, run_enforcement  # noqa: E402
from support_agent.config.settings import Settings, load_settings  # noqa: E402


class Writer:
    def __init__(self, quiet: bool, output_dir: Path = OUTPUT_DIR) -> None:
        self.quiet = quiet
        self.output_dir = output_dir
        self.stamp = time.strftime("%Y%m%d-%H%M%S")
        self.written: list[Path] = []

    def write(self, name: str, suffix: str, content: str) -> Path:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        path = self.output_dir / f"{name}-{self.stamp}{suffix}"
        path.write_text(content, encoding="utf-8")
        self.written.append(path)
        if not self.quiet:
            print(f"  wrote {path.relative_to(self.output_dir.parent)}")
        return path


def run_suite(name: str, args: argparse.Namespace, settings: Settings) -> SuiteResult:
    if name == "mcp":
        from support_agent.evaluation.probe_client import run_mcp

        return asyncio.run(run_mcp())
    build = agent_factory(settings, lambda: make_backend(settings))
    if name == "enforcement":
        return run_enforcement(build, fuzz_sequences=args.fuzz)
    return SUITES[name](build)


ORDER = ["context", "escalation", "decompose", "enforcement", "errors", "mcp"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="support-agent",
        description="Long-conversation support agent: case facts, trimming, refund gate, escalation, structured errors.",
    )
    parser.add_argument("command", nargs="?", default="all", choices=["all", *ORDER],
                        help="Which suite to run. Default: all.")
    parser.add_argument("--mode", choices=["auto", "live", "mock"], default="auto",
                        help="auto (default) uses the API key if it validates, otherwise mock.")
    parser.add_argument("--keep-recent", type=int, help="Customer turns kept verbatim before summarizing (default 4).")
    parser.add_argument("--tool-timeout", type=float, help="Per-tool-call deadline in seconds (default 3).")
    parser.add_argument("--fuzz", type=int, default=2000, help="Random call sequences in the enforcement fuzz.")
    parser.add_argument("--print", dest="echo", action="store_true", help="Also print reports.")
    parser.add_argument("--quiet", action="store_true", help="Suppress progress output.")
    return parser


def run(args: argparse.Namespace, output_dir: Path = OUTPUT_DIR) -> int:
    settings = load_settings(requested_mode=args.mode)
    if args.keep_recent is not None:
        settings.keep_recent_turns = args.keep_recent
    if args.tool_timeout is not None:
        settings.tool_timeout_s = args.tool_timeout
    if not args.quiet:
        print(f"Mode: {settings.mode} ({settings.mode_reason})")

    out = Writer(args.quiet, output_dir)
    chosen = ORDER if args.command == "all" else [args.command]
    failed = 0
    for name in chosen:
        if not args.quiet:
            print(f"\n== {name}")
        try:
            suite = run_suite(name, args, settings)
        except SupportToolError as exc:
            print(json.dumps(exc.to_payload(), indent=2), file=sys.stderr)
            return 1
        markdown = report.render(suite, settings)
        out.write(name, ".md", markdown)
        out.write(name, ".json", report.trace_json(suite, settings))
        passed = sum(c.passed for c in suite.checks)
        failed += len(suite.checks) - passed
        if not args.quiet:
            for c in suite.checks:
                print(f"  {'PASS' if c.passed else 'FAIL'}  {c.claim}")
        if args.echo:
            print("\n" + markdown)

    if not args.quiet:
        print(f"\n{len(out.written)} file(s) in {output_dir.relative_to(output_dir.parent)}/; "
              f"{failed} check(s) failed")
    return 0 if failed == 0 else 2


def main() -> int:
    args = build_parser().parse_args()
    try:
        return run(args)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
