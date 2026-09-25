"""Self-check #2: each rule fires only for the files its `paths` glob names."""

from review.checks.rules_check import check_cases, load_rules


def test_rules_load_with_expected_names_and_paths():
    rules = load_rules()
    names = {r.name for r in rules}
    assert names == {"tests", "mcp-tools"}
    by_name = {r.name: r for r in rules}
    assert by_name["tests"].paths == ("**/tests/**/*.py",)
    assert by_name["mcp-tools"].paths == ("**/issue_tracker/**/*.py",)


def test_each_sample_file_matches_exactly_the_expected_rules():
    rules = load_rules()
    cases = check_cases(rules)
    mismatches = [c for c in cases if not c.ok]
    assert mismatches == [], f"rule matching mismatch: {mismatches}"


def test_a_file_matching_neither_rule_stays_silent():
    rules = load_rules()
    unrelated = "1_agent_architecture_and_orchestration/research_coordinator/orchestration/agents.py"
    assert not any(r.matches(unrelated) for r in rules)
