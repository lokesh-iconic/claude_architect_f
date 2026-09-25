"""Self-check #4: a second run must not re-post what a prior run already
flagged, even when a finding's line number has since shifted."""

from review.clients.claude_cli import MockRunner
from review.pipeline.dedupe import dedupe, extract_fingerprints
from review.pipeline.findings import Finding


def test_dedupe_skips_known_fingerprint():
    prior = {Finding(path="a.py", line=1, category="bug", message="m").fingerprint()}
    findings = [
        Finding(path="a.py", line=99, category="bug", message="m"),  # same issue, moved
        Finding(path="a.py", line=2, category="bug", message="new one"),
    ]
    result = dedupe(findings, prior)
    assert [f.message for f in result.to_post] == ["new one"]
    assert [f.message for f in result.skipped] == ["m"]


def test_dedupe_deduplicates_within_the_same_run_too():
    findings = [
        Finding(path="a.py", line=1, category="bug", message="dup"),
        Finding(path="a.py", line=2, category="bug", message="dup"),
    ]
    result = dedupe(findings, prior_fingerprints=set())
    assert len(result.to_post) == 1
    assert len(result.skipped) == 1


def test_extract_fingerprints_reads_hidden_markers():
    f = Finding(path="a.py", line=1, category="bug", message="m")
    bodies = [f.to_comment_body(), "an unrelated human comment", None]
    assert extract_fingerprints(bodies) == {f.fingerprint()}


def test_two_runs_against_a_growing_diff_only_posts_the_new_issue():
    """End-to-end through the mock runner: same TODO survives a second run
    unflagged, only a genuinely new TODO is new."""
    run_1_diff = (
        "diff --git a/x.py b/x.py\n"
        "--- a/x.py\n"
        "+++ b/x.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+# TODO: handle retries\n"
        "+def f(): pass\n"
    )
    run_2_diff = (
        "diff --git a/x.py b/x.py\n"
        "--- a/x.py\n"
        "+++ b/x.py\n"
        "@@ -0,0 +1,4 @@\n"
        "+# a leading comment shifts everything down\n"
        "+# TODO: handle retries\n"
        "+def f(): pass\n"
        "+# TODO: add validation\n"
    )
    runner = MockRunner()

    run_1_findings = runner.review(run_1_diff)
    run_1_result = dedupe(run_1_findings, prior_fingerprints=set())
    posted_fingerprints = {f.fingerprint() for f in run_1_result.to_post}

    run_2_findings = runner.review(run_2_diff)
    run_2_result = dedupe(run_2_findings, prior_fingerprints=posted_fingerprints)

    assert len(run_1_result.to_post) == 1
    assert len(run_2_result.to_post) == 1
    assert "add validation" in run_2_result.to_post[0].message
    assert len(run_2_result.skipped) == 1
