"""Pipeline explorer: a web GUI that answers a question and shows the multi-agent pipeline as it runs.

    python -m fedrag.ui.app            # http://127.0.0.1:7860   (or: fedrag ui)

The page (``static/``) draws each run as a flow: the planner, the specialist agents it summons (parallel
agents side by side, with every tool call, SQL query and passage they retrieve), the writer, the
fact-checker and the cited answer, plus a timeline of which agent ran when. The server runs the
orchestrator and streams its trace as NDJSON, one JSON object per line:

    {"type": "start", "id": ...}
    {"type": "event", "t": 3.2, "agent": "fed_research", "kind": "tool_call", "data": {...}}
    {"type": "evidence", "items": [{"id": "D3", "title": ..., "source": ..., "url": ..., "text": ...}]}
    {"type": "done", "result": {...}}   or   {"type": "error", "error": "..."}

Every run is saved to ``data/ui_runs/`` and can be reopened or replayed, and so can the committed
evaluation runs (``eval/results/final_agentic.jsonl``): the explorer is useful while the GPU is off.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import re
import secrets
import time
from collections import Counter
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .. import config
from ..agents.specialists import build_specialists
from ..evidence import Evidence, EvidenceStore, cited_ids
from ..gpu_client import GPUClient
from ..llm import LLM
from ..orchestrator import Orchestrator, RunResult, evidence_url
from ..trace import Event

STATIC = Path(__file__).with_name("static")
RUNS_DIR = config.DATA_DIR / "ui_runs"
EVAL_RESULTS = config.ROOT / "eval" / "results" / "final_agentic.jsonl"
EVAL_QUESTIONS = config.ROOT / "eval" / "questions.jsonl"
RUN_ID = re.compile(r"^(\d{8}-\d{6}-[0-9a-f]{4}|eval-[a-z]{2}\d{2})$")
EVIDENCE_CHARS = 8000

EXAMPLES = [
    {"level": "simple", "tag": "SQL table",
     "question": "In the 2026 stress test, which bank had the lowest projected minimum CET1 capital ratio under "
                 "the severely adverse scenario?"},
    {"level": "simple", "tag": "Excel data",
     "question": "What was the median one-year-ahead inflation expectation in the New York Fed's Survey of "
                 "Consumer Expectations for August 2026?"},
    {"level": "simple", "tag": "Fed document",
     "question": "What does SR letter 26-2 do, and which earlier guidance does it replace?"},
    {"level": "simple", "tag": "Fed document",
     "question": "What did the FOMC decide at its July 2026 meeting, and who dissented?"},
    {"level": "simple", "tag": "Live data", "question": "What is the current EUR/USD exchange rate?"},
    {"level": "simple", "tag": "Web", "question": "Who is the current chair of the Federal Reserve?"},
    {"level": "simple", "tag": "No tools",
     "question": "What is the difference between quantitative easing and quantitative tightening?"},
    {"level": "complex", "tag": "SQL + documents",
     "question": "How did the median FOMC projection for the federal funds rate at the end of 2026 evolve across "
                 "the Summaries of Economic Projections since September 2025, and how did Chairman Warsh explain "
                 "the September 2026 decision?"},
    {"level": "complex", "tag": "Documents + live data",
     "question": "The May 2026 Financial Stability Report flagged an oil shock as a salient risk. What is the WTI "
                 "oil price now and how has it moved since May 2026?"},
    {"level": "complex", "tag": "Documents + live data",
     "question": "What did the September 2026 Beige Book say about prices, and how does that compare with the "
                 "latest CPI inflation data?"},
    {"level": "complex", "tag": "Web + documents",
     "question": "Did the Fed raise rates in September 2026, and did the July 2026 FOMC minutes foreshadow that "
                 "possibility?"},
    {"level": "complex", "tag": "SQL across years",
     "question": "Compare the aggregate decline in the CET1 capital ratio in the 2025 and 2026 stress tests, and "
                 "name the banks whose projected minimum fell below 10 percent in 2026."},
    {"level": "complex", "tag": "Cross-format",
     "question": "What median PCE inflation did FOMC participants project for 2026 in the September 2026 SEP, "
                 "and how did Chairman Warsh describe recent inflation at his press conference?"},
    {"level": "complex", "tag": "Comparison over time",
     "question": "How did the Beige Book's national summary of labor markets change between January 2026 and "
                 "September 2026?"},
    {"level": "complex", "tag": "Three sources",
     "question": "How does today's fed funds target range compare with the July 2026 decision, what did the "
                 "September 2026 SEP project for the end of 2026, and how has the 2-year Treasury yield moved "
                 "since July?"},
]

AGENT_LABELS = {"fed_research": "Fed document research", "data_analyst": "Data analyst (SQL)",
                "web_research": "Web research", "market_data": "Market & economic data"}
AGENT_ABOUT = {
    "fed_research": "Searches, lists and reads the collection's PDFs, web pages and Word files; cites pages.",
    "data_analyst": "Finds the right table among the collection's SQL tables, reads its schema and notes, "
                    "and answers with read-only SQL.",
    "web_research": "Searches the web and reads pages for news and anything outside the collection.",
    "market_data": "Live FRED series, ECB exchange rates and daily market prices.",
}


class RunRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    verify: bool = True
    thinking: bool = False
    history: list[dict] = Field(default_factory=list)  # earlier turns [{question, answer}] for a follow-up


def evidence_json(ev: Evidence, index=None) -> dict:
    return {"id": ev.id, "kind": ev.kind, "title": ev.title, "source": ev.source, "url": evidence_url(ev, index),
            "text": ev.text[:EVIDENCE_CHARS], "chars": len(ev.text), "meta": ev.meta}


def result_json(res: RunResult) -> dict:
    return {"answer": res.answer, "sources": res.sources, "plan": res.plan, "verifications": res.verifications,
            "usage": res.usage, "seconds": res.seconds, "agents_used": res.agents_used}


_END = object()


def _line(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str) + "\n"


def _merge_deltas(msgs: list[dict]) -> list[dict]:
    """Join consecutive streamed-token events of one agent (fewer, larger lines)."""
    out: list[dict] = []
    for m in msgs:
        prev = out[-1] if out else None
        if prev and m.get("kind") == "delta" and prev.get("kind") == "delta" and m["agent"] == prev["agent"]:
            out[-1] = {**prev, "data": {"text": prev["data"]["text"] + m["data"]["text"]}}
        else:
            out.append(m)
    return out


def _specialists(agents: list[str] | None) -> list[str]:
    return [a for a in agents or [] if a in AGENT_LABELS or a == "direct"]


def _dotenv_gateway() -> tuple[str, str] | None:
    """FEDRAG_GPU_URL / FEDRAG_API_KEY as scripts/colab_up.py last wrote them (the tunnel URL changes)."""
    path = config.ROOT / ".env"
    if not path.exists():
        return None
    vals = {}
    for line in path.read_text().splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and not key.startswith("#"):
            vals[key.strip()] = value.strip().strip('"').strip("'")
    url = vals.get("FEDRAG_GPU_URL", "").rstrip("/")
    return (url, vals.get("FEDRAG_API_KEY", "")) if url else None


class Backend:
    """The orchestrator, pointed at whatever gateway .env names when a run starts or the status is polled."""

    def __init__(self, orch: Orchestrator | None = None, follow_dotenv: bool = True):
        self.orch = orch or Orchestrator()
        self.follow_dotenv = follow_dotenv
        # the LLM follows the gateway unless FEDRAG_LLM_BASE_URL points it elsewhere
        self.llm_on_gateway = config.LLM_BASE_URL in ("", f"{config.GPU_URL}/v1")
        self._health: tuple[float, dict] | None = None
        self.active: dict[str, asyncio.Task] = {}

    async def refresh(self) -> None:
        gw = _dotenv_gateway() if self.follow_dotenv else None
        if not gw or (gw[0], gw[1]) == (self.orch.gpu.base_url, self.orch.gpu.api_key):
            return
        old = self.orch.gpu
        self.orch.gpu = self.orch.index.gpu = GPUClient(base_url=gw[0], api_key=gw[1])
        if self.llm_on_gateway:
            self.orch.llm = LLM(base_url=f"{gw[0]}/v1", api_key=gw[1])
        self._health = None
        await old.aclose()

    async def health(self, fresh: bool = False) -> dict:
        if not fresh and self._health and time.time() - self._health[0] < 10:
            return self._health[1]
        await self.refresh()
        gpu = self.orch.gpu
        if not gpu.base_url:
            out = {"status": "unconfigured", "detail": "no GPU gateway configured"}
        else:
            try:
                h = await asyncio.wait_for(gpu.health(), 8)
                out = {"status": "online" if h.get("llm_ready") else "starting", "detail": h}
            except Exception as e:  # tunnel gone, VM released, ...
                out = {"status": "offline", "detail": f"{type(e).__name__}: {e}"[:300]}
        out["model"] = self.orch.llm.model
        self._health = (time.time(), out)
        return out


class RunStore:
    """Runs made in the explorer (data/ui_runs/*.json) and the committed evaluation runs, in one format."""

    def __init__(self, root: Path = RUNS_DIR, eval_results: Path = EVAL_RESULTS,
                 eval_questions: Path = EVAL_QUESTIONS):
        self.root = root
        self.eval_results, self.eval_questions = eval_results, eval_questions
        self._summaries: dict[str, dict] | None = None
        self._eval: dict[str, dict] | None = None

    @staticmethod
    def summary(run: dict) -> dict:
        res = run.get("result") or {}
        agents = _specialists(res.get("agents_used")) or _specialists(
            [t.get("agent") for t in (res.get("plan") or {}).get("tasks", [])])
        return {"id": run["id"], "source": run.get("source", "ui"), "question": run["question"],
                "created": run.get("created", ""), "status": run.get("status", "done"),
                "seconds": res.get("seconds") or run.get("seconds"), "agents": agents,
                "n_sources": len(res.get("sources") or []), "score": run.get("eval", {}).get("score"),
                "category": run.get("eval", {}).get("category")}

    def _load_summaries(self) -> dict[str, dict]:
        if self._summaries is None:
            self._summaries = {}
            for p in sorted(self.root.glob("*.json")) if self.root.exists() else []:
                try:
                    run = json.loads(p.read_text())
                    self._summaries[run["id"]] = self.summary(run)
                except (OSError, ValueError, KeyError):
                    continue
        return self._summaries

    def list(self) -> list[dict]:
        return sorted(self._load_summaries().values(), key=lambda s: s["created"], reverse=True)

    def save(self, run: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.root / f".{run['id']}.tmp"
        tmp.write_text(json.dumps(run, ensure_ascii=False, default=str))
        tmp.replace(self.root / f"{run['id']}.json")
        self._load_summaries()[run["id"]] = self.summary(run)

    def get(self, run_id: str) -> dict | None:
        if run_id.startswith("eval-"):
            return self._load_eval().get(run_id)
        p = self.root / f"{run_id}.json"
        return json.loads(p.read_text()) if p.exists() else None

    def delete(self, run_id: str) -> bool:
        p = self.root / f"{run_id}.json"
        if run_id.startswith("eval-") or not p.exists():
            return False
        p.unlink()
        self._load_summaries().pop(run_id, None)
        return True

    def _load_eval(self) -> dict[str, dict]:
        if self._eval is None:
            self._eval = {}
            if not self.eval_results.exists():
                return self._eval
            refs = {}
            if self.eval_questions.exists():
                refs = {q["id"]: q for q in map(json.loads, self.eval_questions.read_text().splitlines()) if q}
            created = dt.datetime.fromtimestamp(self.eval_results.stat().st_mtime).isoformat(timespec="seconds")
            for line in self.eval_results.read_text().splitlines():
                r = json.loads(line)
                # the eval file keeps cited sources (no passage text) and verdicts only; the trace has the rest
                sources = [{**s, "title": s.get("source", ""), "excerpt": ""} for s in r.get("sources", [])]
                verifs = [e["data"] for e in r.get("trace", []) if e["type"] == "verification"]
                self._eval[f"eval-{r['id']}"] = {
                    "id": f"eval-{r['id']}", "source": "eval", "question": r["question"], "created": created,
                    "status": "error" if r.get("error") else "done", "options": {"verify": True},
                    "events": r.get("trace", []),
                    "evidence": {s["id"]: {"id": s["id"], "kind": s["kind"], "title": s["source"],
                                           "source": s["source"], "url": s.get("url"), "text": "", "meta": {}}
                                 for s in sources},
                    "result": {"answer": r.get("answer", ""), "sources": sources, "plan": r.get("plan"),
                               "verifications": verifs, "usage": r.get("usage"), "seconds": r.get("seconds"),
                               "agents_used": r.get("agents_used")},
                    "error": r.get("error"),
                    "eval": {"score": r.get("score"), "judge": r.get("judge"), "category": r.get("category"),
                             "routing_ok": r.get("routing_ok"), "reference": refs.get(r["id"], {}).get("reference"),
                             "expected_agents": refs.get(r["id"], {}).get("expected_agents")},
                }
        return self._eval

    def list_eval(self) -> list[dict]:
        return [self.summary(r) for r in self._load_eval().values()]


def create_app(backend: Backend | None = None, runs: RunStore | None = None, allowed_hosts: list[str] | None = None
               ) -> FastAPI:
    app = FastAPI(title="FedRAG pipeline explorer", docs_url=None, redoc_url=None)
    if allowed_hosts:  # a page on another site cannot reach this server through DNS rebinding
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)
    backend = backend or Backend()
    runs = runs or RunStore()
    app.state.backend, app.state.runs = backend, runs
    info_cache: dict = {}

    def guard(request: Request) -> None:
        # a custom header forces a CORS preflight, so other sites cannot start runs from a visitor's browser
        if request.headers.get("x-fedrag") != "1":
            raise HTTPException(403, "missing X-FedRAG header")

    @app.get("/")
    async def index() -> HTMLResponse:
        # the version query makes browsers fetch app.js / app.css again after they change
        version = int(max((STATIC / f).stat().st_mtime for f in ("app.js", "app.css")))
        html = (STATIC / "index.html").read_text().replace("__V__", str(version))
        return HTMLResponse(html, headers={"Cache-Control": "no-cache"})

    @app.get("/api/info")
    async def info() -> dict:
        if not info_cache:
            idx = backend.orch.index
            tools = {name: list(agent.tools) for name, agent in
                     build_specialists(idx, dt.date.today().strftime("%A, %B %d, %Y")).items()}
            n_tables = sum(1 for t in idx.tables.catalog.values() if t["kind"] == "table")
            info_cache.update({
                "docs": len(idx.docs), "chunks": len(idx.chunks), "tables": n_tables,
                "views": len(idx.tables.catalog) - n_tables,
                "formats": dict(Counter(d.get("format", "pdf") for d in idx.docs.values()).most_common()),
                "examples": EXAMPLES,
                "agents": [{"name": n, "label": AGENT_LABELS[n], "about": AGENT_ABOUT[n], "tools": tools[n]}
                           for n in AGENT_LABELS],
            })
        return {**info_cache, "model": backend.orch.llm.model, "today": dt.date.today().isoformat()}

    @app.get("/api/health")
    async def health() -> dict:
        return await backend.health()

    @app.get("/api/runs")
    async def list_runs() -> dict:
        return {"runs": runs.list(), "eval": runs.list_eval()}

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str) -> dict:
        run = runs.get(run_id) if RUN_ID.match(run_id) else None
        if run is None:
            raise HTTPException(404, "no such run")
        return run

    @app.delete("/api/runs/{run_id}")
    async def delete_run(run_id: str, request: Request) -> dict:
        guard(request)
        if not RUN_ID.match(run_id) or not runs.delete(run_id):
            raise HTTPException(404, "no such run")
        return {"ok": True}

    @app.post("/api/runs/{run_id}/cancel")
    async def cancel_run(run_id: str, request: Request) -> dict:
        guard(request)
        task = backend.active.get(run_id)
        if task is None:
            raise HTTPException(404, "run is not active")
        task.cancel()
        return {"ok": True}

    @app.post("/api/run")
    async def run(req: RunRequest, request: Request) -> StreamingResponse:
        guard(request)
        question = req.question.strip()
        if not question:
            raise HTTPException(400, "empty question")
        h = await backend.health(fresh=True)
        if h["status"] in ("offline", "unconfigured"):
            raise HTTPException(503, f"GPU backend {h['status']}: {h['detail']}")
        history = [{"question": str(t.get("question", ""))[:4000], "answer": str(t.get("answer", ""))[:8000]}
                   for t in req.history[-3:] if isinstance(t, dict)]
        run_id = dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
        rec = {"id": run_id, "source": "ui", "question": question,
               "created": dt.datetime.now().isoformat(timespec="seconds"),
               "options": {"verify": req.verify, "thinking": req.thinking, "followup": bool(history)},
               "history": history, "status": "running", "events": [], "evidence": {}, "result": None,
               "error": None}
        store = EvidenceStore()
        index = backend.orch.index
        queue: asyncio.Queue = asyncio.Queue()
        sent: dict[str, int] = {}  # evidence id -> length of the text sent (a snippet can grow into a page)

        def listener(ev: Event) -> None:
            item = {"t": ev.t, "agent": ev.agent, "type": ev.type, "data": ev.data}
            if ev.type != "delta":
                rec["events"].append(item)
            queue.put_nowait({"type": "event", "t": ev.t, "agent": ev.agent, "kind": ev.type, "data": ev.data})
            if ev.type == "tool_output":
                fresh = [store.get(i) for i in cited_ids(ev.data.get("text", ""))]
                fresh = [e for e in fresh if e is not None and sent.get(e.id) != len(e.text)]
                if fresh:
                    sent.update({e.id: len(e.text) for e in fresh})
                    queue.put_nowait({"type": "evidence", "items": [evidence_json(e, index) for e in fresh]})

        task = asyncio.create_task(backend.orch.run(question, history=history or None, listeners=[listener],
                                                    verify=req.verify, thinking=req.thinking, evidence=store))
        task.add_done_callback(lambda _: queue.put_nowait(_END))  # queued after every event of the run
        backend.active[run_id] = task

        async def stream():
            try:
                yield _line({"type": "start", "id": run_id, "created": rec["created"]})
                finished = False
                while not finished:
                    try:
                        batch = [await asyncio.wait_for(queue.get(), timeout=5)]
                    except asyncio.TimeoutError:
                        yield _line({"type": "ping"})  # writing also notices a closed connection
                        continue
                    while not queue.empty():
                        batch.append(queue.get_nowait())
                    finished = batch[-1] is _END
                    msgs = _merge_deltas([m for m in batch if m is not _END])
                    if msgs:
                        yield "".join(_line(m) for m in msgs)
                try:
                    rec.update(status="done", result=result_json(task.result()))
                    yield _line({"type": "done", "id": run_id, "result": rec["result"]})
                except asyncio.CancelledError:
                    rec.update(status="cancelled", error="stopped by the user")
                    yield _line({"type": "error", "id": run_id, "error": rec["error"], "cancelled": True})
                except Exception as e:
                    rec.update(status="error", error=f"{type(e).__name__}: {e}")
                    yield _line({"type": "error", "id": run_id, "error": rec["error"]})
            finally:
                backend.active.pop(run_id, None)
                if not task.done():  # the page went away mid-run: stop spending GPU time on it
                    task.cancel()
                    rec.update(status="cancelled", error="connection closed")
                rec["evidence"] = {i: evidence_json(e, index) for i, e in store.items.items()}
                rec["seconds"] = rec["events"][-1]["t"] if rec["events"] else 0
                runs.save(rec)

        return StreamingResponse(stream(), media_type="application/x-ndjson",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


def main(argv: list[str] | None = None) -> None:
    import uvicorn

    ap = argparse.ArgumentParser(description="FedRAG pipeline explorer (web GUI)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=7860)
    args = ap.parse_args(argv)
    loopback = args.host in ("127.0.0.1", "localhost", "::1")
    app = create_app(allowed_hosts=["127.0.0.1", "localhost"] if loopback else None)
    print(f"FedRAG pipeline explorer on http://{'127.0.0.1' if loopback else args.host}:{args.port}", flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
