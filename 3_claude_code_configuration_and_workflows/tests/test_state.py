from pathlib import Path

from review.state import load_state, save_state


def test_missing_state_file_is_empty(tmp_path: Path):
    assert load_state(tmp_path / "nope.json") == set()


def test_save_then_load_round_trips(tmp_path: Path):
    path = tmp_path / "nested" / "state.json"
    save_state(path, {"abc123", "def456"})
    assert load_state(path) == {"abc123", "def456"}
