---
description: Conventions for this repo's pytest suites
paths: ["**/tests/**/*.py"]
---

# Test conventions

- No test in this repo may require a live `ANTHROPIC_API_KEY`, a live `claude`
  CLI, or network access to pass. If the code under test has a live mode,
  test it through its mock backend.
- One test per claim a module's README self-check table makes, named so the
  claim is recognizable from the test name (`test_*attribution*`,
  `test_*dedupe*`, ...).
- Prefer a real in-process component (a real MCP client against a real
  server subprocess, a real orchestrator with a mock backend) over mocking
  the thing you're trying to prove works. Mock only the network/API edge.
- Fixtures that spawn a subprocess or server must clean up on failure too —
  use `try`/`finally` or a fixture with `yield`, not a bare setup call.
