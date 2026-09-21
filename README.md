# claude_architect_foundations

Each solved exercise gets its own folder with its own README covering what it
builds, how to run it, and how to verify it.

| Module | Builds |
|---|---|
| [1_agent_architecture_and_orchestration/](1_agent_architecture_and_orchestration/) | A multi-agent research coordinator: decomposition, parallel delegation to specialist subagents, cited synthesis |
| [2_tool_design_and_MCP_integration/](2_tool_design_and_MCP_integration/) | An issue-tracker MCP server: overlapping-tool disambiguation, structured errors, resources, project/user scopes |

## Setup

One [uv](https://docs.astral.sh/uv/) project shared by every module.

```bash
uv sync
cp .env.example .env    # then add ANTHROPIC_API_KEY, or leave it empty
```

Then run a module from its own directory:

```bash
cd 1_agent_architecture_and_orchestration
uv run python main.py "your topic"
```

Without a valid `ANTHROPIC_API_KEY`, modules fall back to an offline mock mode
so they still run end to end. Each module writes its results to its own
`output/` folder, which is git-ignored.
