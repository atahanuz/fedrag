"""Synthesizer (final answer writer), direct answerer and verifier."""

from __future__ import annotations

from ..evidence import EvidenceStore, cited_ids
from ..llm import LLM
from . import prompts
from .base import AgentResult
from .planner import TASK_SCHEMA

MAX_EVIDENCE_ITEMS = 28
EVIDENCE_CHARS = 1800


def select_evidence(results: list[AgentResult], store: EvidenceStore) -> list[str]:
    """Evidence the writer sees: everything the agents cited, then their top unsighted results."""
    ids: list[str] = []
    for r in results:
        ids += [i for i in r.evidence_ids if i not in ids]
    for r in results:  # agents that cited little: add the first few items they looked at
        extra = [i for i in r.seen_ids if i not in ids][: max(0, 4 - len(r.evidence_ids))]
        ids += extra
    return [i for i in ids if store.get(i)][:MAX_EVIDENCE_ITEMS]


def _findings_block(results: list[AgentResult]) -> str:
    return "\n\n".join(r.render() for r in results) or "(no findings)"


async def synthesize(llm: LLM, question: str, today: str, results: list[AgentResult], store: EvidenceStore,
                     history_note: str = "", on_delta=None) -> str:
    ids = select_evidence(results, store)
    user = (f"{history_note}User question: {question}\n\n"
            f"## Findings from the research agents\n{_findings_block(results)}\n\n"
            f"## Evidence excerpts (cite by ID)\n{store.render(ids, EVIDENCE_CHARS) or '(none)'}\n\n"
            "Write the final answer now.")
    r = await llm.chat([{"role": "system", "content": prompts.SYNTHESIZER.format(today=today)},
                        {"role": "user", "content": user}],
                       agent="synthesizer", temperature=0.3, max_tokens=2500, on_delta=on_delta)
    return r.content


async def revise(llm: LLM, question: str, today: str, draft: str, issues: list[str], results: list[AgentResult],
                 store: EvidenceStore, on_delta=None) -> str:
    ids = list(dict.fromkeys(cited_ids(draft) + select_evidence(results, store)))[:MAX_EVIDENCE_ITEMS]
    user = (f"User question: {question}\n\n## Draft answer\n{draft}\n\n"
            "## Problems found by the fact-checker\n" + "\n".join(f"- {i}" for i in issues) +
            f"\n\n## Findings\n{_findings_block(results)}\n\n## Evidence excerpts (cite by ID)\n"
            f"{store.render(ids, EVIDENCE_CHARS)}\n\n"
            "Rewrite the answer fixing every problem. Keep what was correct. Output only the final answer.")
    r = await llm.chat([{"role": "system", "content": prompts.SYNTHESIZER.format(today=today)},
                        {"role": "user", "content": user}],
                       agent="synthesizer", temperature=0.2, max_tokens=2500, on_delta=on_delta)
    return r.content


async def direct_answer(llm: LLM, question: str, today: str, history_note: str = "", on_delta=None) -> str:
    r = await llm.chat([{"role": "system", "content": prompts.DIRECT.format(today=today)},
                        {"role": "user", "content": f"{history_note}{question}"}],
                       agent="direct", temperature=0.4, max_tokens=1500, on_delta=on_delta)
    return r.content


VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "issues": {"type": "array", "items": {"type": "string"}},
        "verdict": {"type": "string", "enum": ["accept", "revise", "research"]},
        "follow_up_tasks": {"type": "array", "items": TASK_SCHEMA},
    },
    "required": ["issues", "verdict", "follow_up_tasks"],
    "additionalProperties": False,
}


async def verify(llm: LLM, question: str, today: str, draft: str, store: EvidenceStore) -> dict:
    ids = cited_ids(draft)
    unknown = [i for i in ids if not store.get(i)]
    user = (f"User question: {question}\n\n## Draft answer\n{draft}\n\n"
            f"## Cited evidence\n{store.render([i for i in ids if store.get(i)], 3000) or '(none cited)'}\n\n"
            + (f"Note: the draft cites IDs that do not exist: {unknown}\n\n" if unknown else "")
            + "Return your verdict as JSON.")
    data = await llm.chat_json([{"role": "system", "content": prompts.VERIFIER.format(today=today)},
                                {"role": "user", "content": user}],
                               VERDICT_SCHEMA, agent="verifier", schema_name="verdict", temperature=0.1,
                               max_tokens=1500)
    data.setdefault("issues", [])
    data.setdefault("follow_up_tasks", [])
    if data.get("verdict") not in ("accept", "revise", "research"):
        data["verdict"] = "accept"
    return data
