"""Reading a set of documents, diversified search and planner interpretations (no GPU: BM25 only)."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from types import SimpleNamespace

import pytest

from fedrag.agents.planner import make_plan
from fedrag.config import CORPUS_DIR

HAVE_CORPUS = (CORPUS_DIR / "chunks.jsonl").exists()


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(scope="module")
def ctx():
    if not HAVE_CORPUS:
        pytest.skip("corpus not built")
    from fedrag.retrieval.index import CorpusIndex
    from fedrag.tools.base import RunContext

    return RunContext(index=CorpusIndex(gpu=None), llm=None, today=dt.date(2026, 10, 6))


def test_each_document_of_a_set_gets_its_own_passages(ctx):
    from fedrag.tools.fed_docs import search_each_document

    out = run(search_each_document(ctx, "overall economic activity national summary",
                                   doc_type="economic_conditions_report", date_from="2024", date_to="2024", per_doc=1))
    editions = [line for line in out.splitlines() if line.startswith("### Beige Book")]
    assert len(editions) == 8 and "(no matching passage)" not in out
    assert editions[0].endswith("doc_id=fed_beige_book_2024-01-17") and "2024-12-04" in editions[-1]
    assert ctx.evidence.get("D1").meta["doc_id"] == "fed_beige_book_2024-01-17"


def test_each_document_needs_a_named_set_and_keeps_the_most_recent(ctx):
    from fedrag.tools.fed_docs import search_each_document

    assert run(search_each_document(ctx, "inflation")).startswith("ERROR")
    out = run(search_each_document(ctx, "inflation", doc_type="fomc_statement", date_from="2023", max_documents=3,
                                   per_doc=1))
    assert out.count("\n### ") == 3 and "fed_fomc_statement_2026-09-16" in out and "older matching document" in out


def test_search_results_spread_over_documents(ctx):
    from fedrag.tools.fed_docs import search_fed_documents

    out = run(search_fed_documents(ctx, "stablecoin reserve assets redemption", doc_types=["speech"], top_k=8))
    docs = [line.split(" | ")[0].split("] ", 1)[1] for line in out.splitlines() if line.startswith("[D")]
    assert len(docs) >= 4 and max(docs.count(d) for d in docs) <= 2


class PlanLLM:
    def __init__(self, plan: dict):
        self.plan = plan
        self.seen: list = []

    async def chat_json(self, messages, schema, **kw):
        self.seen.append(messages)
        return self.plan


def test_planner_keeps_its_interpretation_and_sees_related_documents():
    plan = {"standalone_question": "Who dissented?", "reasoning": "no context", "intent": "fed_documents",
            "assumptions": ["'Who dissented?' = the latest FOMC meeting, September 15-16, 2026", " "],
            "needs_tools": True, "tasks": [{"id": "t1", "agent": "fed_research", "instruction": "September 2026 "
                                            "statement: vote and dissents", "depends_on": []}]}
    llm = PlanLLM(plan)
    p = run(make_plan(llm, "Who dissented?", "Tuesday, October 06, 2026", "card",
                      related="- fomc_statement | 2026-09-16 | FOMC statement, September 16, 2026 | best match: -"))
    assert p.assumptions == ["'Who dissented?' = the latest FOMC meeting, September 15-16, 2026"]
    assert p.as_dict()["assumptions"] == p.assumptions
    assert "FOMC statement, September 16, 2026" in llm.seen[0][1]["content"]


def test_interpretation_reaches_the_writer(monkeypatch):
    from fedrag import orchestrator as orch_mod

    captured = {}

    async def fake_plan(*a, **kw):
        from fedrag.agents.planner import Plan, Task
        return Plan(standalone_question=a[1], reasoning="", intent="fed_documents", needs_tools=True,
                    tasks=[Task(id="t1", agent="fed_research", instruction="x")],
                    assumptions=["'last meeting' = September 15-16, 2026"])

    async def fake_execute(self, ctx, tasks, specialists):
        return []

    async def fake_synth(llm, question, today, results, store, history_note="", on_delta=None):
        captured["note"] = history_note
        return "Taking the last meeting to be September 15-16, 2026: ..."

    monkeypatch.setattr(orch_mod, "make_plan", fake_plan)
    monkeypatch.setattr(orch_mod.Orchestrator, "_execute", fake_execute)
    monkeypatch.setattr(orch_mod, "synthesize", fake_synth)
    monkeypatch.setattr(orch_mod, "build_specialists", lambda *a, **kw: {})
    index = SimpleNamespace(docs={}, tables=SimpleNamespace(catalog={}), corpus_card=lambda: "",
                            dataset_card=lambda: "", related_docs=None)
    o = orch_mod.Orchestrator(index=index, llm=SimpleNamespace(), gpu=SimpleNamespace(), verify=False)
    res = run(o.run("What did the Fed decide at its last meeting?"))
    assert "Interpretation of the ambiguous question: 'last meeting' = September 15-16, 2026" in captured["note"]
    plan_event = next(e for e in res.trace if e["type"] == "plan")
    assert plan_event["data"]["assumptions"] == ["'last meeting' = September 15-16, 2026"]
    assert json.dumps(res.to_dict())  # serializable for the explorer and the eval files
