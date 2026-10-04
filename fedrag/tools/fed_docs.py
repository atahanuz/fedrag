"""Tools over the local Federal Reserve document collection."""

from __future__ import annotations

import re

from .base import RunContext, Tool, schema

DOC_TYPES = [
    "meeting_minutes", "economic_conditions_report", "monetary_policy_report", "financial_stability_report",
    "supervisory_report", "stress_test", "annual_report", "working_paper",
]
DOC_TYPE_HELP = (
    "meeting_minutes = FOMC minutes (dated by MEETING date; released ~3 weeks later); "
    "economic_conditions_report = Beige Book (12 District reports + national summary); "
    "monetary_policy_report = semiannual report to Congress; financial_stability_report = FSR; "
    "supervisory_report = Supervision and Regulation Report; stress_test = stress test results/scenarios/"
    "methodology and large-bank capital requirements; annual_report = Board annual report; "
    "working_paper = FEDS research papers"
)


def _norm_date(d: str | None, end: bool) -> str | None:
    if not d:
        return None
    d = d.strip()
    if re.fullmatch(r"\d{4}", d):
        return f"{d}-12-31" if end else f"{d}-01-01"
    if re.fullmatch(r"\d{4}-\d{2}", d):
        return f"{d}-31" if end else f"{d}-01"
    return d[:10]


def _as_list(v) -> list[str] | None:
    if v is None or v == "" or v == []:
        return None
    if isinstance(v, str):
        return [x.strip() for x in v.split(",") if x.strip()]
    return list(v)


# ---------------------------------------------------------------------------


async def search_fed_documents(ctx: RunContext, query: str, doc_types=None, date_from: str | None = None,
                               date_to: str | None = None, doc_ids=None, top_k: int = 6) -> str:
    doc_types = _as_list(doc_types)
    bad = [t for t in doc_types or [] if t not in DOC_TYPES]
    if bad:
        return f"ERROR: unknown doc_types {bad}. Valid: {DOC_TYPES}"
    top_k = max(1, min(int(top_k), 10))
    hits = await ctx.index.search(query, k=top_k, doc_types=doc_types, date_from=_norm_date(date_from, False),
                                  date_to=_norm_date(date_to, True), doc_ids=_as_list(doc_ids))
    if not hits:
        return "No passages match these filters. Loosen the filters (dates/doc_types) or check list_fed_documents."
    out = []
    for h in hits:
        c = h.chunk
        ev = ctx.evidence.add_chunk(c)
        rel = f"relevance={h.rerank:.2f}" if h.rerank is not None else f"rrf={h.score:.3f}"
        pages = f"p. {c['page_start']}" if c["page_start"] == c["page_end"] else f"pp. {c['page_start']}-{c['page_end']}"
        out.append(f"[{ev.id}] {c['title']} | {c['section'] or '-'} | {pages} | {rel} | chunk={c['chunk_id']}\n{c['text']}")
    best = max((h.rerank or 0.0) for h in hits)
    note = ""
    if hits[0].rerank is not None and best < 0.2:
        note = ("\n\nNOTE: no passage is clearly relevant (max relevance < 0.2). Rephrase, change filters, or "
                "conclude the collection may not contain this information.")
    return "\n\n---\n\n".join(out) + note


async def list_fed_documents(ctx: RunContext, doc_type: str | None = None, date_from: str | None = None,
                             date_to: str | None = None, title_contains: str | None = None) -> str:
    if doc_type and doc_type not in DOC_TYPES:
        return f"ERROR: unknown doc_type {doc_type!r}. Valid: {DOC_TYPES}"
    docs = ctx.index.list_docs([doc_type] if doc_type else None, _norm_date(date_from, False),
                               _norm_date(date_to, True), title_contains)
    if not docs:
        return "No documents match."
    lines = [f"{len(docs)} document(s):"]
    for d in docs:
        lines.append(f"- doc_id={d['doc_id']} | {d['title']} | date={d['date']} | {d['pages']} pages")
        if len(docs) <= 3 and d.get("toc"):
            for t in d["toc"]:
                if t["level"] <= 2:
                    lines.append(f"    {'  ' * (t['level'] - 1)}{t['title']} (p. {t['page']})")
    return "\n".join(lines)


async def get_document_outline(ctx: RunContext, doc_id: str) -> str:
    d = ctx.index.docs.get(doc_id)
    if d is None:
        return f"ERROR: unknown doc_id {doc_id!r}; use list_fed_documents to find ids."
    if d.get("toc"):
        body = "\n".join(f"{'  ' * (t['level'] - 1)}{t['title']} (p. {t['page']})" for t in d["toc"])
    else:
        # no PDF outline (e.g. FOMC minutes): list section paths with their first page
        seen: dict[str, int] = {}
        for i in ctx.index.by_doc[doc_id]:
            c = ctx.index.chunks[i]
            seen.setdefault(c["section"] or "-", c["page_start"])
        body = "\n".join(f"{s} (p. {p})" for s, p in seen.items())
    return f"{d['title']} ({d['date']}, {d['pages']} pages, url={d['url']})\n{body}"


async def read_document_pages(ctx: RunContext, doc_id: str, start_page: int, end_page: int | None = None) -> str:
    d = ctx.index.docs.get(doc_id)
    if d is None:
        return f"ERROR: unknown doc_id {doc_id!r}; use list_fed_documents to find ids."
    start_page = int(start_page)
    end_page = int(end_page or start_page)
    if end_page < start_page:
        start_page, end_page = end_page, start_page
    end_page = min(end_page, start_page + 2, d["pages"])  # at most 3 pages per call
    out = []
    for p in range(start_page, end_page + 1):
        pg = ctx.index.page(doc_id, p)
        if pg is None:
            continue
        ev = ctx.evidence.add_page(d, p, pg["text"], pg.get("section", ""))
        out.append(f"[{ev.id}] {d['title']}, page {p} (section: {pg.get('section') or '-'})\n{pg['text']}")
    return "\n\n---\n\n".join(out) if out else f"ERROR: pages {start_page}-{end_page} not found (document has {d['pages']} pages)."


async def expand_context(ctx: RunContext, evidence_id: str, before: int = 1, after: int = 1) -> str:
    ev = ctx.evidence.get(evidence_id)
    chunk_id = ev.meta.get("chunk_id") if ev else evidence_id
    if not chunk_id or ctx.index.get_chunk(chunk_id) is None:
        return "ERROR: pass the evidence ID of a search result (e.g. D3) or a chunk id."
    before, after = max(0, min(int(before), 3)), max(0, min(int(after), 3))
    out = []
    for c in ctx.index.neighbors(chunk_id, before, after):
        e = ctx.evidence.add_chunk(c)
        out.append(f"[{e.id}] {c['title']} | {c['section'] or '-'} | p. {c['page_start']}\n{c['text']}")
    return "\n\n---\n\n".join(out)


def make_fed_tools() -> list[Tool]:
    return [
        Tool(
            "search_fed_documents",
            "Hybrid semantic + keyword search with reranking over 93 Federal Reserve publications (2022 to Sep "
            "2026). Returns the most relevant passages, each with an evidence ID like [D3] to cite. Use filters "
            "whenever the question names a document type, meeting, report or period. Run several focused "
            "searches (different phrasings, one sub-topic each) rather than one broad search. " + DOC_TYPE_HELP,
            schema({
                "query": {"type": "string", "description": "Focused natural-language search query"},
                "doc_types": {"type": "array", "items": {"type": "string", "enum": DOC_TYPES},
                              "description": "Restrict to these document types"},
                "date_from": {"type": "string", "description": "Earliest document date, YYYY-MM-DD / YYYY-MM / YYYY"},
                "date_to": {"type": "string", "description": "Latest document date, YYYY-MM-DD / YYYY-MM / YYYY"},
                "doc_ids": {"type": "array", "items": {"type": "string"},
                            "description": "Restrict to specific documents (ids from list_fed_documents)"},
                "top_k": {"type": "integer", "description": "Number of passages (1-10, default 6)"},
            }, ["query"]),
            search_fed_documents,
        ),
        Tool(
            "list_fed_documents",
            "List documents in the collection (id, title, date, pages), optionally filtered. With 3 or fewer "
            "matches, also shows each document's table of contents with page numbers. Use it to resolve "
            "phrases like 'the latest Beige Book' or 'the June 2026 FOMC meeting' to a doc_id.",
            schema({
                "doc_type": {"type": "string", "enum": DOC_TYPES},
                "date_from": {"type": "string", "description": "YYYY-MM-DD / YYYY-MM / YYYY"},
                "date_to": {"type": "string", "description": "YYYY-MM-DD / YYYY-MM / YYYY"},
                "title_contains": {"type": "string", "description": "Case-insensitive title substring"},
            }),
            list_fed_documents,
        ),
        Tool(
            "get_document_outline",
            "Table of contents of one document with page numbers, to locate a section, box or table before "
            "reading pages.",
            schema({"doc_id": {"type": "string"}}, ["doc_id"]),
            get_document_outline,
        ),
        Tool(
            "read_document_pages",
            "Read the full cleaned text of up to 3 consecutive pages of a document (tables included as "
            "'label | value | value' rows). Use it to read a whole section or table that search only partly "
            "returned. Each page gets an evidence ID.",
            schema({
                "doc_id": {"type": "string"},
                "start_page": {"type": "integer"},
                "end_page": {"type": "integer", "description": "Inclusive; at most start_page + 2"},
            }, ["doc_id", "start_page"]),
            read_document_pages,
        ),
        Tool(
            "expand_context",
            "Return the passages immediately before/after a search result (by its evidence ID) to read the "
            "surrounding discussion.",
            schema({
                "evidence_id": {"type": "string", "description": "e.g. D3"},
                "before": {"type": "integer", "description": "0-3, default 1"},
                "after": {"type": "integer", "description": "0-3, default 1"},
            }, ["evidence_id"]),
            expand_context,
        ),
    ]
