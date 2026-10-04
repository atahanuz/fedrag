"""Tool abstraction shared by all agents."""

from __future__ import annotations

import asyncio
import datetime as dt
import inspect
import json
import os
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import httpx

from ..evidence import EvidenceStore
from ..llm import LLM
from ..retrieval.index import CorpusIndex
from ..trace import Trace

MAX_RESULT_CHARS = 14000

_contact = os.environ.get("FEDRAG_HTTP_CONTACT", "").strip()  # e.g. a URL or email; Wikimedia requires one
BOT_UA = f"fedrag/0.1 (agentic research assistant{'; ' + _contact if _contact else ''}) python-httpx"
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/126.0 Safari/537.36")


@dataclass
class RunContext:
    """Everything a tool may touch during one question."""

    index: CorpusIndex
    llm: LLM
    evidence: EvidenceStore = field(default_factory=EvidenceStore)
    trace: Trace = field(default_factory=Trace)
    http: httpx.AsyncClient | None = None
    today: dt.date = field(default_factory=dt.date.today)

    def client(self) -> httpx.AsyncClient:
        if self.http is None or self.http.is_closed:
            # An honest bot UA: some APIs (e.g. FRED) tarpit clients that claim to be a browser.
            self.http = httpx.AsyncClient(
                timeout=httpx.Timeout(25.0, connect=10.0),
                follow_redirects=True,
                headers={"User-Agent": BOT_UA},
            )
        return self.http

    async def get(self, url: str, params: dict | None = None, retries: int = 3,
                  timeout: float = 20.0, browser_fallback: bool = False) -> httpx.Response:
        """GET with retries on timeouts / connection errors / 5xx (public APIs are flaky)."""
        last: Exception | None = None
        for attempt in range(retries):
            try:
                r = await self.client().get(url, params=params, timeout=timeout)
                if r.status_code in (401, 403) and browser_fallback:
                    r = await self.client().get(url, params=params, timeout=timeout,
                                                headers={"User-Agent": BROWSER_UA})
                if r.status_code < 500:
                    return r
                last = httpx.HTTPStatusError(f"HTTP {r.status_code}", request=r.request, response=r)
            except httpx.TransportError as e:
                last = e
            await asyncio.sleep(1.0 + 2 * attempt)
        raise last  # type: ignore[misc]


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    fn: Callable[..., Awaitable[str]]

    def spec(self) -> dict:
        return {"type": "function", "function": {"name": self.name, "description": self.description,
                                                 "parameters": self.parameters}}

    async def __call__(self, ctx: RunContext, args: dict[str, Any]) -> str:
        allowed = set(inspect.signature(self.fn).parameters) - {"ctx"}
        clean = {k: v for k, v in args.items() if k in allowed and v is not None}
        missing = [p for p in self.parameters.get("required", []) if p not in clean]
        if missing:
            return f"ERROR: missing required argument(s): {', '.join(missing)}"
        try:
            out = await self.fn(ctx, **clean)
        except Exception as e:  # tool failures are reported to the agent, which can adapt
            return f"ERROR: {type(e).__name__}: {e}"
        if not isinstance(out, str):
            out = json.dumps(out, ensure_ascii=False, default=str)
        if len(out) > MAX_RESULT_CHARS:
            out = out[:MAX_RESULT_CHARS] + "\n...[truncated]"
        return out


def schema(properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": properties, "required": required or []}
