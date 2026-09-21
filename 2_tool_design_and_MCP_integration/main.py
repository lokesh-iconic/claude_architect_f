"""CLI for the issue-tracker MCP module.

    uv run python main.py all

Subcommands write their results into ./output/, which is git-ignored.
The MCP server itself is started by Claude Code via .mcp.json, not from here.
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

from issue_tracker import config_check, probe_client, report, resource_eval  # noqa: E402
from issue_tracker import selection as sel  # noqa: E402
from issue_tracker.settings import load_settings  # noqa: E402

ERROR_PROBES: list[tuple[str, str, dict, dict[str, str]]] = [
    (
        "Bare filename passed to the path tool",
        "find_issues_for_path",
        {"path": "session.py"},
        {},
    ),
    (
        "A path handed to the text-search tool",
        "search_issues",
        {"query": "sample_service/auth/session.py"},
        {},
    ),
    ("Unknown issue key", "get_issue", {"key": "CHK-999"}, {}),
    (
        "Restricted project",
        "get_issue",
        {"key": "AUTH-99"},
        {},
    ),
    (
        "Upstream timeout (simulated)",
        "search_issues",
        {"query": "security"},
        {"TRACKER_SIMULATE": "transient", "TRACKER_SIMULATE_TOOL": "search_issues"},
    ),
    (
        "Token missing a scope (simulated)",
        "list_sprint_board",
        {},
        {"TRACKER_SIMULATE": "permission", "TRACKER_SIMULATE_TOOL": "list_sprint_board"},
    ),
]


def write(name: str, content: str, quiet: bool) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / f"{name}-{time.strftime('%Y%m%d-%H%M%S')}.md"
    path.write_text(content, encoding="utf-8")
    if not quiet:
        print(f"  wrote {path.relative_to(MODULE_DIR)}")
    return path


async def cmd_selection(args: argparse.Namespace) -> str:
    settings = load_settings(requested_mode=args.mode)
    if not args.quiet:
        print(f"Mode: {settings.mode} ({settings.mode_reason})")
        if settings.is_live:
            print(f"Model: {settings.model} | trials per case: {args.trials}")
    results = await sel.run_all(settings, trials=args.trials)
    if not args.quiet:
        for version, result in results.items():
            print(
                f"  {version}: overall {result.accuracy:.0%}, "
                f"overlapping pair {result.ambiguous_accuracy:.0%}"
            )
    return report.render_selection(results, settings.mode)


async def cmd_resources(args: argparse.Namespace) -> str:
    rows = resource_eval.run()
    summary = resource_eval.summarize(rows)
    if not args.quiet:
        print(
            f"  {summary['toolCallsWithoutResources']} calls without resources -> "
            f"{summary['toolCallsWithResources']} with ({summary['callsAvoided']} avoided)"
        )
    return report.render_resources(rows, summary)


async def cmd_errors(args: argparse.Namespace) -> str:
    probes = await probe_client.probe_errors(ERROR_PROBES)
    if not args.quiet:
        for probe in probes:
            category = probe.payload["errorCategory"] if probe.payload else "UNSTRUCTURED"
            print(f"  {probe.label}: isError={probe.is_error} category={category}")
    return report.render_errors(probes)


async def cmd_doctor(args: argparse.Namespace) -> str:
    summary = config_check.summarize()
    capabilities = None
    if not args.no_connect:
        capabilities = await probe_client.describe()
    if not args.quiet:
        for server in summary["servers"]:
            mark = "" if server["spawnable"] else "  <-- CANNOT SPAWN"
            print(f"  [{server['scope']}] {server['name']}{mark}")
        for bad in summary["unspawnableServers"]:
            print(f"  ! {bad['name']}: {bad['hint']}")
        if summary["blockingVars"]:
            print(f"  unset without default: {', '.join(summary['blockingVars'])}")
        if capabilities:
            print(
                f"  handshake ok: {len(capabilities.tools)} tools, "
                f"{len(capabilities.resources)} resources"
            )
    return report.render_config(summary, capabilities)


async def run(args: argparse.Namespace) -> int:
    jobs = {
        "selection": ("selection", cmd_selection),
        "resources": ("resources", cmd_resources),
        "errors": ("errors", cmd_errors),
        "doctor": ("config", cmd_doctor),
    }
    chosen = list(jobs) if args.command == "all" else [args.command]

    written: list[Path] = []
    for key in chosen:
        name, handler = jobs[key]
        if not args.quiet:
            print(f"\n== {key}")
        content = await handler(args)
        written.append(write(name, content, args.quiet))
        if args.echo:
            print("\n" + content)

    if not args.quiet:
        print(f"\n{len(written)} file(s) in {OUTPUT_DIR.relative_to(MODULE_DIR)}/")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="issue-tracker-mcp",
        description="Harnesses that measure the issue-tracker MCP server's tool design.",
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="all",
        choices=["all", "selection", "resources", "errors", "doctor"],
        help="Which harness to run. Default: all.",
    )
    parser.add_argument(
        "--mode", choices=["auto", "live", "mock"], default="auto", help="Selection eval backend."
    )
    parser.add_argument("--trials", type=int, default=5, help="Trials per case in live mode.")
    parser.add_argument(
        "--no-connect", action="store_true", help="doctor: skip the live stdio handshake."
    )
    parser.add_argument("--print", dest="echo", action="store_true", help="Also print reports.")
    parser.add_argument("--quiet", action="store_true", help="Suppress progress output.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
