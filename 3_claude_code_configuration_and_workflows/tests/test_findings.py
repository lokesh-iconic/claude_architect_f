import pytest

from review.findings import Finding, FindingsParseError, parse_findings


def test_fingerprint_stable_across_line_shift():
    a = Finding(path="x.py", line=10, category="bug", message="off by one")
    b = Finding(path="x.py", line=42, category="bug", message="off by one")
    assert a.fingerprint() == b.fingerprint()


def test_fingerprint_differs_by_message():
    a = Finding(path="x.py", line=10, category="bug", message="off by one")
    b = Finding(path="x.py", line=10, category="bug", message="null pointer")
    assert a.fingerprint() != b.fingerprint()


def test_fingerprint_differs_by_category():
    a = Finding(path="x.py", line=10, category="bug", message="same text")
    b = Finding(path="x.py", line=10, category="security", message="same text")
    assert a.fingerprint() != b.fingerprint()


def test_marker_round_trips_into_comment_body():
    f = Finding(path="x.py", line=1, category="bug", message="m")
    assert f.fingerprint() in f.to_comment_body()


def test_parse_findings_object_form():
    raw = '{"findings": [{"path": "a.py", "line": 3, "category": "bug", "message": "m"}]}'
    findings = parse_findings(raw)
    assert findings == [Finding(path="a.py", line=3, category="bug", message="m")]


def test_parse_findings_bare_array_form():
    raw = '[{"path": "a.py", "line": 3, "message": "m"}]'
    findings = parse_findings(raw)
    assert findings[0].category == "bug"  # default
    assert findings[0].severity == "medium"  # default


def test_parse_findings_empty():
    assert parse_findings('{"findings": []}') == []


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        '{"findings": "not a list"}',
        '{"findings": [{"path": "a.py"}]}',  # missing line/message
        '{"findings": [{"path": "a.py", "line": "not-an-int", "message": "m"}]}',
    ],
)
def test_parse_findings_rejects_malformed(raw):
    with pytest.raises(FindingsParseError):
        parse_findings(raw)
