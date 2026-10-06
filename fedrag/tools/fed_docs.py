"""Tools over the local Federal Reserve document collection (PDF, web pages, Word, spreadsheets)."""

from __future__ import annotations

import re

from ..retrieval.text_format import locator
from .base import RunContext, Tool, schema

DOC_TYPES = [
    "meeting_minutes", "fomc_statement", "economic_projections", "press_conference", "economic_conditions_report",
    "monetary_policy_report", "financial_stability_report", "supervisory_report", "stress_test", "annual_report",
    "working_paper", "feds_note", "speech", "testimony", "supervisory_letter", "loan_officer_survey",
    "press_release", "household_survey", "forecaster_survey",
]
DOC_TYPE_HELP = (
    "meeting_minutes = FOMC minutes (dated by MEETING date; released ~3 weeks later); fomc_statement = FOMC "
    "policy statements (decision and vote, released on the meeting's last day) and the Statement on Longer-Run "
    "Goals; economic_projections = Summary of Economic Projections (SEP, dot plot); press_conference = Chair's "
    "press conference transcripts (2023-2026); economic_conditions_report = Beige Book (12 District reports + "
    "national summary); monetary_policy_report = semiannual report to Congress; financial_stability_report = FSR; "
    "supervisory_report = Supervision and Regulation Report; stress_test = stress test results, scenarios, "
    "methodology, capital requirements and the bank-level results/scenario data files; annual_report = Board "
    "annual report; working_paper = FEDS research papers; feds_note = FEDS Notes (short research notes, 2025-2026); "
    "speech = speeches by the Chair and Governors (2025-2026); testimony = congressional testimony (2025-2026); "
    "supervisory_letter = SR letters; loan_officer_survey = Senior Loan Officer Opinion Survey (SLOOS); "
    "press_release = Board press releases (2025-2026); household_survey = Survey of Consumer Finances, SHED, NY Fed "
    "household debt and consumer expectations; forecaster_survey = Philadelphia Fed Survey of Professional "
    "Forecasters"
)


PASSAGE_CHARS = 1100  # per search result; agents re-read pages when they need more, the writer sees more


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
    ids = _as_list(doc_ids)
    found = await ctx.index.search(query, k=top_k * 3, doc_types=doc_types, date_from=_norm_date(date_from, False),
                                   date_to=_norm_date(date_to, True), doc_ids=ids, candidates=50)
    # prose passages fill the results, at most two per document unless the search is inside one document
    # (otherwise one long report crowds out the others); data tables that rank among them are pointers
    per_doc_cap = top_k if ids and len(ids) == 1 else 2
    hits, per_doc = [], {}
    for h in found:
        if h.chunk.get("kind", "text") != "text" or per_doc.get(h.chunk["doc_id"], 0) >= per_doc_cap:
            continue
        per_doc[h.chunk["doc_id"]] = per_doc.get(h.chunk["doc_id"], 0) + 1
        hits.append(h)
        if len(hits) == top_k:
            break
    tables = [h for h in found[:top_k] if h.chunk.get("kind") == "table"][:2]
    if not hits and not tables:
        return "No passages match these filters. Loosen the filters (dates/doc_types) or check list_fed_documents."
    out = []
    for h in hits:
        c = h.chunk
        ev = ctx.evidence.add_chunk(c)
        rel = f"relevance={h.rerank:.2f}" if h.rerank is not None else f"rrf={h.score:.3f}"
        where = locator(c.get("unit", "page"), c["page_start"], c["page_end"])
        text = c["text"] if len(c["text"]) <= PASSAGE_CHARS else c["text"][:PASSAGE_CHARS] + " ... [expand_context or read the page for more]"
        out.append(f"[{ev.id}] {c['title']} | {c['section'] or '-'} | {where} | {rel} | chunk={c['chunk_id']}\n{text}")
    if tables:
        out.append("Related data tables (the data_analyst agent can query them with SQL; read_document_pages shows a "
                   "preview):\n" + "\n".join(f"- `{h.chunk['table']}` in {h.chunk['title']} "
                                                f"({h.chunk.get('unit', 'part')} {h.chunk['page_start']}): "
                                                f"{h.chunk['section'].split(': ', 1)[-1][:150]}" for h in tables))
    if not hits:
        return "\n\n---\n\n".join(out)
    best = max((h.rerank or 0.0) for h in hits)
    note = ""
    if hits[0].rerank is not None and best < 0.2:
        note = ("\n\nNOTE: no passage is clearly relevant (max relevance < 0.2). Rephrase, change filters, or "
                "conclude the collection may not contain this information.")
    return "\n\n---\n\n".join(out) + note


async def search_each_document(ctx: RunContext, query: str, doc_type: str | None = None,
                               date_from: str | None = None, date_to: str | None = None,
                               title_contains: str | None = None, speaker: str | None = None, doc_ids=None,
                               per_doc: int = 2, max_documents: int = 10) -> str:
    """The best passages of EACH document in a set, so every meeting, edition or speech is covered."""
    if doc_type and doc_type not in DOC_TYPES:
        return f"ERROR: unknown doc_type {doc_type!r}. Valid: {DOC_TYPES}"
    ids = _as_list(doc_ids)
    if ids:
        unknown = [i for i in ids if i not in ctx.index.docs]
        if unknown:
            return f"ERROR: unknown doc_ids {unknown}; use list_fed_documents."
        docs = [ctx.index.docs[i] for i in ids]
    elif doc_type or date_from or title_contains or speaker:
        docs = ctx.index.list_docs([doc_type] if doc_type else None, _norm_date(date_from, False),
                                   _norm_date(date_to, True), title_contains, speaker)
    else:
        return "ERROR: name the documents: doc_ids, or doc_type / dates / title_contains / speaker."
    if not docs:
        return "No documents match these filters; check them with list_fed_documents."
    docs = sorted(docs, key=lambda d: d["sort_date"])
    cap = max(1, min(int(max_documents), 12))
    skipped = docs[:-cap] if len(docs) > cap else []
    docs = docs[-cap:]  # the most recent ones
    per_doc = max(1, min(int(per_doc), 3))
    found = await ctx.index.search_per_doc(query, [d["doc_id"] for d in docs], per_doc=per_doc)
    budget = max(350, 12500 // max(1, len(docs) * per_doc))  # keep the whole result readable
    out = [f"Best passages for {query!r} in each of {len(docs)} document(s), oldest first:"]
    for d in docs:
        hits = found.get(d["doc_id"], [])
        out.append(f"\n### {d['title']} [{d['date']}] doc_id={d['doc_id']}")
        if not hits:
            out.append("(no matching passage)")
        for h in sorted(hits, key=lambda h: h.chunk["position"]):
            c = h.chunk
            ev = ctx.evidence.add_chunk(c)
            text = c["text"] if len(c["text"]) <= budget else c["text"][:budget] + " ..."
            rel = f"relevance={h.rerank:.2f}" if h.rerank is not None else ""
            out.append(f"[{ev.id}] {c['section'] or '-'} | {locator(c.get('unit', 'page'), c['page_start'], c['page_end'])}"
                       f" | {rel}\n{text}")
    if skipped:
        out.append(f"\n({len(skipped)} older matching document(s) not searched, from {skipped[0]['date']} to "
                   f"{skipped[-1]['date']}: narrow the dates or call again for them.)")
    return "\n".join(out)


async def find_documents(ctx: RunContext, topic: str, doc_types=None, date_from: str | None = None,
                         date_to: str | None = None, max_documents: int = 8) -> str:
    doc_types = _as_list(doc_types)
    hits = await ctx.index.search(topic, k=40, doc_types=doc_types, date_from=_norm_date(date_from, False),
                                  date_to=_norm_date(date_to, True), candidates=40)
    by_doc: dict[str, list] = {}
    for h in hits:
        by_doc.setdefault(h.chunk["doc_id"], []).append(h)
    ranked = sorted(by_doc.items(), key=lambda kv: -sum(sorted((h.score for h in kv[1]), reverse=True)[:3]))
    if not ranked:
        return "No documents match."
    lines = [f"Documents most relevant to {topic!r} (strongest first):"]
    for doc_id, hs in ranked[: max(1, min(int(max_documents), 15))]:
        d = ctx.index.docs[doc_id]
        best = sorted(hs, key=lambda h: -h.score)[:3]
        secs = "; ".join(f"{h.chunk['section'] or '-'} ({locator(d.get('page_unit', 'page'), h.chunk['page_start'])}, "
                         f"rel {h.score:.2f})" for h in best)
        lines.append(f"- doc_id={doc_id} | {d['title']} | date={d['date']} | {d.get('format', 'pdf')} | "
                     f"{len(hs)} matching passages | {secs}")
    return "\n".join(lines)


async def list_fed_documents(ctx: RunContext, doc_type: str | None = None, date_from: str | None = None,
                             date_to: str | None = None, title_contains: str | None = None,
                             speaker: str | None = None) -> str:
    if doc_type and doc_type not in DOC_TYPES:
        return f"ERROR: unknown doc_type {doc_type!r}. Valid: {DOC_TYPES}"
    docs = ctx.index.list_docs([doc_type] if doc_type else None, _norm_date(date_from, False),
                               _norm_date(date_to, True), title_contains, speaker)
    if not docs:
        return "No documents match."
    lines = [f"{len(docs)} document(s):"]
    for d in docs[:80]:
        unit = d.get("page_unit", "page")
        lines.append(f"- doc_id={d['doc_id']} | {d['title']} | date={d['date']} | {d.get('format', 'pdf')}, "
                     f"{d['pages']} {unit}s" + (f" | {len(d['tables'])} data tables" if d.get("tables") else ""))
        if len(docs) <= 3 and d.get("toc"):
            for t in d["toc"][:40]:
                if t["level"] <= 2:
                    lines.append(f"    {'  ' * (t['level'] - 1)}{t['title']} ({locator(unit, t['page'])})")
    if len(docs) > 80:
        lines.append(f"... {len(docs) - 80} more; narrow the filters")
    return "\n".join(lines)


async def get_document_outline(ctx: RunContext, doc_id: str) -> str:
    d = ctx.index.docs.get(doc_id)
    if d is None:
        return f"ERROR: unknown doc_id {doc_id!r}; use list_fed_documents to find ids."
    unit = d.get("page_unit", "page")
    if d.get("toc"):
        body = "\n".join(f"{'  ' * (t['level'] - 1)}{t['title']} ({locator(unit, t['page'])})" for t in d["toc"][:150])
    else:
        # no outline (e.g. FOMC minutes): list section paths with their first page
        seen: dict[str, int] = {}
        for i in ctx.index.by_doc[doc_id]:
            c = ctx.index.chunks[i]
            seen.setdefault(c["section"] or "-", c["page_start"])
        body = "\n".join(f"{s} ({locator(unit, p)})" for s, p in seen.items())
    tables = f"\nData tables ({len(d['tables'])}): " + ", ".join(d["tables"][:40]) if d.get("tables") else ""
    return f"{d['title']} ({d['date']}, {d.get('format', 'pdf')}, {d['pages']} {unit}s, url={d['url']})\n{body}{tables}"


async def read_document_pages(ctx: RunContext, doc_id: str, start_page: int, end_page: int | None = None) -> str:
    d = ctx.index.docs.get(doc_id)
    if d is None:
        return f"ERROR: unknown doc_id {doc_id!r}; use list_fed_documents to find ids."
    start_page = int(start_page)
    end_page = int(end_page or start_page)
    if end_page < start_page:
        start_page, end_page = end_page, start_page
    end_page = min(end_page, start_page + 2, d["pages"])  # at most 3 pages per call
    unit = d.get("page_unit", "page")
    out = []
    for p in range(start_page, end_page + 1):
        pg = ctx.index.page(doc_id, p)
        if pg is None:
            continue
        ev = ctx.evidence.add_page(d, p, pg["text"], pg.get("section", ""))
        out.append(f"[{ev.id}] {d['title']}, {locator(unit, p)} (section: {pg.get('section') or '-'})\n{pg['text']}")
    return "\n\n---\n\n".join(out) if out else \
        f"ERROR: {unit}s {start_page}-{end_page} not found (document has {d['pages']} {unit}s)."


async def expand_context(ctx: RunContext, evidence_id: str, before: int = 1, after: int = 1) -> str:
    ev = ctx.evidence.get(evidence_id)
    chunk_id = ev.meta.get("chunk_id") if ev else evidence_id
    if not chunk_id or ctx.index.get_chunk(chunk_id) is None:
        return "ERROR: pass the evidence ID of a search result (e.g. D3) or a chunk id."
    before, after = max(0, min(int(before), 3)), max(0, min(int(after), 3))
    out = []
    for c in ctx.index.neighbors(chunk_id, before, after):
        e = ctx.evidence.add_chunk(c)
        where = locator(c.get("unit", "page"), c["page_start"], c["page_end"])
        out.append(f"[{e.id}] {c['title']} | {c['section'] or '-'} | {where}\n{c['text']}")
    return "\n\n---\n\n".join(out)


def make_fed_tools() -> list[Tool]:
    return [
        Tool(
            "search_fed_documents",
            "Hybrid semantic + keyword search with reranking over the Federal Reserve collection (about 725 documents: "
            "PDF reports, web pages, Word files and data tables, 2022 to Oct 2026). Returns the most relevant "
            "passages, each with an evidence ID like [D3] to cite, and points to related data tables. Use "
            "filters whenever the question names a document type, meeting, report or period. Run several focused "
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
            "search_each_document",
            "Run ONE query inside EACH document of a set and return the best passages of every document, oldest "
            "first, with evidence IDs. Use it when a question covers several meetings, editions, reports or "
            "speeches ('each FOMC statement of 2025', 'every 2024 Beige Book', 'the FSRs since 2023', 'Governor "
            "speeches on stablecoins'): unlike search_fed_documents, no document is crowded out by another. Name "
            "the set with doc_type and dates (and title_contains / speaker), or with doc_ids. Up to 12 documents "
            "(the most recent are kept; call again for older ones).",
            schema({
                "query": {"type": "string", "description": "What to find in each document"},
                "doc_type": {"type": "string", "enum": DOC_TYPES},
                "date_from": {"type": "string", "description": "YYYY-MM-DD / YYYY-MM / YYYY"},
                "date_to": {"type": "string", "description": "YYYY-MM-DD / YYYY-MM / YYYY"},
                "title_contains": {"type": "string"},
                "speaker": {"type": "string", "description": "e.g. 'Waller'"},
                "doc_ids": {"type": "array", "items": {"type": "string"}},
                "per_doc": {"type": "integer", "description": "Passages per document, 1-3 (default 2)"},
                "max_documents": {"type": "integer", "description": "1-12, default 10"},
            }, ["query"]),
            search_each_document,
        ),
        Tool(
            "find_documents",
            "Find WHICH documents discuss a topic: ranks documents by their best-matching passages and shows "
            "the most relevant sections/pages of each. Use it for questions like 'which reports or papers "
            "discuss X' or to pick documents before searching within them (doc_ids filter).",
            schema({
                "topic": {"type": "string"},
                "doc_types": {"type": "array", "items": {"type": "string", "enum": DOC_TYPES}},
                "date_from": {"type": "string"}, "date_to": {"type": "string"},
                "max_documents": {"type": "integer", "description": "1-15, default 8"},
            }, ["topic"]),
            find_documents,
        ),
        Tool(
            "list_fed_documents",
            "List documents in the collection (id, title, date, format, pages), optionally filtered. With 3 or "
            "fewer matches, also shows each document's table of contents. Use it to resolve phrases like "
            "'the latest Beige Book', 'the June 2026 FOMC meeting' or 'Governor Waller's speeches' to doc_ids.",
            schema({
                "doc_type": {"type": "string", "enum": DOC_TYPES},
                "date_from": {"type": "string", "description": "YYYY-MM-DD / YYYY-MM / YYYY"},
                "date_to": {"type": "string", "description": "YYYY-MM-DD / YYYY-MM / YYYY"},
                "title_contains": {"type": "string", "description": "Case-insensitive title substring"},
                "speaker": {"type": "string", "description": "Speeches/testimony by this person, e.g. 'Waller'"},
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
            "'label | value | value' rows). PDFs have pages; web pages and Word files are split into parts of "
            "~600 words; spreadsheets have one part per table. Use it to read a whole section or table that "
            "search only partly returned. Each page gets an evidence ID.",
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
