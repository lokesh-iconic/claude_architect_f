"""Mode resolution and the CLI's output location."""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path

import pytest

from invoice_extractor.config import settings as settings_mod
from invoice_extractor.config.settings import load_settings

# Every module has a main.py; load this one by path so sys.modules["main"] can't collide.
_spec = importlib.util.spec_from_file_location(
    "invoice_extractor_main", Path(__file__).resolve().parents[1] / "main.py"
)
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)


@pytest.fixture
def no_key(monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return tmp_path / "missing.env"


def test_mode_mock_is_always_honoured(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-real-key")
    s = load_settings("mock", env_file=tmp_path / "none.env")
    assert s.mode == "mock" and "forced" in s.mode_reason


def test_auto_falls_back_to_mock_without_a_key_and_says_why(no_key):
    s = load_settings("auto", env_file=no_key)
    assert s.mode == "mock"
    assert "ANTHROPIC_API_KEY" in s.mode_reason


def test_live_without_a_key_is_a_hard_error(no_key):
    with pytest.raises(SystemExit):
        load_settings("live", env_file=no_key)


def test_auto_falls_back_when_the_key_check_fails(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-real-key")
    monkeypatch.setattr(settings_mod, "_validate_key", lambda k, m: (False, "401"))
    s = load_settings("auto", env_file=tmp_path / "none.env")
    assert s.mode == "mock" and "401" in s.mode_reason
    with pytest.raises(SystemExit):
        load_settings("live", env_file=tmp_path / "none.env")


def test_unknown_mode_rejected(no_key):
    with pytest.raises(ValueError):
        load_settings("bogus", env_file=no_key)


def test_cli_all_writes_every_artifact_into_output(no_key, tmp_path, monkeypatch):
    out = tmp_path / "output"
    args = argparse.Namespace(
        command="all", mode="mock", corpus=None, threshold=None, max_attempts=None,
        batch_size=100, echo=False, quiet=True,
    )
    assert cli.run(args, output_dir=out) == 0
    names = sorted(p.stem for p in out.iterdir())
    assert names == ["batch", "batch", "extract", "extract", "review-queue-batch", "review-queue-extract"]
