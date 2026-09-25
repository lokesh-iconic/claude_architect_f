"""CLI for the capstone support resolution agent.

    uv run python main.py all
    uv run python main.py scenarios --only tool_timeout

Every run writes a Markdown report and a JSON trace per suite into ./output/,
which is git-ignored.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = MODULE_DIR / "output"

sys.path.insert(0, str(MODULE_DIR))

from resolution_agent.reporting import report  # noqa: E402
from resolution_agent.backends.backend import make_backend  # noqa: E402
from resolution_agent.evaluation.config_check import run_workflow  # noqa: E402
from resolution_agent.tools.errors import SupportToolError  # noqa: E402
from resolution_agent.evaluation.results import SuiteResult  # noqa: E402
from resolution_agent.evaluation.scenarios import SCENARIOS, agent_factory, run_scenarios  # noqa: E402
from resolution_agent.config.settings import Settings, load_settings  # noqa: E402
from resolution_agent.evaluation.suites import run_actions, run_context, run_loop, run_tools  # noqa: E402

ORDER = ["scenarios", "loop", "tools", "actions", "context", "workflow"]


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
    if name == "workflow":
        return run_workflow()
    build = agent_factory(settings, lambda: make_backend(settings))
    if name == "scenarios":
        return run_scenarios(build, args.only)
    if name == "loop":
        return run_loop(build, fuzz_sequences=args.fuzz)
    if name == "actions":
        return run_actions(build, fuzz_sequences=args.fuzz)
    return {"tools": run_tools, "context": run_context}[name](build)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="resolution-agent",
        description="Capstone support resolution agent: scenario set and per-domain checks.",
    )
    parser.add_argument("command", nargs="?", default="all", choices=["all", *ORDER],
                        help="Which suite to run. Default: all.")
    parser.add_argument("--only", action="append", metavar="SCENARIO",
                        help=f"With 'scenarios': run just this one (repeatable). "
                             f"Names: {', '.join(s.name for s in SCENARIOS)}.")
    parser.add_argument("--mode", choices=["auto", "live", "mock"], default="auto",
                        help="auto (default) uses the API key if it validates, otherwise mock.")
    parser.add_argument("--keep-recent", type=int, help="Customer turns kept verbatim before summarizing (default 4).")
    parser.add_argument("--tool-timeout", type=float, help="Per-tool-call deadline in seconds (default 3).")
    parser.add_argument("--fuzz", type=int, default=2000, help="Random call sequences in each fuzz.")
    parser.add_argument("--print", dest="echo", action="store_true", help="Also print reports.")
    parser.add_argument("--quiet", action="store_true", help="Suppress progress output.")
    return parser


def run(args: argparse.Namespace, output_dir: Path = OUTPUT_DIR) -> int:
    chosen = ORDER if args.command == "all" else [args.command]
    settings = load_settings(requested_mode=args.mode)
    if args.keep_recent is not None:
        settings.keep_recent_turns = args.keep_recent
    if args.tool_timeout is not None:
        settings.tool_timeout_s = args.tool_timeout
    if not args.quiet:
        print(f"Mode: {settings.mode} ({settings.mode_reason})")

    out = Writer(args.quiet, output_dir)
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
        failed += sum(not c.passed for c in suite.checks)
        if not args.quiet:
            if name == "scenarios":
                for row in suite.tables["scenarios"]:
                    print(f"  {row['result']}  {row['scenario']}  ({row['checks']} checks)"
                          + (f"  -- {row['failing']}" if row["failing"] else ""))
            else:
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
