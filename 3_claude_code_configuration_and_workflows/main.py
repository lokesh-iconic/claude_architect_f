"""CLI for the Claude Code configuration and workflows module.

    uv run python main.py all

Subcommands write their reports into ./output/, which is git-ignored.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = MODULE_DIR / "output"
DEFAULT_STATE_FILE = OUTPUT_DIR / ".review-state.json"

sys.path.insert(0, str(MODULE_DIR))

from review.checks import ci_check, hierarchy_check, rules_check  # noqa: E402
from review.pipeline import dedupe, diffing, state  # noqa: E402
from review.reporting import report  # noqa: E402
from review.clients.claude_cli import LiveRunner, MockRunner, ReviewRunError  # noqa: E402
from review.clients.github_client import GithubReviewClient  # noqa: E402
from review.config.settings import load_settings  # noqa: E402


def write(name: str, content: str, quiet: bool) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / f"{name}-{time.strftime('%Y%m%d-%H%M%S')}.md"
    path.write_text(content, encoding="utf-8")
    if not quiet:
        print(f"  wrote {path.relative_to(MODULE_DIR)}")
    return path


def cmd_hierarchy(args: argparse.Namespace) -> str:
    entries = hierarchy_check.check_hierarchy()
    if not args.quiet:
        for e in entries:
            print(f"  [{e.scope}] {e.path.name}: {'OK' if e.ok else 'FAIL'}")
    return report.render_hierarchy(entries)


def cmd_rules(args: argparse.Namespace) -> str:
    rules = rules_check.load_rules()
    cases = rules_check.check_cases(rules)
    if not args.quiet:
        for c in cases:
            print(f"  {c.path}: {'OK' if c.ok else 'MISMATCH'}")
    return report.render_rules(rules, cases)


def cmd_ci(args: argparse.Namespace) -> str:
    result = ci_check.check_ci()
    if not args.quiet:
        print(f"  overall: {'OK' if result.ok else 'FAIL'}")
    return report.render_ci(result)


def cmd_review(args: argparse.Namespace) -> str:
    settings = load_settings(args.mode)
    if not args.quiet:
        print(f"  mode: {settings.mode} ({settings.mode_reason})")

    diff = diffing.get_diff(args.base, args.head, cwd=MODULE_DIR.parent)

    runner = LiveRunner(settings.claude_binary, settings.model) if settings.is_live else MockRunner()
    try:
        findings = runner.review(diff)
    except ReviewRunError as exc:
        if not args.quiet:
            print(f"  review run failed: {exc}", file=sys.stderr)
        findings = []

    use_github = settings.is_live and args.repo and args.pr
    client = GithubReviewClient(args.repo, args.pr) if use_github else None

    prior_fingerprints = (
        dedupe.extract_fingerprints(client.fetch_existing_comment_bodies())
        if client
        else state.load_state(args.state_file)
    )
    result = dedupe.dedupe(findings, prior_fingerprints)

    if not args.dry_run:
        if client:
            client.post_review(args.head, result.to_post)
        else:
            posted = prior_fingerprints | {f.fingerprint() for f in result.to_post}
            state.save_state(args.state_file, posted)

    if not args.quiet:
        print(f"  {len(findings)} finding(s): {len(result.to_post)} new, {len(result.skipped)} already flagged")
    return report.render_review(settings, diff, findings, result)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="claude-code-config",
        description="Checks and CI review logic for Claude Code team configuration.",
    )
    parser.add_argument(
        "--version", action="version", version="claude-code-config 0.1.0"
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="all",
        choices=["all", "hierarchy-check", "rules-check", "ci-check", "review"],
        help="Which check to run. Default: all.",
    )
    parser.add_argument("--mode", choices=["auto", "live", "mock"], default="auto", help="review: backend mode.")
    parser.add_argument("--base", default="master", help="review: base ref to diff against.")
    parser.add_argument("--head", default="HEAD", help="review: head ref/sha to diff.")
    parser.add_argument("--repo", default=None, help="review: 'owner/name', enables posting via gh api.")
    parser.add_argument("--pr", type=int, default=None, help="review: PR number, enables posting via gh api.")
    parser.add_argument(
        "--state-file",
        type=Path,
        default=DEFAULT_STATE_FILE,
        help="review: local file standing in for 'prior run's posted findings' outside GitHub.",
    )
    parser.add_argument("--dry-run", action="store_true", help="review: compute but don't post/persist.")
    parser.add_argument("--print", dest="echo", action="store_true", help="Also print reports.")
    parser.add_argument("--quiet", action="store_true", help="Suppress progress output.")
    return parser


def run(args: argparse.Namespace) -> int:
    jobs = {
        "hierarchy-check": ("hierarchy", cmd_hierarchy),
        "rules-check": ("rules", cmd_rules),
        "ci-check": ("ci", cmd_ci),
        "review": ("review", cmd_review),
    }
    if args.command == "all":
        chosen = ["hierarchy-check", "rules-check", "ci-check", "review"]
        args.mode = "mock"  # `all` must run offline end to end, like modules 1/2.
    else:
        chosen = [args.command]

    written: list[Path] = []
    for key in chosen:
        name, handler = jobs[key]
        if not args.quiet:
            print(f"\n== {key}")
        content = handler(args)
        written.append(write(name, content, args.quiet))
        if args.echo:
            print("\n" + content)

    if not args.quiet:
        print(f"\n{len(written)} file(s) in {OUTPUT_DIR.relative_to(MODULE_DIR)}/")
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
