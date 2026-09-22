import subprocess
from pathlib import Path

import pytest

from review.diffing import DiffError, changed_files, get_diff


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(["init", "-q", "-b", "main"], tmp_path)
    _git(["config", "user.email", "test@example.com"], tmp_path)
    _git(["config", "user.name", "Test"], tmp_path)
    (tmp_path / "a.py").write_text("print('hi')\n", encoding="utf-8")
    _git(["add", "a.py"], tmp_path)
    _git(["commit", "-q", "-m", "base"], tmp_path)
    _git(["checkout", "-q", "-b", "feature"], tmp_path)
    (tmp_path / "a.py").write_text("print('hi')\nprint('bye')\n", encoding="utf-8")
    _git(["add", "a.py"], tmp_path)
    _git(["commit", "-q", "-m", "feature change"], tmp_path)
    return tmp_path


def test_changed_files_lists_the_modified_file(repo: Path):
    assert changed_files("main", "feature", cwd=repo) == ["a.py"]


def test_get_diff_contains_the_added_line(repo: Path):
    diff = get_diff("main", "feature", cwd=repo)
    assert "+print('bye')" in diff


def test_get_diff_on_unknown_ref_raises_diff_error(repo: Path):
    with pytest.raises(DiffError):
        get_diff("does-not-exist", "feature", cwd=repo)
