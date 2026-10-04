"""Single authenticated HTTP entry point for all GPU models.

* ``POST /v1/chat/completions``, ``GET /v1/models``  -> reverse proxy to vLLM (LLM)
* ``POST /v1/embeddings``                          -> Qwen3-Embedding (OpenAI-compatible)
* ``POST /v1/rerank``                              -> Qwen3-Reranker
* ``GET  /health``                                 -> liveness, no auth

Every route except ``/health`` requires ``Authorization: Bearer $FEDRAG_API_KEY``.
Run with ``uvicorn gateway:app --host 0.0.0.0 --port 8000``.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).parent))
from models import EMBED_MODEL, RERANK_MODEL, Embedder, Reranker  # noqa: E402

API_KEY = os.environ.get("FEDRAG_API_KEY", "")
VLLM_URL = os.environ.get("FEDRAG_VLLM_URL", "http://127.0.0.1:8001")

app = FastAPI(title="fedrag GPU gateway")
state: dict = {"last_request": time.time()}


@app.on_event("startup")
async def _load() -> None:
    t = time.time()
    state["embedder"] = Embedder()
    state["reranker"] = Reranker()
    state["http"] = httpx.AsyncClient(base_url=VLLM_URL, timeout=httpx.Timeout(600.0, connect=10.0))
    print(f"models loaded in {time.time() - t:.0f}s", flush=True)


def auth(request: Request) -> None:
    if API_KEY and request.headers.get("authorization", "") != f"Bearer {API_KEY}":
        raise HTTPException(status_code=401, detail="invalid or missing API key")
    state["last_request"] = time.time()  # drives the idle shutdown of the keep-alive loop


@app.get("/health")
async def health() -> dict:
    llm_ok = False
    try:
        r = await state["http"].get("/health", timeout=3.0)
        llm_ok = r.status_code == 200
    except Exception:
        pass
    return {"ok": True, "embedder": EMBED_MODEL, "reranker": RERANK_MODEL, "llm_ready": llm_ok,
            "idle_seconds": round(time.time() - state["last_request"])}


# ---------------------------------------------------------------------------
# Embeddings / rerank
# ---------------------------------------------------------------------------


class EmbedRequest(BaseModel):
    input: str | list[str]
    model: str | None = None
    input_type: str = "query"  # "query" (instruction-prefixed) or "document"
    instruction: str | None = None


class RerankRequest(BaseModel):
    query: str
    documents: list[str]
    instruction: str | None = None
    top_n: int | None = None


@app.post("/v1/embeddings", dependencies=[Depends(auth)])
async def embeddings(req: EmbedRequest) -> dict:
    texts = [req.input] if isinstance(req.input, str) else req.input
    if not texts:
        raise HTTPException(400, "empty input")
    vecs = await asyncio.to_thread(
        state["embedder"].embed, texts, req.input_type == "query", req.instruction
    )
    return {
        "object": "list",
        "model": EMBED_MODEL,
        "data": [{"object": "embedding", "index": i, "embedding": v.tolist()} for i, v in enumerate(vecs)],
    }


@app.post("/v1/rerank", dependencies=[Depends(auth)])
async def rerank(req: RerankRequest) -> dict:
    if not req.documents:
        return {"model": RERANK_MODEL, "results": []}
    scores = await asyncio.to_thread(state["reranker"].score, req.query, req.documents, req.instruction)
    results = sorted(
        ({"index": i, "relevance_score": s} for i, s in enumerate(scores)),
        key=lambda r: r["relevance_score"],
        reverse=True,
    )
    if req.top_n:
        results = results[: req.top_n]
    return {"model": RERANK_MODEL, "results": results}


# ---------------------------------------------------------------------------
# LLM reverse proxy (supports streaming)
# ---------------------------------------------------------------------------


async def _proxy(request: Request, path: str) -> Response:
    client: httpx.AsyncClient = state["http"]
    body = await request.body()
    headers = {"content-type": request.headers.get("content-type", "application/json")}
    wants_stream = b'"stream": true' in body or b'"stream":true' in body
    if not wants_stream:
        r = await client.request(request.method, path, content=body, headers=headers)
        return Response(content=r.content, status_code=r.status_code,
                        media_type=r.headers.get("content-type", "application/json"))

    req = client.build_request(request.method, path, content=body, headers=headers)
    r = await client.send(req, stream=True)

    async def gen():
        try:
            async for chunk in r.aiter_raw():
                yield chunk
        finally:
            await r.aclose()

    return StreamingResponse(gen(), status_code=r.status_code,
                             media_type=r.headers.get("content-type", "text/event-stream"))


@app.api_route("/v1/chat/completions", methods=["POST"], dependencies=[Depends(auth)])
async def chat_completions(request: Request) -> Response:
    try:
        return await _proxy(request, "/v1/chat/completions")
    except httpx.ConnectError:
        return JSONResponse({"error": "LLM server not ready"}, status_code=503)


@app.api_route("/v1/completions", methods=["POST"], dependencies=[Depends(auth)])
async def completions(request: Request) -> Response:
    return await _proxy(request, "/v1/completions")


@app.get("/v1/models", dependencies=[Depends(auth)])
async def models(request: Request) -> Response:
    return await _proxy(request, "/v1/models")
