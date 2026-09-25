import pytest

from review.config import settings as settings_mod
from review.config.settings import load_settings


def test_mode_mock_is_always_honoured(monkeypatch):
    monkeypatch.setattr(settings_mod, "_claude_availability", lambda: (True, "available"))
    s = load_settings("mock")
    assert s.mode == "mock"
    assert not s.is_live


def test_auto_falls_back_to_mock_when_unavailable(monkeypatch):
    monkeypatch.setattr(settings_mod, "_claude_availability", lambda: (False, "no binary"))
    s = load_settings("auto")
    assert s.mode == "mock"
    assert "no binary" in s.mode_reason


def test_auto_goes_live_when_available(monkeypatch):
    monkeypatch.setattr(settings_mod, "_claude_availability", lambda: (True, "ok"))
    s = load_settings("auto")
    assert s.is_live


def test_mode_live_raises_when_unavailable(monkeypatch):
    monkeypatch.setattr(settings_mod, "_claude_availability", lambda: (False, "no key"))
    with pytest.raises(RuntimeError):
        load_settings("live")


def test_unknown_mode_rejected(monkeypatch):
    monkeypatch.setattr(settings_mod, "_claude_availability", lambda: (True, "ok"))
    with pytest.raises(ValueError):
        load_settings("bogus")
