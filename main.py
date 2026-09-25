"""Run every module's test suite from the repo root and print one summary.

    uv run python main.py                 # the five domains, then the capstone
    uv run python main.py 2 5             # just those modules
    uv run python main.py 1 2 3 4 5       # the five domains only
    uv run python main.py capstone -v     # stream pytest's own output
    uv run python main.py -k escalat      # pass a -k expression through to pytest

Each module runs in its own pytest process, so one module's imports and
fixtures can't leak into another's. The tests never need an API key or the
network. Exits non-zero if any module fails, errors, times out, or collects
no tests.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent

MODULES: dict[str, tuple[str, str]] = {
    "1": ("1_agent_architecture_and_orchestration", "Agent architecture & orchestration"),
    "2": ("2_tool_design_and_MCP_integration", "Tool design & MCP integration"),
    "3": ("3_claude_code_configuration_and_workflows", "Claude Code configuration & workflows"),
    "4": ("4_prompt_engineering_and_structured_output", "Prompt engineering & structured output"),
    "5": ("5_context_management_and_reliability", "Context management & reliability"),
    "capstone": ("capstone", "Capstone: support resolution agent"),
}

_COUNT = re.compile(r"(\d+) (passed|failed|errors?|skipped|xfailed|xpassed|deselected)")


@dataclass
class ModuleResult:
    key: str
    title: str
    status: str
    counts: dict[str, int]
    seconds: float
    output: str = ""


def _counts(output: str) -> dict[str, int]:
    summary = next((line for line in reversed(output.splitlines()) if _COUNT.search(line)), "")
    counts: dict[str, int] = {}
    for n, kind in _COUNT.findall(summary):
        counts["error" if kind.startswith("error") else kind] = int(n)
    return counts


def run_module(key: str, args: argparse.Namespace) -> ModuleResult:
    folder, title = MODULES[key]
    cmd = [sys.executable, "-m", "pytest", f"{folder}/tests", "-q" if not args.verbose else "-v"]
    if args.k:
        cmd += ["-k", args.k]
    started = time.perf_counter()
    try:
        proc = subprocess.run(cmd, cwd=ROOT, capture_output=not args.verbose, text=True,
                              encoding="utf-8", errors="replace", timeout=args.timeout)
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout if isinstance(exc.stdout, str) else ""
        return ModuleResult(key, title, "TIMEOUT", {}, time.perf_counter() - started, output)
    elapsed = time.perf_counter() - started
    output = proc.stdout or ""
    # pytest exit codes: 0 ok, 1 failures, 5 nothing collected (e.g. -k matched nothing)
    status = {0: "PASS", 1: "FAIL", 5: "NO TESTS"}.get(proc.returncode, f"ERROR ({proc.returncode})")
    return ModuleResult(key, title, status, _counts(output), elapsed, output + (proc.stderr or ""))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run-all-tests",
        description="Run each module's pytest suite in turn and summarize. Always offline.",
    )
    parser.add_argument("modules", nargs="*", metavar="MODULE",
                        help=f"Which modules to run: {', '.join(MODULES)}. Default: all.")
    parser.add_argument("-k", metavar="EXPR", help="Only run tests matching this pytest -k expression.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Stream pytest's verbose output live.")
    parser.add_argument("--fail-fast", action="store_true", help="Stop after the first module that doesn't pass.")
    parser.add_argument("--timeout", type=float, default=900, help="Per-module time limit in seconds (default 900).")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    unknown = [m for m in args.modules if m not in MODULES]
    if unknown:
        parser.error(f"unknown module(s) {', '.join(unknown)}; choose from {', '.join(MODULES)}")
    chosen = args.modules or list(MODULES)
    results: list[ModuleResult] = []
    for key in chosen:
        folder, title = MODULES[key]
        print(f"== {folder}", flush=True)
        result = run_module(key, args)
        results.append(result)
        passed = result.counts.get("passed", 0)
        print(f"   {result.status}  {passed} passed, {result.counts.get('failed', 0)} failed "
              f"in {result.seconds:.1f}s", flush=True)
        if result.status != "PASS" and not args.verbose:
            tail = "\n".join(result.output.rstrip().splitlines()[-40:])
            print("\n".join(f"   | {line}" for line in tail.splitlines()))
        if args.fail_fast and result.status != "PASS":
            break

    width = max(len(r.title) for r in results) + 2
    print(f"\n{'':<9}{'Module':<{width}}{'Result':<10}{'Passed':>7}{'Failed':>7}{'Errors':>7}{'Time':>9}")
    for r in results:
        print(f"{r.key:<9}{r.title:<{width}}{r.status:<10}{r.counts.get('passed', 0):>7}"
              f"{r.counts.get('failed', 0):>7}{r.counts.get('error', 0):>7}{r.seconds:>8.1f}s")
    total = {k: sum(r.counts.get(k, 0) for r in results) for k in ("passed", "failed", "error")}
    ok = all(r.status == "PASS" for r in results) and len(results) == len(chosen)
    print(f"\n{'ALL PASSED' if ok else 'FAILURES'}: {total['passed']} passed, {total['failed']} failed, "
          f"{total['error']} errors across {len(results)} module(s) in "
          f"{sum(r.seconds for r in results):.1f}s")
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        raise SystemExit(130)
