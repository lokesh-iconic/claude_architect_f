"""Self-check #3: the CI step is non-interactive and bounded, and really
does invoke `claude -p ... --output-format json` and post to the PR reviews
API."""

from review.ci_check import check_ci


def test_ci_pipeline_is_non_interactive_and_bounded():
    result = check_ci()
    assert result.workflow_exists
    assert result.has_timeout
    assert result.no_interactive_markers


def test_ci_pipeline_invokes_claude_print_mode_with_json_output():
    result = check_ci()
    assert result.invokes_print_mode
    assert result.invokes_json_output


def test_ci_pipeline_posts_inline_review_comments():
    result = check_ci()
    assert result.posts_inline_review_comments
    assert result.ok
