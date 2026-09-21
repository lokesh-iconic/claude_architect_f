"""A coordinator/subagent research system built on the Claude Messages API."""

from .agents import AgentDefinition, build_agents
from .backend import Backend, LiveBackend, MockBackend, build_backend
from .loop import AgentFailure, run_agent
from .orchestrator import Orchestrator, RunReport, SubagentRun
from .report import render_markdown, render_trace
from .settings import Settings, load_settings

__all__ = [
    "AgentDefinition",
    "AgentFailure",
    "Backend",
    "LiveBackend",
    "MockBackend",
    "Orchestrator",
    "RunReport",
    "Settings",
    "SubagentRun",
    "build_agents",
    "build_backend",
    "load_settings",
    "render_markdown",
    "render_trace",
    "run_agent",
]
