import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from support_agent.backends.backend import MockBackend  # noqa: E402
from support_agent.conversation.memory import MockSummarizer  # noqa: E402
from support_agent.evaluation.scenarios import agent_factory  # noqa: E402
from support_agent.config.settings import Settings  # noqa: E402


@pytest.fixture
def settings() -> Settings:
    return Settings(mode="mock", mode_reason="test", tool_timeout_s=0.3)


@pytest.fixture
def build(settings):
    return agent_factory(settings, lambda: (MockBackend(), MockSummarizer()))
