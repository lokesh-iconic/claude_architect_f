"""Self-check #1: root and directory-scope CLAUDE.md both exist. `tracked`
is asserted loosely here since it depends on git add/commit state at test
time -- see review/checks/hierarchy_check.py's docstring."""

from review.checks.hierarchy_check import check_hierarchy


def test_expected_claude_md_files_exist():
    entries = check_hierarchy()
    assert {e.scope for e in entries} == {"project (root)", "directory (module 3)"}
    for e in entries:
        assert e.exists, f"missing {e.path}"
        assert not e.ignored, f"{e.path} is gitignored"
