"""Tests for the self-check questions in the problem statement.

The MCP tests talk to a real server process over stdio, so what they assert
is what an MCP client actually receives -- not what an in-process call
happens to return.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from mcp import Client

from issue_tracker.config import config_check
from issue_tracker.evaluation import resource_eval
from issue_tracker.evaluation import selection as sel
from issue_tracker.core.errors import TrackerError, parse_error_payload
from issue_tracker.tools.handlers import call
from issue_tracker.evaluation.probe_client import server_params
from issue_tracker.tools.toolspecs import DESCRIPTIONS_V1, DESCRIPTIONS_V2, SCHEMAS, TOOL_NAMES


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------
# Tool design
# --------------------------------------------------------------------------


def test_the_two_overlapping_tools_are_actually_overlapping():
    """If these stopped returning the same shape the eval would be vacuous."""
    a = call("search_issues", {"query": "rounding"})
    b = call("find_issues_for_path", {"path": "sample_service/checkout/pricing.py"})
    assert set(a) >= {"matchCount", "issues"}
    assert set(b) >= {"matchCount", "issues"}
    assert {i["key"] for i in a["issues"]} & {i["key"] for i in b["issues"]}


def test_v2_descriptions_name_their_sibling_as_a_boundary():
    assert "find_issues_for_path" in DESCRIPTIONS_V2["search_issues"]
    assert "search_issues" in DESCRIPTIONS_V2["find_issues_for_path"]
    assert "get_issue" in DESCRIPTIONS_V2["search_issues"]
    # v1 is the "before" picture: no boundaries at all.
    for text in DESCRIPTIONS_V1.values():
        assert not any(name in text for name in TOOL_NAMES)


def test_v2_is_substantially_more_specific_than_v1():
    for name in TOOL_NAMES:
        assert len(DESCRIPTIONS_V2[name]) > 4 * len(DESCRIPTIONS_V1[name])


def test_selection_improves_from_v1_to_v2_and_v2_is_perfect():
    settings = sel.Settings(mode="mock", mode_reason="test")
    results = run(sel.run_all(settings, trials=1))

    assert results["v1"].accuracy < 0.5
    assert results["v2"].accuracy == 1.0
    assert results["v2"].ambiguous_accuracy == 1.0


def test_selection_is_consistent_across_repeated_tries():
    settings = sel.Settings(mode="mock", mode_reason="test")
    result = run(sel.run_version(settings, "v2", trials=5))
    assert all(row.consistent for row in result.results)


# --------------------------------------------------------------------------
# Structured errors
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool,args,category,retryable",
    [
        ("find_issues_for_path", {"path": "session.py"}, "validation", False),
        ("search_issues", {"query": "a/b.py"}, "validation", False),
        ("get_issue", {"key": "CHK-999"}, "validation", False),
        ("get_issue", {"key": "AUTH-99"}, "permission", False),
    ],
)
def test_handler_errors_are_categorized(tool, args, category, retryable):
    with pytest.raises(TrackerError) as excinfo:
        call(tool, args)
    payload = excinfo.value.to_payload()
    assert payload["errorCategory"] == category
    assert payload["isRetryable"] is retryable
    assert payload["attempted"]
    assert payload["description"]


def test_transient_errors_are_the_only_retryable_ones():
    from issue_tracker.core.errors import transient_error, validation_error, permission_error

    assert transient_error("x", attempted="t").retryable is True
    assert validation_error("x", attempted="t").retryable is False
    assert permission_error("x", attempted="t").retryable is False


def test_mcp_error_result_carries_the_structured_payload():
    """The SDK swallows a plain exception into 'Error executing tool <name>'.
    This asserts the payload survives the round trip."""

    async def go():
        async with Client(server_params()) as client:
            return await client.call_tool("find_issues_for_path", {"path": "session.py"})

    result = run(go())
    assert result.is_error is True
    payload = parse_error_payload(result.content[0].text)
    assert payload["errorCategory"] == "validation"
    assert payload["isRetryable"] is False
    assert "find_issues_for_path" in payload["attempted"]
    assert "sample_service/auth/session.py" in payload["remediation"]


def test_simulated_upstream_failure_is_transient_and_retryable():
    async def go():
        env = {"TRACKER_SIMULATE": "transient", "TRACKER_SIMULATE_TOOL": "search_issues"}
        async with Client(server_params(env)) as client:
            return await client.call_tool("search_issues", {"query": "security"})

    result = run(go())
    assert result.is_error is True
    payload = parse_error_payload(result.content[0].text)
    assert payload["errorCategory"] == "transient"
    assert payload["isRetryable"] is True
    assert "partialResults" in payload


# --------------------------------------------------------------------------
# The MCP surface
# --------------------------------------------------------------------------


def test_server_exposes_every_tool_and_resource_over_stdio():
    async def go():
        async with Client(server_params()) as client:
            tools = await client.list_tools()
            resources = await client.list_resources()
            return tools, resources

    tools, resources = run(go())
    assert {t.name for t in tools.tools} == set(TOOL_NAMES)
    assert {str(r.uri) for r in resources.resources} == {
        "issues://catalog",
        "issues://paths",
        "issues://tool-guide",
    }


def test_published_schemas_carry_per_field_descriptions():
    """Auto-generated schemas silently drop field docs unless the arguments
    are Annotated; without them an agent only sees the type."""

    async def go():
        async with Client(server_params()) as client:
            return (await client.list_tools()).tools

    tools = {t.name: t for t in run(go())}
    for name in TOOL_NAMES:
        properties = tools[name].input_schema["properties"]
        assert properties, f"{name} publishes no properties"
        for field, spec in properties.items():
            assert spec.get("description"), f"{name}.{field} has no description"
        # The published required set must match the hand-written contract.
        assert set(tools[name].input_schema.get("required", [])) == set(
            SCHEMAS[name].get("required", [])
        )


def test_schema_constraints_are_enforced_before_our_code_runs():
    async def go():
        async with Client(server_params()) as client:
            return await client.call_tool("search_issues", {"query": "bug", "limit": 99})

    result = run(go())
    assert result.is_error is True
    assert "less_than_equal" in result.content[0].text


def test_resources_are_readable_and_json_parses():
    async def go():
        async with Client(server_params()) as client:
            return await client.read_resource("issues://catalog")

    catalog = json.loads(run(go()).contents[0].text)
    assert set(catalog["projects"]) == {"CHK", "AUTH", "PLAT"}
    assert catalog["currentSprint"]
    assert catalog["linkedPaths"]


# --------------------------------------------------------------------------
# Resources vs exploratory calls
# --------------------------------------------------------------------------


def test_resources_remove_every_exploratory_call():
    rows = resource_eval.run()
    summary = resource_eval.summarize(rows)
    assert summary["toolCallsWithResources"] == 0
    assert summary["toolCallsWithoutResources"] > 0
    assert all(r.satisfied_with for r in rows)


def test_at_least_one_question_is_unanswerable_without_resources():
    """Closed issues outside the sprint never surface through the tools, so
    the resource is not merely a shortcut."""
    summary = resource_eval.summarize(resource_eval.run())
    assert summary["unanswerableWithoutResources"]


# --------------------------------------------------------------------------
# Configuration scopes
# --------------------------------------------------------------------------


def test_project_config_declares_the_server_with_env_expansion():
    servers = config_check.project_servers()
    assert [s.name for s in servers] == ["issue-tracker"]
    names = {v.name for v in servers[0].env_vars}
    assert "TRACKER_API_TOKEN" in names


def test_no_credential_is_inlined_in_the_committed_config():
    assert config_check.secrets_are_inlined() == []


def test_personal_server_is_kept_out_of_the_project_config():
    project = {s.name for s in config_check.project_servers()}
    user = {s.name for s in config_check.user_scope_servers()}
    assert "scratchpad" in user
    assert "scratchpad" not in project


def test_optional_vars_have_defaults_so_the_server_starts_without_them():
    servers = config_check.project_servers()
    optional = [v for v in servers[0].env_vars if v.name != "TRACKER_API_TOKEN"]
    assert optional and all(v.has_default for v in optional)


def test_config_check_detects_an_unspawnable_command():
    """A command that is not on PATH fails at spawn time with a bare
    FileNotFoundError, and the client just reports the server as failed. The
    config check has to catch that before a session does."""
    resolved, hint = config_check.resolve_command("definitely-not-a-real-command-xyz")
    assert resolved is None
    assert hint and "not found on PATH" in hint


def test_config_check_resolves_a_command_that_is_on_path():
    import os
    import sys

    resolved, hint = config_check.resolve_command(os.path.basename(sys.executable))
    assert resolved is not None
    assert hint is None


def test_summary_reports_spawnability_for_every_configured_server():
    summary = config_check.summarize()
    assert summary["servers"]
    for server in summary["servers"]:
        assert "spawnable" in server
        # An unspawnable server must always come with an actionable hint.
        if not server["spawnable"]:
            assert server["commandHint"]


# --------------------------------------------------------------------------
# The user-scope server
# --------------------------------------------------------------------------


def test_personal_scratchpad_server_starts_and_round_trips(tmp_path):
    """The README tells the reader to register this at user scope, so it has
    to actually run."""
    import os
    import sys

    from mcp import StdioServerParameters

    from issue_tracker.config.settings import MODULE_DIR

    script = MODULE_DIR / "personal_scratchpad.py"
    assert script.is_file(), "user_scope.example.json points at a missing script"

    env = {
        **os.environ,
        "SCRATCHPAD_DIR": str(tmp_path),
        "SCRATCHPAD_LOG_LEVEL": "CRITICAL",
        "PYTHONIOENCODING": "utf-8",
    }
    params = StdioServerParameters(command=sys.executable, args=[str(script)], env=env)

    async def go():
        async with Client(params) as client:
            tools = {t.name for t in (await client.list_tools()).tools}
            await client.call_tool("append_note", {"slug": "notes", "text": "hello"})
            read = await client.call_tool("read_note", {"slug": "notes"})
            traversal = await client.call_tool("read_note", {"slug": "../secrets"})
            return tools, read, traversal

    tools, read, traversal = run(go())
    assert tools == {"append_note", "read_note", "list_notes"}
    assert read.is_error is False and "hello" in read.content[0].text
    assert traversal.is_error is True


def test_user_scope_example_points_at_a_script_that_exists():
    from issue_tracker.config.settings import MODULE_DIR

    servers = config_check.user_scope_servers()
    assert servers
    for server in servers:
        scripts = [a for a in server.args if a.endswith(".py")]
        assert scripts
        for script in scripts:
            assert (MODULE_DIR / script).is_file(), f"{script} is referenced but missing"
