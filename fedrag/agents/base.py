"""Tool-using agent: a ReAct loop over native function calling.

Each turn the model may call several tools; they run concurrently and their
results go back into the conversation. The agent ends by calling the
``submit_findings`` tool, which forces a structured report (answer, cited key
facts, confidence, gaps). On the last allowed step the finish tool is forced.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field

from ..evidence import cited_ids
from ..tools.base import RunContext, Tool, schema

FINISH = "submit_findings"
FINISH_SPEC = {
    "type": "function",
    "function": {
        "name": FINISH,
        "description": "Submit your final findings for the task. Call this exactly once, when done.",
        "parameters": schema({
            "answer": {"type": "string",
                       "description": "Direct answer to the task (2-8 sentences), citing evidence IDs like [D3]."},
            "key_facts": {"type": "array", "items": {"type": "string"},
                          "description": "Specific supporting facts, figures, dates or short quotes, each ending "
                                         "with its evidence ID(s)."},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            "gaps": {"type": "string",
                     "description": "What could not be found or verified (empty string if nothing)."},
        }, ["answer", "key_facts", "confidence", "gaps"]),
    },
}


@dataclass
class AgentResult:
    agent: str
    task_id: str
    task: str
    answer: str
    key_facts: list[str] = field(default_factory=list)
    confidence: str = "low"
    gaps: str = ""
    evidence_ids: list[str] = field(default_factory=list)
    seen_ids: list[str] = field(default_factory=list)
    steps: int = 0
    tool_calls: int = 0
    error: str | None = None

    def render(self) -> str:
        facts = "\n".join(f"  - {f}" for f in self.key_facts) or "  (none)"
        return (f"### Task {self.task_id} [{self.agent}] {self.task}\n"
                f"Answer: {self.answer}\nKey facts:\n{facts}\n"
                f"Confidence: {self.confidence}" + (f"\nGaps: {self.gaps}" if self.gaps else ""))


class ToolAgent:
    def __init__(self, name: str, system_prompt: str, tools: list[Tool], *, max_steps: int = 8,
                 thinking: bool = False, temperature: float = 0.3, max_tokens: int = 3000):
        self.name = name
        self.system_prompt = system_prompt
        self.tools = {t.name: t for t in tools}
        self.max_steps = max_steps
        self.thinking = thinking
        self.temperature = temperature
        self.max_tokens = max_tokens

    async def _exec(self, ctx: RunContext, tc, task_id: str) -> str:
        ctx.trace.emit(self.name, "tool_call", task_id=task_id, tool=tc.name, args=tc.arguments)
        if tc.parse_error:
            out = f"ERROR: {tc.parse_error}. Re-issue the call with valid JSON arguments."
        elif tc.name not in self.tools:
            out = f"ERROR: unknown tool {tc.name!r}. Available: {', '.join(self.tools)} or {FINISH}."
        else:
            out = await self.tools[tc.name](ctx, tc.arguments)
        ctx.trace.emit(self.name, "tool_result", task_id=task_id, tool=tc.name, ids=cited_ids(out)[:12],
                       preview=out[:300], chars=len(out))
        return out

    async def run(self, ctx: RunContext, task: str, task_id: str = "t1", context: str = "") -> AgentResult:
        ctx.trace.emit(self.name, "agent_start", task_id=task_id, task=task)
        user = f"Task: {task}"
        if context:
            user += f"\n\nContext from the orchestrator:\n{context}"
        messages: list[dict] = [{"role": "system", "content": self.system_prompt}, {"role": "user", "content": user}]
        specs = [t.spec() for t in self.tools.values()] + [FINISH_SPEC]
        n_calls = 0
        seen: list[str] = []
        nudged = False
        try:
            for step in range(1, self.max_steps + 1):
                last = step == self.max_steps
                if last:
                    messages.append({"role": "user", "content": "Step budget reached. Call submit_findings now "
                                                                "with what you have (state gaps honestly)."})
                resp = await ctx.llm.chat(
                    messages, agent=self.name, tools=specs,
                    tool_choice={"type": "function", "function": {"name": FINISH}} if last else "auto",
                    thinking=self.thinking, temperature=self.temperature, max_tokens=self.max_tokens,
                )
                messages.append(resp.assistant_message())
                if resp.reasoning:
                    ctx.trace.emit(self.name, "thought", task_id=task_id, text=resp.reasoning[:1500])
                elif resp.content and resp.tool_calls:
                    ctx.trace.emit(self.name, "thought", task_id=task_id, text=resp.content[:1500])

                if not resp.tool_calls:
                    if not nudged and not last:
                        nudged = True
                        messages.append({"role": "user", "content": "Use the tools to research the task if you "
                                         "have not yet, then call submit_findings with your final findings."})
                        continue
                    return self._finish(ctx, task_id, task, {"answer": resp.content, "key_facts": [],
                                                              "confidence": "low", "gaps": ""}, step, n_calls, seen)

                finish = [tc for tc in resp.tool_calls if tc.name == FINISH]
                others = [tc for tc in resp.tool_calls if tc.name != FINISH]
                if others:
                    n_calls += len(others)
                    outs = await asyncio.gather(*(self._exec(ctx, tc, task_id) for tc in others))
                    for tc, out in zip(others, outs):
                        seen += [i for i in cited_ids(out) if i not in seen]
                        messages.append({"role": "tool", "tool_call_id": tc.id, "content": out})
                if finish:
                    if others and not last:
                        # findings were written before seeing these results: let the agent review them first
                        messages.append({"role": "tool", "tool_call_id": finish[0].id,
                                         "content": "Not accepted yet: review the tool results above first, "
                                                    "then call submit_findings again."})
                        continue
                    return self._finish(ctx, task_id, task, finish[0].arguments, step, n_calls, seen)
            return self._finish(ctx, task_id, task, {"answer": "", "key_facts": [], "confidence": "low",
                                                     "gaps": "step budget exhausted"}, self.max_steps, n_calls, seen)
        except Exception as e:  # an agent failure must not sink the whole run
            ctx.trace.emit(self.name, "error", error=f"{type(e).__name__}: {e}")
            return AgentResult(agent=self.name, task_id=task_id, task=task, answer="", confidence="low",
                               gaps=f"agent failed: {type(e).__name__}: {e}", seen_ids=seen, tool_calls=n_calls,
                               error=str(e))

    def _finish(self, ctx: RunContext, task_id: str, task: str, args: dict, steps: int, n_calls: int,
                seen: list[str]) -> AgentResult:
        facts = args.get("key_facts") or []
        if isinstance(facts, str):
            try:
                facts = json.loads(facts)
            except json.JSONDecodeError:
                facts = [facts]
        answer = str(args.get("answer") or "")
        cited = [i for i in cited_ids(answer + "\n" + "\n".join(map(str, facts))) if ctx.evidence.get(i)]
        res = AgentResult(agent=self.name, task_id=task_id, task=task, answer=answer,
                          key_facts=[str(f) for f in facts], confidence=str(args.get("confidence") or "medium"),
                          gaps=str(args.get("gaps") or ""), evidence_ids=cited, seen_ids=seen, steps=steps,
                          tool_calls=n_calls)
        ctx.trace.emit(self.name, "agent_finish", task_id=task_id, answer=answer, key_facts=res.key_facts,
                       confidence=res.confidence, gaps=res.gaps, evidence=cited, steps=steps, tool_calls=n_calls)
        return res
