"""The pipeline explorer's server: streaming a run, saving it, reopening it (no GPU, no network)."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")  # the explorer is an optional extra: pip install -e ".[ui]"

from fastapi.testclient import TestClient  # noqa: E402

from fedrag.gpu_client import GPUClient  # noqa: E402
from fedrag.orchestrator import RunResult  # noqa: E402
from fedrag.trace import Trace  # noqa: E402
from fedrag.ui import app as ui  # noqa: E402

DOC = {"doc_id": "fed_beige_book_2026-09", "title": "Beige Book, September 2026", "doc_type": "economic_conditions_report",
       "date": "2026-09-02", "url": "https://www.federalreserve.gov/beigebook.pdf", "format": "pdf", "page_unit": "page"}


class FakeIndex:
    def __init__(self):
        self.docs = {DOC["doc_id"]: DOC}
        self.chunks = []
        self.tables = SimpleNamespace(catalog={})
        self.gpu = None

    def corpus_card(self) -> str:
        return ""

    def dataset_card(self) -> str:
        return ""


class FakeGPU:
    def __init__(self, status: str = "online"):
        self.base_url, self.api_key, self.status = "http://gpu", "k", status

    async def health(self) -> dict:
        if self.status == "offline":
            raise ConnectionError("tunnel gone")
        return {"ok": True, "llm_ready": True, "idle_seconds": 3}

    async def aclose(self) -> None:
        pass


class FakeOrchestrator:
    """Emits the events of a one-agent run, registering evidence the way the tools do."""

    def __init__(self, gpu_status: str = "online"):
        self.gpu, self.index, self.llm = FakeGPU(gpu_status), FakeIndex(), SimpleNamespace(model="fake-llm")
        self.calls: list[dict] = []

    async def run(self, question, history=None, listeners=None, verify=True, thinking=False, evidence=None, **kw):
        self.calls.append({"question": question, "history": history, "verify": verify, "thinking": thinking})
        tr = Trace()
        tr.listeners.extend(listeners or [])
        tr.emit("planner", "agent_start", task="route and decompose the question")
        tr.emit("planner", "plan", standalone_question=question, reasoning="Beige Book question", intent="fed_documents",
                needs_tools=True, tasks=[{"id": "t1", "agent": "fed_research", "instruction": "Read it",
                                          "depends_on": []}])
        tr.emit("fed_research", "agent_start", task_id="t1", task="Read it")
        ids = {"task_id": "t1", "step": 1, "call_id": "c1"}
        tr.emit("fed_research", "tool_call", **ids, tool="read_document_pages", args={"doc_id": DOC["doc_id"],
                                                                                       "start_page": 6})
        ev = evidence.add_page(DOC, 6, "Prices increased moderately in most Districts.")
        out = f"[{ev.id}] {DOC['title']}, p. 6\nPrices increased moderately in most Districts."
        tr.emit("fed_research", "tool_result", **ids, tool="read_document_pages", ids=[ev.id], preview=out[:300],
                chars=len(out))
        tr.transient("fed_research", "tool_output", **ids, text=out)
        tr.emit("fed_research", "agent_finish", task_id="t1", answer=f"Moderate increases [{ev.id}].", key_facts=[],
                confidence="high", gaps="", evidence=[ev.id], steps=2, tool_calls=1)
        tr.emit("synthesizer", "agent_start", task="write the cited answer")
        for part in ("Prices rose ", "moderately ", f"[{ev.id}]."):
            tr.stream("synthesizer", part)
            await asyncio.sleep(0)
        answer = f"Prices rose moderately [{ev.id}]."
        tr.emit("synthesizer", "synthesis", answer=answer)
        tr.emit("orchestrator", "final", answer=answer, sources=[ev.id])
        sources = [{"id": ev.id, "kind": "doc", "title": ev.title, "source": ev.source, "url": DOC["url"] + "#page=6",
                    "excerpt": ev.text, "meta": ev.meta}]
        return RunResult(question=question, answer=answer, sources=sources, plan={}, agent_results=[],
                         verifications=[], trace=tr.to_list(), usage={"llm_calls": 3, "tool_calls": {}},
                         seconds=0.1, agents_used=["planner", "fed_research", "synthesizer"])


def eval_file(tmp_path):
    path = tmp_path / "final_agentic.jsonl"
    rec = {"id": "fd01", "category": "fed_docs", "question": "What did the FOMC decide?", "answer": "It held [D1].",
           "agents_used": ["planner", "fed_research", "synthesizer", "verifier"], "plan": {"tasks": []},
           "sources": [{"id": "D1", "kind": "doc", "source": "FOMC statement, part 1", "url": "https://x"}],
           "verifications": ["accept"], "usage": {}, "seconds": 9.5, "error": None, "score": 2, "judge": "ok",
           "routing_ok": True,
           "trace": [{"t": 0.0, "agent": "planner", "type": "agent_start", "data": {}},
                     {"t": 1.0, "agent": "verifier", "type": "verification",
                      "data": {"verdict": "accept", "issues": [], "follow_up_tasks": []}}]}
    path.write_text(json.dumps(rec) + "\n")
    questions = tmp_path / "questions.jsonl"
    questions.write_text(json.dumps({"id": "fd01", "reference": "Held at 3.5-3.75", "expected_agents": []}) + "\n")
    return path, questions


def client(tmp_path, gpu_status="online"):
    orch = FakeOrchestrator(gpu_status)
    results, questions = eval_file(tmp_path)
    runs = ui.RunStore(root=tmp_path / "runs", eval_results=results, eval_questions=questions)
    return TestClient(ui.create_app(ui.Backend(orch, follow_dotenv=False), runs)), orch, runs


def stream(c: TestClient, body: dict, headers=None) -> list[dict]:
    r = c.post("/api/run", json=body, headers={"X-FedRAG": "1"} if headers is None else headers)
    assert r.status_code == 200, r.text
    return [json.loads(line) for line in r.text.splitlines() if line.strip()]


def test_run_streams_events_evidence_and_result(tmp_path):
    c, orch, runs = client(tmp_path)
    msgs = stream(c, {"question": "What did the Beige Book say about prices?", "verify": False, "thinking": True})
    assert msgs[0]["type"] == "start" and msgs[-1]["type"] == "done"
    kinds = [m["kind"] for m in msgs if m["type"] == "event"]
    assert kinds[:3] == ["agent_start", "plan", "agent_start"]
    assert "tool_output" in kinds and "delta" in kinds
    # every passage a tool returns reaches the page, with a link to its PDF page
    ev = [i for m in msgs if m["type"] == "evidence" for i in m["items"]]
    assert ev[0]["id"] == "D1" and ev[0]["url"].endswith("#page=6") and "moderately" in ev[0]["text"]
    assert msgs[-1]["result"]["answer"] == "Prices rose moderately [D1]."
    assert orch.calls[0]["verify"] is False and orch.calls[0]["thinking"] is True

    # the run is saved: trace with the full tool outputs, no token deltas, and every passage
    saved = c.get(f"/api/runs/{msgs[0]['id']}").json()
    assert saved["status"] == "done" and saved["options"]["thinking"] is True
    types = [e["type"] for e in saved["events"]]
    assert "tool_output" in types and "delta" not in types
    assert saved["evidence"]["D1"]["source"] == "Beige Book, September 2026, p. 6"
    listed = c.get("/api/runs").json()
    assert listed["runs"][0]["id"] == msgs[0]["id"] and listed["runs"][0]["agents"] == ["fed_research"]


def test_streamed_deltas_are_merged():
    msgs = [{"type": "event", "kind": "delta", "agent": "synthesizer", "data": {"text": "a"}},
            {"type": "event", "kind": "delta", "agent": "synthesizer", "data": {"text": "b"}},
            {"type": "evidence", "items": []},
            {"type": "event", "kind": "delta", "agent": "synthesizer", "data": {"text": "c"}}]
    out = ui._merge_deltas(msgs)
    assert [m.get("data", {}).get("text") for m in out] == ["ab", None, "c"]


def test_follow_up_history_is_passed_on(tmp_path):
    c, orch, _ = client(tmp_path)
    stream(c, {"question": "And in Dallas?", "history": [{"question": "Q1", "answer": "A1"}]})
    assert orch.calls[0]["history"] == [{"question": "Q1", "answer": "A1"}]


def test_requests_need_the_custom_header(tmp_path):
    c, orch, _ = client(tmp_path)
    assert c.post("/api/run", json={"question": "x"}).status_code == 403
    assert c.delete("/api/runs/20261005-120000-abcd").status_code == 403
    assert not orch.calls


def test_offline_backend_is_reported_before_running(tmp_path):
    c, orch, _ = client(tmp_path, gpu_status="offline")
    r = c.post("/api/run", json={"question": "x"}, headers={"X-FedRAG": "1"})
    assert r.status_code == 503 and "offline" in r.json()["detail"]
    assert c.get("/api/health").json()["status"] == "offline"
    assert not orch.calls


def test_eval_runs_open_like_saved_runs(tmp_path):
    c, _, _ = client(tmp_path)
    listed = c.get("/api/runs").json()["eval"]
    assert listed[0]["id"] == "eval-fd01" and listed[0]["score"] == 2
    run = c.get("/api/runs/eval-fd01").json()
    assert run["eval"]["reference"] == "Held at 3.5-3.75"
    assert run["result"]["verifications"][0]["verdict"] == "accept"
    assert run["evidence"]["D1"]["source"] == "FOMC statement, part 1"
    assert c.get("/api/runs/..%2F..%2Fetc").status_code == 404
    assert c.delete("/api/runs/eval-fd01", headers={"X-FedRAG": "1"}).status_code == 404


def test_saved_runs_can_be_deleted(tmp_path):
    c, _, runs = client(tmp_path)
    run_id = stream(c, {"question": "x"})[0]["id"]
    assert c.delete(f"/api/runs/{run_id}", headers={"X-FedRAG": "1"}).json() == {"ok": True}
    assert c.get(f"/api/runs/{run_id}").status_code == 404 and runs.list() == []


def test_info_lists_examples_and_agent_tools(tmp_path):
    c, _, _ = client(tmp_path)
    info = c.get("/api/info").json()
    assert {e["level"] for e in info["examples"]} == {"simple", "complex"}
    tools = {a["name"]: a["tools"] for a in info["agents"]}
    assert "query_data" in tools["data_analyst"] and "web_search" in tools["web_research"]
    assert c.get("/").status_code == 200 and c.get("/static/app.js").status_code == 200


def test_backend_follows_a_new_gateway_url(tmp_path, monkeypatch):
    monkeypatch.setattr(ui.config, "ROOT", tmp_path)
    orch = FakeOrchestrator()
    orch.gpu = orch.index.gpu = GPUClient(base_url="http://old", api_key="k1")
    backend = ui.Backend(orch)
    backend.llm_on_gateway = True
    (tmp_path / ".env").write_text("FEDRAG_GPU_URL=https://new.trycloudflare.com\nFEDRAG_API_KEY=k2\n")
    asyncio.run(backend.refresh())
    assert orch.gpu.base_url == "https://new.trycloudflare.com" and orch.gpu.api_key == "k2"
    assert orch.index.gpu is orch.gpu
    assert str(orch.llm.client.base_url).rstrip("/") == "https://new.trycloudflare.com/v1"


def test_transient_events_reach_listeners_but_not_the_trace():
    tr = Trace()
    seen = []
    tr.listeners.append(seen.append)
    tr.emit("a", "tool_result", ids=[])
    tr.transient("a", "tool_output", text="x" * 10)
    tr.stream("a", "tok")
    assert [e.type for e in seen] == ["tool_result", "tool_output", "delta"]
    assert [e["type"] for e in tr.to_list()] == ["tool_result"]
