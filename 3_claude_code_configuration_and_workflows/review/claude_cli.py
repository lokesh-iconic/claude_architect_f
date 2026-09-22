"""Two ways to turn a diff into findings: the real `claude -p` invocation,
and a deterministic offline stand-in exercising the same plumbing.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass

from .findings import Finding, FindingsParseError, parse_findings

REVIEW_PROMPT_TEMPLATE = """You are reviewing a pull request diff for correctness bugs, \
security issues, and missing test coverage. Respond with ONLY a JSON object of the \
form {{"findings": [{{"path": str, "line": int, \
"category": "bug"|"security"|"test-coverage"|"style"|"simplification", \
"message": str, "severity": "low"|"medium"|"high"}}]}}. \
An empty diff, or a diff with no issues, returns {{"findings": []}}. \
Do not include any text outside the JSON object.

Diff:
{diff}
"""


class ReviewRunError(RuntimeError):
    pass


@dataclass(frozen=True)
class LiveRunner:
    """Shells out to the real Claude Code CLI: `claude -p <prompt>
    --output-format json`. This is the literal mechanism the brief asks
    the CI step to use.
    """

    binary: str
    model: str
    timeout_seconds: int = 300

    def review(self, diff: str) -> list[Finding]:
        prompt = REVIEW_PROMPT_TEMPLATE.format(diff=diff or "(no changes)")
        argv = [
            self.binary,
            "-p",
            prompt,
            "--output-format",
            "json",
            "--model",
            self.model,
        ]
        try:
            result = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=True,
            )
        except subprocess.TimeoutExpired as exc:
            raise ReviewRunError(
                f"claude -p timed out after {self.timeout_seconds}s"
            ) from exc
        except subprocess.CalledProcessError as exc:
            raise ReviewRunError(f"claude -p exited {exc.returncode}: {exc.stderr}") from exc

        return parse_findings(_unwrap_cli_envelope(result.stdout))


def _unwrap_cli_envelope(stdout: str) -> str:
    """`claude --output-format json` wraps the reply in a CLI envelope
    (session id, cost, etc.) whose `result` field carries the model's own
    text -- which is itself the findings JSON we asked for.
    """
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError:
        return stdout
    if isinstance(envelope, dict) and "result" in envelope:
        return str(envelope["result"])
    return stdout


SYNTHETIC_TAG = "[SYNTHETIC]"
_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


@dataclass(frozen=True)
class MockRunner:
    """No `claude` binary or API key required. Scans added lines for an
    unresolved TODO/FIXME -- a real, deterministic signal -- so the parse /
    fingerprint / dedupe pipeline downstream is exercised end to end without
    a live call. Findings are tagged SYNTHETIC and must never be treated as
    a real review, same convention module 1 uses for its mock sources.
    """

    def review(self, diff: str) -> list[Finding]:
        findings: list[Finding] = []
        current_path: str | None = None
        current_line = 0
        for line in diff.splitlines():
            if line.startswith("+++ "):
                target = line[4:]
                current_path = target[2:] if target.startswith("b/") else target
                continue
            if line.startswith("--- "):
                continue
            hunk = _HUNK_HEADER.match(line)
            if hunk:
                current_line = int(hunk.group(1))
                continue
            if line.startswith("+"):
                text = line[1:]
                if "TODO" in text or "FIXME" in text:
                    findings.append(
                        Finding(
                            path=current_path or "unknown",
                            line=current_line,
                            category="test-coverage",
                            message=(
                                f"{SYNTHETIC_TAG} unresolved TODO/FIXME added: "
                                f"{text.strip()[:120]}"
                            ),
                            severity="low",
                        )
                    )
                current_line += 1
            elif line.startswith("-"):
                continue
            else:
                current_line += 1
        return findings
