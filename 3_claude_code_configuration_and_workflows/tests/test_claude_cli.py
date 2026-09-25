import subprocess

from review.clients.claude_cli import (
    SYNTHETIC_TAG,
    LiveRunner,
    MockRunner,
    ReviewRunError,
    _unwrap_cli_envelope,
)


def test_mock_runner_flags_added_todo():
    diff = (
        "diff --git a/x.py b/x.py\n"
        "--- a/x.py\n"
        "+++ b/x.py\n"
        "@@ -1,2 +1,3 @@\n"
        " def f():\n"
        "+    # TODO: handle this\n"
        "     return 1\n"
    )
    findings = MockRunner().review(diff)
    assert len(findings) == 1
    assert findings[0].path == "x.py"
    assert SYNTHETIC_TAG in findings[0].message


def test_mock_runner_ignores_removed_todo():
    diff = (
        "diff --git a/x.py b/x.py\n"
        "--- a/x.py\n"
        "+++ b/x.py\n"
        "@@ -1,2 +1,1 @@\n"
        "-# TODO: old\n"
        " def f(): pass\n"
    )
    assert MockRunner().review(diff) == []


def test_mock_runner_empty_diff_has_no_findings():
    assert MockRunner().review("") == []


def test_unwrap_cli_envelope_extracts_result_field():
    envelope = '{"result": "{\\"findings\\": []}", "cost_usd": 0.01}'
    assert _unwrap_cli_envelope(envelope) == '{"findings": []}'


def test_unwrap_cli_envelope_falls_back_when_not_an_envelope():
    raw = '{"findings": []}'
    assert _unwrap_cli_envelope(raw) == raw


def test_live_runner_invokes_claude_with_dash_p_and_json_output(monkeypatch):
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout='{"findings": []}', stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    runner = LiveRunner(binary="claude", model="claude-opus-5")
    findings = runner.review("some diff")

    assert findings == []
    argv = captured["argv"]
    assert argv[0] == "claude"
    assert "-p" in argv
    assert "--output-format" in argv
    assert argv[argv.index("--output-format") + 1] == "json"


def test_live_runner_raises_on_timeout(monkeypatch):
    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=1)

    monkeypatch.setattr(subprocess, "run", fake_run)
    runner = LiveRunner(binary="claude", model="claude-opus-5", timeout_seconds=1)
    try:
        runner.review("diff")
        assert False, "expected ReviewRunError"
    except ReviewRunError:
        pass
