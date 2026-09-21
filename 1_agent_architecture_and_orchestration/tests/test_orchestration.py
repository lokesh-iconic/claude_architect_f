"""Tests for the self-check questions in the problem statement.

Each test maps to a claim the README makes, so a regression in the
orchestration shows up as a failing assertion rather than a worse report.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from research_coordinator.agents import build_agents
from research_coordinator.backend import ModelResponse, TextBlock, ToolUseBlock
from research_coordinator.loop import AgentFailure, run_agent
from research_coordinator.orchestrator import Orchestrator
from research_coordinator.settings import DEFAULT_CORPUS, Settings
from research_coordinator.tools import ToolContext, ToolError, exec_read_document


def make_settings(**overrides) -> Settings:
    base = dict(
        mode="mock",
        mode_reason="test",
        subagent_timeout_s=10.0,
        max_rounds=4,
        corpus_dir=DEFAULT_CORPUS,
    )
    base.update(overrides)
    return Settings(**base)


class ScriptedBackend:
    """Returns a fixed sequence of responses, recording what it was asked."""

    name = "scripted"

    def __init__(self, script: list[ModelResponse]) -> None:
        self.script = list(script)
        self.calls: list[dict] = []

    async def create(self, *, model, system, messages, tools, effort, max_tokens):
        self.calls.append({"system": system, "messages": list(messages), "tools": tools})
        if not self.script:
            return ModelResponse("end_turn", [TextBlock("done")], [])
        return self.script.pop(0)


def tool_use(name: str, payload: dict, call_id: str = "t1") -> ModelResponse:
    block = ToolUseBlock(id=call_id, name=name, input=payload)
    return ModelResponse(
        "tool_use",
        [block],
        [{"type": "tool_use", "id": call_id, "name": name, "input": payload}],
    )


def end_turn(text: str) -> ModelResponse:
    return ModelResponse("end_turn", [TextBlock(text)], [{"type": "text", "text": text}])


# --------------------------------------------------------------------------
# The agentic loop
# --------------------------------------------------------------------------


def test_loop_continues_on_tool_use_and_stops_on_end_turn():
    settings = make_settings()
    agent = build_agents(settings)["document_analyst"]
    backend = ScriptedBackend(
        [
            tool_use("list_documents", {}, "a"),
            tool_use("search_documents", {"query": "clearance"}, "b"),
            tool_use(
                "submit_findings",
                {"summary": "s", "findings": [], "gaps": []},
                "c",
            ),
        ]
    )

    result = asyncio.run(
        run_agent(
            agent=agent,
            backend=backend,
            settings=settings,
            prompt="brief",
            ctx=ToolContext(settings.corpus_dir, "mock"),
            terminal_tool="submit_findings",
        )
    )

    assert result.turns == 3
    assert result.terminal_payload == {"summary": "s", "findings": [], "gaps": []}


def test_out_of_allowlist_tool_call_is_refused_not_executed():
    """The document_analyst has no web access; asking for it must fail closed."""
    settings = make_settings()
    agent = build_agents(settings)["document_analyst"]
    backend = ScriptedBackend(
        [
            tool_use("web_search", {"query": "anything"}, "a"),
            tool_use("submit_findings", {"summary": "s", "findings": [], "gaps": []}, "b"),
        ]
    )

    result = asyncio.run(
        run_agent(
            agent=agent,
            backend=backend,
            settings=settings,
            prompt="brief",
            ctx=ToolContext(settings.corpus_dir, "mock"),
            terminal_tool="submit_findings",
        )
    )

    assert result.refused_tool_calls == ["web_search"]
    refusal = json.loads(backend.calls[1]["messages"][-1]["content"][0]["content"])
    assert refusal["errorCategory"] == "permission"
    assert refusal["isRetryable"] is False


def test_subagent_only_sees_tools_on_its_allowlist():
    settings = make_settings()
    agents = build_agents(settings)
    doc_tools = {t["name"] for t in agents["document_analyst"].tool_specs(settings)}
    web_tools = {t["name"] for t in agents["web_researcher"].tool_specs(settings)}

    assert "Task" not in doc_tools and "Task" not in web_tools
    assert "web_search" not in doc_tools
    assert "read_document" not in web_tools
    assert "Task" in {t["name"] for t in agents["coordinator"].tool_specs(settings)}


def test_terminal_tool_is_enforced_programmatically():
    """Prompt instructions alone are not the contract - the loop is."""
    settings = make_settings()
    agent = build_agents(settings)["web_researcher"]
    backend = ScriptedBackend([end_turn("here is some prose"), end_turn("more prose")])

    with pytest.raises(AgentFailure) as excinfo:
        asyncio.run(
            run_agent(
                agent=agent,
                backend=backend,
                settings=settings,
                prompt="brief",
                ctx=ToolContext(settings.corpus_dir, "mock"),
                terminal_tool="submit_findings",
            )
        )

    assert excinfo.value.failure_type == "contract_violation"
    assert len(backend.calls) == 2  # nudged exactly once before giving up


def test_nudge_recovers_a_missing_terminal_call():
    settings = make_settings()
    agent = build_agents(settings)["web_researcher"]
    backend = ScriptedBackend(
        [
            end_turn("prose instead of a tool call"),
            tool_use("submit_findings", {"summary": "ok", "findings": [], "gaps": []}, "z"),
        ]
    )

    result = asyncio.run(
        run_agent(
            agent=agent,
            backend=backend,
            settings=settings,
            prompt="brief",
            ctx=ToolContext(settings.corpus_dir, "mock"),
            terminal_tool="submit_findings",
        )
    )
    assert result.terminal_payload["summary"] == "ok"


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------


def test_read_document_refuses_paths_outside_the_corpus():
    ctx = ToolContext(DEFAULT_CORPUS, "mock")
    with pytest.raises(ToolError) as excinfo:
        exec_read_document({"path": "../../../pyproject.toml"}, ctx)
    assert excinfo.value.category == "permission"


def test_read_document_reports_a_bad_path_as_validation_not_a_crash():
    ctx = ToolContext(DEFAULT_CORPUS, "mock")
    with pytest.raises(ToolError) as excinfo:
        exec_read_document({"path": "nope.md"}, ctx)
    assert excinfo.value.category == "validation"
    assert excinfo.value.retryable is False


def test_tool_error_payload_is_structured():
    payload = json.loads(
        ToolError("boom", category="transient", retryable=True, attempted="x()").to_payload()
    )
    assert payload["errorCategory"] == "transient"
    assert payload["isRetryable"] is True
    assert payload["description"] == "boom"


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


def run_research(topic="the impact of AI on creative industries", **kwargs):
    settings = make_settings()
    orchestrator = Orchestrator(settings, verbose=False, **kwargs)
    return asyncio.run(orchestrator.research(topic))


def test_attribution_every_claim_resolves_to_a_registered_source():
    report = run_research()
    known = {s.id for s in report.sources}
    claims = [f for run in report.runs for f in run.findings]

    assert claims, "the run produced no findings at all"
    assert all(f.source_id in known for f in claims)
    # One locator gets exactly one id, so the same URL is never cited twice.
    assert len({s.locator for s in report.sources}) == len(report.sources)


def test_decomposition_spans_several_angles_and_both_specialists():
    report = run_research()
    assert len(report.rounds) >= 2, "no refinement round was issued"
    assert len({r.title for r in report.runs}) == len(report.runs)
    assert {r.subagent for r in report.runs} == {"web_researcher", "document_analyst"}


def test_parallel_run_is_faster_than_the_identical_serial_plan():
    parallel = run_research(parallel=True)
    serial = run_research(parallel=False)

    assert [r.title for r in parallel.runs] == [r.title for r in serial.runs]
    assert parallel.total_elapsed_s < serial.total_elapsed_s
    assert parallel.parallel_speedup > serial.parallel_speedup


def test_a_timed_out_subagent_still_yields_a_usable_report():
    # Comfortably above a healthy mock subagent (~0.4-0.7s) so only the
    # deliberately hung one trips the deadline.
    settings = make_settings(subagent_timeout_s=1.5)
    orchestrator = Orchestrator(
        settings, verbose=False, simulate_timeout="document_analyst"
    )
    report = asyncio.run(orchestrator.research("a topic"))

    failed = report.failed_runs
    assert len(failed) == 1
    error = failed[0].error
    assert error["category"] == "transient"
    assert "toolCallsCompleted" in error["partial"]

    # The report survives, still cites the successful subtasks, and says what
    # is missing rather than failing silently.
    assert report.sources
    assert "Coverage not obtained" in report.report_markdown
    assert failed[0].title in report.report_markdown


def test_task_rejects_a_brief_that_relies_on_inherited_context():
    settings = make_settings()
    orchestrator = Orchestrator(settings, verbose=False)

    call = ToolUseBlock(
        id="x",
        name="Task",
        input={"subagent": "web_researcher", "title": "t", "brief": "research the above"},
    )
    with pytest.raises(ToolError) as excinfo:
        asyncio.run(orchestrator._handle_task(call))
    assert excinfo.value.category == "validation"


def test_task_rejects_an_unknown_specialist():
    settings = make_settings()
    orchestrator = Orchestrator(settings, verbose=False)
    call = ToolUseBlock(
        id="x",
        name="Task",
        input={
            "subagent": "database_expert",
            "title": "t",
            "brief": "A brief long enough to pass the length check for delegation.",
        },
    )
    with pytest.raises(ToolError) as excinfo:
        asyncio.run(orchestrator._handle_task(call))
    assert excinfo.value.category == "validation"


# --------------------------------------------------------------------------
# Mode resolution
# --------------------------------------------------------------------------


def test_missing_key_falls_back_to_mock(tmp_path: Path, monkeypatch):
    from research_coordinator.settings import load_settings

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=\n", encoding="utf-8")

    settings = load_settings(env_file=env)
    assert settings.mode == "mock"
    assert "no ANTHROPIC_API_KEY" in settings.mode_reason


def test_invalid_key_falls_back_to_mock(tmp_path: Path, monkeypatch):
    import research_coordinator.settings as settings_module

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-a-real-key")
    monkeypatch.setattr(
        settings_module, "_validate_key", lambda key, model: (False, "401 invalid key")
    )

    settings = settings_module.load_settings(env_file=tmp_path / ".env")
    assert settings.mode == "mock"
    assert "401 invalid key" in settings.mode_reason


def test_valid_key_selects_live_mode(tmp_path: Path, monkeypatch):
    import research_coordinator.settings as settings_module

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-looks-real")
    monkeypatch.setattr(settings_module, "_validate_key", lambda key, model: (True, "ok"))

    settings = settings_module.load_settings(env_file=tmp_path / ".env")
    assert settings.mode == "live"
    assert settings.is_live


def test_live_mode_swaps_in_the_server_side_web_search_tool():
    live = make_settings(mode="live", api_key="sk-ant-x", enable_web_search=True)
    specs = build_agents(live)["web_researcher"].tool_specs(live)
    search = next(t for t in specs if t["name"] == "web_search")
    assert search["type"] == "web_search_20260209"

    offline = make_settings()
    specs = build_agents(offline)["web_researcher"].tool_specs(offline)
    search = next(t for t in specs if t["name"] == "web_search")
    assert "type" not in search and "input_schema" in search
