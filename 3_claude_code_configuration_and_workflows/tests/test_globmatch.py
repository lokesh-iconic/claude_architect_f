import pytest

from review.globmatch import glob_match


@pytest.mark.parametrize(
    "pattern,path,expected",
    [
        ("**/tests/**/*.py", "tests/foo.py", True),  # ** matches zero dirs
        ("**/tests/**/*.py", "a/b/tests/c/foo.py", True),
        ("**/tests/**/*.py", "a/b/tests/foo.py", True),
        ("**/tests/**/*.py", "a/nottests/foo.py", False),
        ("**/tests/**/*.py", "tests/foo.txt", False),
        ("**/issue_tracker/**/*.py", "2_x/issue_tracker/mcp_server.py", True),
        ("**/issue_tracker/**/*.py", "2_x/issue_tracker/sub/handlers.py", True),
        ("**/issue_tracker/**/*.py", "2_x/other/mcp_server.py", False),
        ("**/*.md", "README.md", True),
        ("**/*.md", "a/b/README.md", True),
        ("**/*.md", "README.txt", False),
    ],
)
def test_glob_match(pattern, path, expected):
    assert glob_match(pattern, path) is expected
