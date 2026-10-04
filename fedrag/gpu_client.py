"""Async client for the GPU gateway's embedding and rerank endpoints."""

from __future__ import annotations

import asyncio

import httpx
import numpy as np

from . import config


class GPUUnavailable(RuntimeError):
    pass


class GPUClient:
    def __init__(self, base_url: str | None = None, api_key: str | None = None, timeout: float = 60.0):
        self.base_url = (base_url or config.GPU_URL).rstrip("/")
        self.api_key = api_key if api_key is not None else config.API_KEY
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=self.timeout,
            )
        return self._client

    async def _post(self, path: str, payload: dict, retries: int = 3) -> dict:
        if not self.base_url:
            raise GPUUnavailable("FEDRAG_GPU_URL is not set")
        last: Exception | None = None
        for attempt in range(retries):
            try:
                r = await self.client.post(path, json=payload)
                if r.status_code >= 500:
                    raise httpx.HTTPStatusError(f"{r.status_code}", request=r.request, response=r)
                r.raise_for_status()
                return r.json()
            except (httpx.TransportError, httpx.HTTPStatusError) as e:
                last = e
                await asyncio.sleep(1.5 * (attempt + 1))
        raise GPUUnavailable(f"{path} failed: {last}")

    async def embed_queries(self, texts: list[str], instruction: str | None = None) -> np.ndarray:
        data = await self._post("/v1/embeddings", {"input": texts, "input_type": "query", "instruction": instruction})
        vecs = [d["embedding"] for d in sorted(data["data"], key=lambda d: d["index"])]
        return np.asarray(vecs, dtype=np.float32)

    async def rerank(self, query: str, documents: list[str], instruction: str | None = None) -> list[float]:
        data = await self._post("/v1/rerank", {"query": query, "documents": documents, "instruction": instruction})
        scores = [0.0] * len(documents)
        for r in data["results"]:
            scores[r["index"]] = r["relevance_score"]
        return scores

    async def health(self) -> dict:
        r = await self.client.get("/health", timeout=10)
        r.raise_for_status()
        return r.json()

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
