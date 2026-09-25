"""Coordinator/subagent orchestration.

Owns the `Task` tool: validating a delegation, spawning the subagent with a
fresh context, enforcing a timeout, retrying once when the failure says it is
retryable, and registering every returned finding in a source table so
attribution survives synthesis.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any

from .agents import SUBAGENT_NAMES, build_agents
from ..backends.backend import Backend, build_backend
from .loop import AgentFailure, LoopResult, run_agent
from ..config.settings import Settings
from .tools import ToolContext, ToolError


@dataclass
class Source:
    id: str
    title: str
    locator: str
    type: str
    date: str
    specialist: str
    subtask: str


@dataclass
class Finding:
    source_id: str
    claim: str
    confidence: str


@dataclass
class SubagentRun:
    task_id: str
    subagent: str
    title: str
    brief: str
    status: str  # ok | failed
    started_at: float
    elapsed_s: float
    attempts: int = 1
    summary: str = ""
    findings: list[Finding] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    error: dict[str, Any] | None = None


@dataclass
class RunReport:
    topic: str
    mode: str
    mode_reason: str
    parallel: bool
    report_markdown: str
    sources: list[Source]
    runs: list[SubagentRun]
    rounds: list[list[str]]
    total_elapsed_s: float
    subagent_cpu_s: float
    coordinator_turns: int
    input_tokens: int
    output_tokens: int
    refused_tool_calls: list[str]

    @property
    def failed_runs(self) -> list[SubagentRun]:
        return [r for r in self.runs if r.status != "ok"]

    @property
    def parallel_speedup(self) -> float:
        """Summed subagent time / wall clock. Above 1.0 means real overlap."""
        return self.subagent_cpu_s / self.total_elapsed_s if self.total_elapsed_s else 0.0


GAP_REVIEW_NOTE = (
    "Coverage check before you write anything: name any dimension of the topic "
    "that the findings above do not cover, and any claim two subagents disagree "
    "on. If a material gap remains, issue one more round of Task calls now. "
    "Otherwise write the final report."
)


class Orchestrator:
    def __init__(
        self,
        settings: Settings,
        *,
        backend: Backend | None = None,
        parallel: bool = True,
        simulate_timeout: str | None = None,
        verbose: bool = True,
    ) -> None:
        self.settings = settings
        self.backend = backend or build_backend(settings)
        self.agents = build_agents(settings)
        self.ctx = ToolContext(corpus_dir=settings.corpus_dir, mode=settings.mode)
        self.parallel = parallel
        self.simulate_timeout = simulate_timeout
        self.verbose = verbose

        self.sources: list[Source] = []
        self.runs: list[SubagentRun] = []
        self.rounds: list[list[str]] = []
        self._task_seq = 0
        self._round_seq = 0

    # -- logging -----------------------------------------------------------

    def log(self, message: str) -> None:
        if self.verbose:
            print(message, flush=True)

    # -- public entry point ------------------------------------------------

    async def research(self, topic: str) -> RunReport:
        coordinator = self.agents["coordinator"]
        prompt = (
            f"Research topic: {topic}\n\n"
            "Produce a comprehensive, cited report. Decompose the topic first, "
            "delegate in parallel, then synthesize."
        )

        started = time.perf_counter()
        try:
            loop_result: LoopResult = await run_agent(
                agent=coordinator,
                backend=self.backend,
                settings=self.settings,
                prompt=prompt,
                ctx=self.ctx,
                special_handlers={"Task": self._handle_task},
                max_turns=self.settings.max_rounds + 2,
                on_event=self._on_event,
                round_hook=lambda n: GAP_REVIEW_NOTE if n == 1 else None,
                parallel_tools=self.parallel,
            )
            report_md = loop_result.final_text
            turns = loop_result.turns
            refused = loop_result.refused_tool_calls
            in_tok, out_tok = loop_result.input_tokens, loop_result.output_tokens
        except AgentFailure as exc:
            report_md = (
                f"> The coordinator failed before producing a report "
                f"({exc.failure_type}): {exc.message}\n\n"
                "The findings collected before the failure are listed below."
            )
            turns, refused, in_tok, out_tok = 0, [], 0, 0

        elapsed = time.perf_counter() - started
        return RunReport(
            topic=topic,
            mode=self.settings.mode,
            mode_reason=self.settings.mode_reason,
            parallel=self.parallel,
            report_markdown=report_md,
            sources=list(self.sources),
            runs=list(self.runs),
            rounds=list(self.rounds),
            total_elapsed_s=elapsed,
            subagent_cpu_s=sum(r.elapsed_s for r in self.runs),
            coordinator_turns=turns,
            input_tokens=in_tok + sum(r.input_tokens for r in self.runs),
            output_tokens=out_tok + sum(r.output_tokens for r in self.runs),
            refused_tool_calls=refused,
        )

    # -- the Task tool -----------------------------------------------------

    def _on_event(self, kind: str, payload: dict[str, Any]) -> None:
        if kind == "tool_batch":
            tasks = [t for t in payload.get("tools", []) if t == "Task"]
            if tasks:
                self._round_seq = len(self.rounds)
                mode = "in parallel" if self.parallel and len(tasks) > 1 else "serially"
                self.log(f"\nRound {self._round_seq + 1}: {len(tasks)} subtask(s), {mode}")
        elif kind == "tool_call" and payload.get("tool") == "Task":
            args = payload.get("input", {})
            self.log(f"  -> Task[{args.get('subagent')}] {args.get('title')}")
        elif kind == "nudge":
            self.log(f"  !  {payload['agent']} had to be nudged to call {payload['tool']}")

    async def _handle_task(self, call: Any) -> str:
        """Execute one Task tool call: spawn a subagent, return its findings."""
        args = call.input
        subagent_name = str(args.get("subagent", "")).strip()
        title = str(args.get("title", "")).strip() or "untitled subtask"
        brief = str(args.get("brief", "")).strip()

        if subagent_name not in SUBAGENT_NAMES:
            raise ToolError(
                f"unknown subagent {subagent_name!r}; choose one of {', '.join(SUBAGENT_NAMES)}",
                category="validation",
                retryable=False,
                attempted=f"Task(subagent={subagent_name!r})",
            )
        if len(brief) < 40:
            raise ToolError(
                "brief is too short to stand alone. The subagent inherits no "
                "context, so the brief must restate the topic and the angle.",
                category="validation",
                retryable=False,
                attempted=f"Task({title!r})",
            )

        self._task_seq += 1
        task_id = f"T{self._task_seq}"
        self._record_round(title)

        run = await self._run_subagent_with_retry(task_id, subagent_name, title, brief)
        self.runs.append(run)

        if run.status != "ok":
            return json.dumps(
                {
                    "error": True,
                    "taskId": task_id,
                    "subtask": title,
                    "errorCategory": run.error["category"],
                    "isRetryable": run.error["retryable"],
                    "description": run.error["message"],
                    "attempted": run.error["attempted"],
                    "attempts": run.attempts,
                    "partialResults": run.error.get("partial"),
                    "guidance": (
                        "This angle has no findings. Either re-delegate it with a "
                        "narrower brief, or write the report without it and say so."
                    ),
                },
                indent=2,
            )

        return json.dumps(
            {
                "taskId": task_id,
                "subtask": title,
                "specialist": subagent_name,
                "summary": run.summary,
                "findings": [
                    {"sourceId": f.source_id, "claim": f.claim, "confidence": f.confidence}
                    for f in run.findings
                ],
                "gaps": run.gaps,
                "note": "Cite these claims by sourceId, e.g. [S2].",
            },
            indent=2,
        )

    def _record_round(self, title: str) -> None:
        if len(self.rounds) <= self._round_seq:
            self.rounds.append([])
        self.rounds[self._round_seq].append(title)

    async def _run_subagent_with_retry(
        self, task_id: str, subagent_name: str, title: str, brief: str
    ) -> SubagentRun:
        attempts = 0
        last: SubagentRun | None = None
        while attempts < 2:
            attempts += 1
            run = await self._run_subagent(task_id, subagent_name, title, brief)
            run.attempts = attempts
            if run.status == "ok" or not run.error or not run.error["retryable"]:
                return run
            last = run
            self.log(f"  ~  {task_id} {title}: retryable failure, one retry")
        assert last is not None
        return last

    async def _run_subagent(
        self, task_id: str, subagent_name: str, title: str, brief: str
    ) -> SubagentRun:
        agent = self.agents[subagent_name]
        started = time.perf_counter()
        progress: list[dict[str, Any]] = []

        def sink(kind: str, payload: dict[str, Any]) -> None:
            if kind == "tool_call":
                progress.append({"tool": payload["tool"], "input": payload.get("input")})

        async def body() -> LoopResult:
            if self.simulate_timeout and self.simulate_timeout in {subagent_name, title}:
                # Deliberately blow the deadline to exercise the timeout path.
                await asyncio.sleep(self.settings.subagent_timeout_s + 5)
            return await run_agent(
                agent=agent,
                backend=self.backend,
                settings=self.settings,
                prompt=brief,
                ctx=self.ctx,
                terminal_tool="submit_findings",
                max_turns=10,
                on_event=sink,
                parallel_tools=self.parallel,
            )

        base = SubagentRun(
            task_id=task_id,
            subagent=subagent_name,
            title=title,
            brief=brief,
            status="failed",
            started_at=started,
            elapsed_s=0.0,
        )

        try:
            result = await asyncio.wait_for(body(), timeout=self.settings.subagent_timeout_s)
        except asyncio.TimeoutError:
            base.elapsed_s = time.perf_counter() - started
            base.tool_calls = progress
            base.error = {
                "category": "transient",
                "retryable": False,  # a deterministic simulated hang: retrying burns the clock twice
                "message": f"subagent exceeded its {self.settings.subagent_timeout_s:.0f}s deadline",
                "attempted": f"{subagent_name} on {title!r}",
                "partial": {"toolCallsCompleted": progress},
            }
            self.log(f"  x  {task_id} {title}: TIMEOUT after {base.elapsed_s:.1f}s")
            return base
        except AgentFailure as exc:
            base.elapsed_s = time.perf_counter() - started
            base.tool_calls = progress
            base.error = {
                "category": "internal" if not exc.retryable else "transient",
                "retryable": exc.retryable,
                "message": exc.message,
                "attempted": exc.attempted,
                "partial": exc.partial,
            }
            self.log(f"  x  {task_id} {title}: {exc.failure_type} - {exc.message}")
            return base

        base.elapsed_s = result.elapsed_s
        base.tool_calls = result.tool_calls
        base.input_tokens = result.input_tokens
        base.output_tokens = result.output_tokens
        payload = result.terminal_payload or {}
        base.summary = str(payload.get("summary", "")).strip()
        base.gaps = [str(g) for g in payload.get("gaps", []) or []]
        base.findings = [
            Finding(
                source_id=self._register_source(item, subagent_name, title),
                claim=str(item.get("claim", "")).strip(),
                confidence=str(item.get("confidence", "unknown")),
            )
            for item in payload.get("findings", []) or []
        ]
        base.status = "ok"
        self.log(
            f"  v  {task_id} {title}: {len(base.findings)} finding(s) in {base.elapsed_s:.1f}s"
        )
        return base

    def _register_source(self, item: dict[str, Any], specialist: str, subtask: str) -> str:
        """Assign a stable [S#] id, reusing one when the locator repeats."""
        locator = str(item.get("source_locator", "")).strip() or "unattributed"
        for existing in self.sources:
            if existing.locator == locator:
                return existing.id
        source = Source(
            id=f"S{len(self.sources) + 1}",
            title=str(item.get("source_title", "")).strip() or locator,
            locator=locator,
            type=str(item.get("source_type", "unknown")),
            date=str(item.get("source_date", "unknown")),
            specialist=specialist,
            subtask=subtask,
        )
        self.sources.append(source)
        return source.id
