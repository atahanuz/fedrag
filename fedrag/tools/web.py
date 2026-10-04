"""Web search (DuckDuckGo via ddgs, no API key) and page reading."""

from __future__ import annotations

import asyncio
import re
from urllib.parse import urlparse

from .base import RunContext, Tool, schema

_RECENCY = {"day": "d", "week": "w", "month": "m", "year": "y"}


def _ddgs_search(query: str, max_results: int, timelimit: str | None, news: bool) -> list[dict]:
    from ddgs import DDGS

    with DDGS() as d:
        if news:
            return d.news(query, max_results=max_results, timelimit=timelimit)
        return d.text(query, max_results=max_results, timelimit=timelimit)


async def web_search(ctx: RunContext, query: str, max_results: int = 6, recency: str | None = None,
                     news: bool = False) -> str:
    max_results = max(1, min(int(max_results), 10))
    timelimit = _RECENCY.get((recency or "").lower())
    last_err = None
    for attempt in range(3):
        try:
            results = await asyncio.wait_for(
                asyncio.to_thread(_ddgs_search, query, max_results, timelimit, bool(news)), timeout=30)
            break
        except Exception as e:  # ddgs raises on rate limits / backend hiccups
            last_err = e
            await asyncio.sleep(2 * (attempt + 1))
    else:
        return f"ERROR: web search failed: {last_err}"
    if not results:
        return "No results. Try a different phrasing or remove the recency filter."
    lines = []
    for r in results:
        url = r.get("href") or r.get("url") or ""
        title = (r.get("title") or "").strip()
        body = (r.get("body") or "").strip()
        date = r.get("date")
        ev = ctx.evidence.add_web(url, title, body, published=(date or "")[:10] or None, kind="snippet")
        dom = urlparse(url).netloc
        lines.append(f"[{ev.id}] {title} — {dom}" + (f" ({date[:10]})" if date else "") + f"\n{url}\n{body}")
    return "\n\n".join(lines)


def _extract(html: str, url: str) -> tuple[str, str, str | None]:
    import trafilatura

    text = trafilatura.extract(html, url=url, include_tables=True, include_comments=False, favor_precision=True) or ""
    meta = trafilatura.extract_metadata(html, default_url=url)
    title = (meta.title if meta and meta.title else "") or url
    date = meta.date if meta and meta.date else None
    return text, title, date


def _pdf_text(data: bytes) -> str:
    import pymupdf

    doc = pymupdf.open(stream=data, filetype="pdf")
    return "\n\n".join(page.get_text() for page in doc[:40])


def _focus(text: str, query: str, max_chars: int) -> str:
    """Keep the paragraphs most relevant to ``query`` (BM25), in document order."""
    from ..retrieval.bm25 import BM25

    paras = [p.strip() for p in re.split(r"\n\s*\n|\n(?=[A-Z])", text) if len(p.strip()) > 40]
    if not paras:
        return text[:max_chars]
    scores = BM25(paras).scores(query)
    order = sorted(range(len(paras)), key=lambda i: -scores[i])
    keep, total = set(), 0
    for i in order:
        if total + len(paras[i]) > max_chars and keep:
            break
        keep.add(i)
        total += len(paras[i])
    out, prev = [], -2
    for i in sorted(keep):
        if i != prev + 1:
            out.append("[...]")
        out.append(paras[i])
        prev = i
    return "\n\n".join(out)


async def fetch_webpage(ctx: RunContext, url: str, focus: str | None = None, max_chars: int = 7000) -> str:
    if not re.match(r"^https?://", url):
        return "ERROR: url must start with http:// or https://"
    max_chars = max(1500, min(int(max_chars), 12000))
    try:
        r = await ctx.get(url, timeout=25.0, retries=2, browser_fallback=True)
    except Exception as e:
        return f"ERROR: could not fetch {url}: {type(e).__name__}: {e}"
    if r.status_code >= 400:
        return f"ERROR: HTTP {r.status_code} for {url}"
    ctype = r.headers.get("content-type", "")
    if "pdf" in ctype or url.lower().endswith(".pdf"):
        text, title, date = await asyncio.to_thread(_pdf_text, r.content), url.rsplit("/", 1)[-1], None
    else:
        text, title, date = await asyncio.to_thread(_extract, r.text, url)
    text = (text or "").strip()
    if len(text) < 200:
        return f"Fetched {url} but found little readable text (paywall, script-rendered page or blocked)."
    if len(text) > max_chars:
        text = _focus(text, focus, max_chars) if focus else text[:max_chars] + "\n[...truncated]"
    ev = ctx.evidence.add_web(url, title, text, published=date, kind="page")
    return f"[{ev.id}] {title}" + (f" (published {date})" if date else "") + f"\n{url}\n\n{text}"


def make_web_tools() -> list[Tool]:
    return [
        Tool(
            "web_search",
            "Search the web (DuckDuckGo). Returns titles, URLs and snippets, each with an evidence ID like [W2]. "
            "Use news=true for recent events and recency to restrict to the last day/week/month/year. "
            "Snippets are often enough for simple facts; fetch the page when details matter.",
            schema({
                "query": {"type": "string"},
                "max_results": {"type": "integer", "description": "1-10, default 6"},
                "recency": {"type": "string", "enum": ["day", "week", "month", "year"]},
                "news": {"type": "boolean", "description": "Search news articles (with publication dates)"},
            }, ["query"]),
            web_search,
        ),
        Tool(
            "fetch_webpage",
            "Download a web page or PDF and return its main text. Pass `focus` (what you are looking for) to get "
            "the most relevant paragraphs of long pages. The page gets an evidence ID.",
            schema({
                "url": {"type": "string"},
                "focus": {"type": "string", "description": "What to look for on the page"},
                "max_chars": {"type": "integer", "description": "1500-12000, default 7000"},
            }, ["url"]),
            fetch_webpage,
        ),
    ]
