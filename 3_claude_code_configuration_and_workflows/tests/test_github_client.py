import json

from review.findings import Finding
from review.github_client import GithubReviewClient


class FakeTransport:
    def __init__(self, comments_response="[]"):
        self.calls: list[tuple[list[str], str | None]] = []
        self.comments_response = comments_response

    def __call__(self, args: list[str], input_data: str | None = None) -> str:
        self.calls.append((args, input_data))
        if args[0] == "api" and args[1].endswith("/comments") and "--input" not in args:
            return self.comments_response
        return ""


def test_fetch_existing_comment_bodies_parses_gh_api_output():
    transport = FakeTransport(comments_response=json.dumps([{"body": "hello"}, {"body": "world"}]))
    client = GithubReviewClient(repo="o/r", pr_number=7, transport=transport)
    assert client.fetch_existing_comment_bodies() == ["hello", "world"]
    assert transport.calls[0][0] == ["api", "repos/o/r/pulls/7/comments", "--paginate"]


def test_post_review_sends_one_review_with_all_findings():
    transport = FakeTransport()
    client = GithubReviewClient(repo="o/r", pr_number=7, transport=transport)
    findings = [
        Finding(path="a.py", line=1, category="bug", message="m1"),
        Finding(path="b.py", line=2, category="style", message="m2"),
    ]

    client.post_review(commit_id="abc123", findings=findings)

    assert len(transport.calls) == 1
    args, payload = transport.calls[0]
    assert args == ["api", "repos/o/r/pulls/7/reviews", "--input", "-"]
    body = json.loads(payload)
    assert body["commit_id"] == "abc123"
    assert body["event"] == "COMMENT"
    assert len(body["comments"]) == 2
    assert body["comments"][0]["path"] == "a.py"
    assert findings[0].fingerprint() in body["comments"][0]["body"]


def test_post_review_with_no_findings_makes_no_call():
    transport = FakeTransport()
    client = GithubReviewClient(repo="o/r", pr_number=7, transport=transport)
    client.post_review(commit_id="abc123", findings=[])
    assert transport.calls == []
