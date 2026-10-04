"""The multi-agent pipeline.

    question
      -> planner (route + decompose into tasks)
      -> specialists run in parallel waves (fed_research / data_analyst / web_research / market_data),
         each a ReAct tool-calling loop
      -> corrective fallback to web search when the collection had nothing
      -> synthesizer writes a cited answer
      -> verifier checks it: accept | revise | research (follow-up tasks, then re-synthesize)
      -> answer + sources + full trace
"""

from __future__ import annotations

import asyncio
import datetime as dt
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Callable

from .agents.base import AgentResult
from .agents.planner import Plan, Task, make_plan
from .agents.specialists import build_specialists
from .agents.writer import direct_answer, revise, synthesize, verify
from .evidence import EvidenceStore, cited_ids
from .gpu_client import GPUClient
from .llm import LLM, Usage, run_usage
from .retrieval.index import CorpusIndex
from .tools.base import RunContext
from .trace import Event, Trace


@dataclass
class RunResult:
    question: str
    answer: str
    sources: list[dict]
    plan: dict
    agent_results: list[dict]
    verifications: list[dict]
    trace: list[dict]
    usage: dict
    seconds: float
    agents_used: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


class Orchestrator:
    def __init__(self, index: CorpusIndex | None = None, llm: LLM | None = None, gpu: GPUClient | None = None,
                 verify: bool = True, max_research_rounds: int = 1, thinking: bool = False):
        self.gpu = gpu or GPUClient()
        self.index = index or CorpusIndex(gpu=self.gpu)
        self.llm = llm or LLM()
        self.verify_enabled = verify
        self.max_research_rounds = max_research_rounds
        self.thinking = thinking

    # ------------------------------------------------------------------ execution
    async def _execute(self, ctx: RunContext, tasks: list[Task], specialists: dict) -> list[AgentResult]:
        done: dict[str, AgentResult] = {}
        pending = list(tasks)
        while pending:
            ready = [t for t in pending if all(d in done for d in t.depends_on)] or pending
            for t in ready:
                t.depends_on = [d for d in t.depends_on if d in done]

            async def run_one(t: Task) -> AgentResult:
                context = "\n\n".join(done[d].render() for d in t.depends_on)
                return await specialists[t.agent].run(ctx, t.instruction, task_id=t.id, context=context)

            outs = await asyncio.gather(*(run_one(t) for t in ready))
            for t, r in zip(ready, outs):
                done[t.id] = r
            pending = [t for t in pending if t.id not in done]
        return [done[t.id] for t in tasks if t.id in done]

    # ------------------------------------------------------------------ main entry
    async def run(self, question: str, history: list[dict] | None = None,
                  listeners: list[Callable[[Event], None]] | None = None,
                  today: dt.date | None = None, verify: bool | None = None) -> RunResult:
        """``verify`` overrides the instance default for this run only."""
        t0 = time.time()
        usage = Usage()
        token = run_usage.set(usage)
        try:
            return await self._run(question, history, listeners or [], today or dt.date.today(), usage, t0,
                                   self.verify_enabled if verify is None else verify)
        finally:
            run_usage.reset(token)

    async def _run(self, question, history, listeners, today, usage, t0, verify_enabled) -> RunResult:
        trace = Trace()
        trace.listeners.extend(listeners)
        ctx = RunContext(index=self.index, llm=self.llm, evidence=EvidenceStore(), trace=trace, today=today)
        today_s = today.strftime("%A, %B %d, %Y")
        try:
            trace.emit("planner", "agent_start", task="route and decompose the question")
            plan = await make_plan(self.llm, question, today_s, self.index.corpus_card(), history,
                                   dataset_card=self.index.dataset_card(), n_docs=len(self.index.docs),
                                   n_tables=sum(1 for t in self.index.tables.catalog.values() if t["kind"] == "table"))
            trace.emit("planner", "plan", **plan.as_dict())
            q = plan.standalone_question or question
            history_note = "" if q == question else f"(Standalone form of the question: {q})\n"

            if not plan.needs_tools:
                trace.emit("direct", "agent_start", task="answer from general knowledge")
                answer = await direct_answer(self.llm, question, today_s, history_note,
                                             on_delta=(lambda d: trace.stream("direct", d)) if listeners else None)
                trace.emit("direct", "final", answer=answer)
                return self._result(question, answer, ctx, plan, [], [], usage, t0)

            specialists = build_specialists(self.index, today_s, thinking=self.thinking)
            results = await self._execute(ctx, plan.tasks, specialists)

            # Corrective step: the collection (or every agent) found nothing -> try the open web once.
            if not any(r.evidence_ids or r.seen_ids for r in results) and \
                    not any(t.agent == "web_research" for t in plan.tasks):
                trace.emit("orchestrator", "info", message="no evidence found; falling back to web research")
                fb = Task(id="fallback", agent="web_research", instruction=q)
                results += await self._execute(ctx, [fb], specialists)

            stream = (lambda d: trace.stream("synthesizer", d)) if listeners else None
            trace.emit("synthesizer", "agent_start", task="write the cited answer")
            draft = await synthesize(self.llm, question, today_s, results, ctx.evidence, history_note, stream)
            trace.emit("synthesizer", "synthesis", answer=draft)

            verifications: list[dict] = []
            rounds = 0
            while verify_enabled and len(verifications) < 3:
                trace.emit("verifier", "agent_start", task="fact-check the draft")
                v = await verify(self.llm, question, today_s, draft, ctx.evidence)
                verifications.append(v)
                trace.emit("verifier", "verification", **v)
                if v["verdict"] == "accept":
                    break
                follow = [Task(id=f"r{rounds + 1}-{i + 1}", agent=t["agent"], instruction=t["instruction"])
                          for i, t in enumerate(v["follow_up_tasks"][:2])
                          if t.get("agent") in specialists and t.get("instruction")]
                if v["verdict"] == "research" and rounds < self.max_research_rounds and follow:
                    rounds += 1
                    results += await self._execute(ctx, follow, specialists)
                    trace.emit("synthesizer", "agent_start", task="rewrite with the new findings")
                    draft = await synthesize(self.llm, question, today_s, results, ctx.evidence, history_note,
                                             stream)
                    trace.emit("synthesizer", "synthesis", answer=draft)
                    continue
                if v["issues"]:
                    trace.emit("synthesizer", "agent_start", task="revise per fact-check")
                    draft = await revise(self.llm, question, today_s, draft, v["issues"], results, ctx.evidence,
                                         stream)
                    trace.emit("synthesizer", "synthesis", answer=draft, revised=True)
                break
            return self._result(question, draft, ctx, plan, results, verifications, usage, t0)
        except Exception as e:
            trace.emit("orchestrator", "error", error=f"{type(e).__name__}: {e}")
            raise
        finally:
            if ctx.http is not None:
                await ctx.http.aclose()

    # ------------------------------------------------------------------ output
    def _result(self, question: str, draft: str, ctx: RunContext, plan: Plan, results: list[AgentResult],
                verifications: list[dict], usage: Usage, t0: float) -> RunResult:
        answer, sources = finalize(draft, ctx.evidence, self.index)
        ctx.trace.emit("orchestrator", "final", answer=answer, sources=[s["id"] for s in sources])
        return RunResult(
            question=question, answer=answer, sources=sources, plan=plan.as_dict(),
            agent_results=[asdict(r) for r in results], verifications=verifications,
            trace=ctx.trace.to_list(),
            usage={"llm_calls": usage.calls, "prompt_tokens": usage.prompt_tokens,
                   "completion_tokens": usage.completion_tokens, "llm_seconds": round(usage.seconds, 1),
                   "by_agent": usage.by_agent, "tool_calls": ctx.trace.tool_counts()},
            seconds=round(time.time() - t0, 1),
            agents_used=ctx.trace.agents_used(),
        )


def finalize(draft: str, store: EvidenceStore, index: CorpusIndex | None = None) -> tuple[str, list[dict]]:
    """Drop citations to unknown IDs and build the source list (with page-level PDF links)."""
    def fix(m: re.Match) -> str:
        ids = [i for i in re.split(r"\s*[,;]\s*", m.group(1)) if store.get(i)]
        return "".join(f"[{i}]" for i in ids)

    answer = re.sub(r"\[((?:[DWM]\d+)(?:\s*[,;]\s*[DWM]\d+)*)\]", fix, draft).strip()
    sources = []
    for eid in cited_ids(answer):
        ev = store.get(eid)
        url = ev.url
        if ev.kind == "doc" and index is not None:
            doc = index.docs.get(ev.meta.get("doc_id", ""))
            if doc:
                url = doc["url"]
                if doc.get("format", "pdf") == "pdf" and ev.meta.get("unit", "page") == "page":
                    url += f"#page={ev.meta.get('page_start', 1)}"
        sources.append({"id": eid, "kind": ev.kind, "title": ev.title, "source": ev.source, "url": url,
                        "excerpt": ev.text[:600], "meta": ev.meta})
    return answer, sources
