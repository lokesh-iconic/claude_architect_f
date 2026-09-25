"""The test conversation set (what /run-scenarios runs), the workflow checks, and the CLI."""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path

import pytest

from resolution_agent.config import settings as settings_mod
from resolution_agent.evaluation.config_check import run_workflow
from resolution_agent.evaluation.scenarios import SCENARIOS, run_scenarios
from resolution_agent.config.settings import load_settings

# Every module has a main.py; load this one by path so sys.modules["main"] can't collide.
_spec = importlib.util.spec_from_file_location(
    "resolution_agent_main", Path(__file__).resolve().parents[1] / "main.py"
)
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
def test_scenario_passes(build, scenario):
    checks, _ = scenario.run(build)
    failed = [(c.claim, c.detail) for c in checks if not c.passed]
    assert checks and not failed


def test_scenario_runner_reports_one_row_per_scenario_and_filters(build):
    suite = run_scenarios(build, ["tool_timeout", "account_change_to_human"])
    assert [r["scenario"] for r in suite.tables["scenarios"]] == ["account_change_to_human", "tool_timeout"]
    with pytest.raises(SystemExit):
        run_scenarios(build, ["no_such_scenario"])


@pytest.mark.parametrize("check", run_workflow().checks, ids=lambda c: c.claim[:60])
def test_workflow_artifact(check):
    assert check.passed, check.detail


@pytest.fixture
def no_key(monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return tmp_path / "missing.env"


def test_mode_mock_is_always_honoured(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-real-key")
    s = load_settings("mock", env_file=tmp_path / "none.env")
    assert s.mode == "mock" and "forced" in s.mode_reason


def test_auto_falls_back_to_mock_without_a_key_and_live_is_a_hard_error(no_key):
    s = load_settings("auto", env_file=no_key)
    assert s.mode == "mock" and "ANTHROPIC_API_KEY" in s.mode_reason
    with pytest.raises(SystemExit):
        load_settings("live", env_file=no_key)


def test_auto_falls_back_when_the_key_check_fails(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-real-key")
    monkeypatch.setattr(settings_mod, "_validate_key", lambda k, m: (False, "401"))
    assert load_settings("auto", env_file=tmp_path / "none.env").mode == "mock"


def test_cli_all_writes_every_report_into_output_and_passes(no_key, tmp_path):
    out = tmp_path / "output"
    args = argparse.Namespace(command="all", only=None, mode="mock", keep_recent=None, tool_timeout=0.5,
                              fuzz=200, echo=False, quiet=True)
    assert cli.run(args, output_dir=out) == 0
    names = sorted(p.name.rsplit("-", 2)[0] + p.suffix for p in out.iterdir())
    assert names == sorted(f"{n}{s}" for n in cli.ORDER for s in (".md", ".json"))
