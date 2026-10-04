"""Planner / router: decides which specialist agents to run and writes their tasks."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..llm import LLM
from . import prompts

AGENTS = ("fed_research", "data_analyst", "web_research", "market_data")

TASK_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "agent": {"type": "string", "enum": ["fed_research", "data_analyst", "web_research", "market_data"]},
        "instruction": {"type": "string"},
        "depends_on": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["id", "agent", "instruction", "depends_on"],
    "additionalProperties": False,
}

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "standalone_question": {"type": "string"},
        "reasoning": {"type": "string"},
        "intent": {"type": "string",
                   "enum": ["fed_documents", "live_data", "web", "general_knowledge", "mixed", "conversational"]},
        "needs_tools": {"type": "boolean"},
        "tasks": {"type": "array", "items": TASK_SCHEMA},
    },
    "required": ["standalone_question", "reasoning", "intent", "needs_tools", "tasks"],
    "additionalProperties": False,
}

EXAMPLES = """Examples (abridged):
Q: What did the Beige Book say about labor markets in the Dallas District in July 2026?
-> intent fed_documents; tasks: [fed_research: "In the July 2026 Beige Book (economic_conditions_report,
   2026-07-15), find what the Federal Reserve Bank of Dallas section says about labor markets: employment,
   hiring, wages."]
Q: How did the FOMC's view of inflation risks change between the March and July 2026 meetings?
-> intent fed_documents; tasks: [fed_research: "Minutes of the March 17-18, 2026 FOMC meeting: participants'
   assessment of inflation and inflation risks.", fed_research: "Minutes of the July 28-29, 2026 FOMC meeting:
   same."] (parallel)
Q: Did the Fed raise rates in September 2026, and did the July minutes foreshadow it?
-> intent mixed; tasks: [web_research: "FOMC decision at the September 2026 meeting (target range, vote,
   date)", fed_research: "July 2026 FOMC minutes: participants' views on the future path of policy, e.g.
   possible rate increases"]
Q: Which five banks had the lowest projected minimum CET1 ratios in the 2026 stress test, and how did those
   banks fare in 2025?
-> intent fed_documents; tasks: [data_analyst: "In the stress-test results table (DFAST 2013-2026), rank banks
   by projected minimum CET1 ratio under the 2026 severely adverse scenario (five lowest) and give the same
   banks' 2025 values."]
Q: How has the median FOMC projection for the end-2026 federal funds rate changed over the past year, and how
   did the Chair explain the September 2026 decision?
-> intent fed_documents; tasks: [data_analyst: "From the SEP Table 1 view across meetings, the median federal
   funds rate projection for 2026 at each meeting from September 2025 to September 2026.", fed_research:
   "September 16, 2026 FOMC statement and the Chair's press conference: the decision and its rationale."]
Q: What is the euro worth in dollars today and how does the fed funds rate compare with ECB rates?
-> intent mixed; tasks: [market_data: "Latest EUR/USD rate and the current fed funds target range
   (DFEDTARL/DFEDTARU)", web_research: "Current ECB key interest rates (deposit facility rate) and the date
   of the last change"]
Q: What is quantitative tightening?   -> intent general_knowledge; needs_tools false; tasks []
Q: thanks!                            -> intent conversational; needs_tools false; tasks []"""


@dataclass
class Task:
    id: str
    agent: str
    instruction: str
    depends_on: list[str] = field(default_factory=list)


@dataclass
class Plan:
    standalone_question: str
    reasoning: str
    intent: str
    needs_tools: bool
    tasks: list[Task]

    def as_dict(self) -> dict:
        return {"standalone_question": self.standalone_question, "reasoning": self.reasoning,
                "intent": self.intent, "needs_tools": self.needs_tools,
                "tasks": [t.__dict__ for t in self.tasks]}


def _history_block(history: list[dict] | None, max_turns: int = 3) -> str:
    if not history:
        return ""
    turns = history[-max_turns:]
    lines = []
    for t in turns:
        ans = re.sub(r"\[[DWM]\d+\]", "", t.get("answer", ""))[:700]
        lines.append(f"User: {t['question']}\nAssistant: {ans}")
    return "Conversation so far (most recent last):\n" + "\n\n".join(lines) + "\n\n"


async def make_plan(llm: LLM, question: str, today: str, corpus_card: str, history: list[dict] | None = None,
                    dataset_card: str = "", n_docs: int = 0, n_tables: int = 0) -> Plan:
    system = prompts.PLANNER.format(today=today, corpus_card=corpus_card, dataset_card=dataset_card, n_docs=n_docs,
                                    n_tables=n_tables) + "\n\n" + EXAMPLES
    user = f"{_history_block(history)}New user question: {question}\n\nReturn the plan as JSON."
    data = await llm.chat_json(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        PLAN_SCHEMA, agent="planner", schema_name="plan", temperature=0.2, max_tokens=1500,
    )
    tasks = []
    for i, t in enumerate(data.get("tasks") or []):
        if t.get("agent") not in AGENTS or not t.get("instruction"):
            continue
        tasks.append(Task(id=t.get("id") or f"t{i + 1}", agent=t["agent"], instruction=t["instruction"],
                          depends_on=[d for d in t.get("depends_on") or []]))
    tasks = tasks[:5]
    ids = {t.id for t in tasks}
    for t in tasks:
        t.depends_on = [d for d in t.depends_on if d in ids and d != t.id]
    needs = bool(data.get("needs_tools")) and bool(tasks)
    return Plan(standalone_question=data.get("standalone_question") or question,
                reasoning=data.get("reasoning", ""), intent=data.get("intent", "mixed"),
                needs_tools=needs, tasks=tasks if needs else [])
