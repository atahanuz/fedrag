"""Tools over the collection's structured data: find a table, inspect it, query it with SQL.

Every table found in the collection's spreadsheets, CSV files, web pages and Word files is a DuckDB
table (see fedrag.ingest.tables and fedrag.retrieval.tables). Query results become evidence [D#] that
cites the source documents, so numbers computed in SQL are as traceable as quoted passages.
"""

from __future__ import annotations

import asyncio

from .base import RunContext, Tool, schema
from .fed_docs import DOC_TYPES, _as_list, _norm_date


async def search_tables(ctx: RunContext, query: str, doc_types=None, date_from: str | None = None,
                        date_to: str | None = None, max_tables: int = 8) -> str:
    if not ctx.index.tables.available:
        return "ERROR: no data tables in this collection build."
    k = max(1, min(int(max_tables), 15))
    hits = await ctx.index.search(query, k=k, doc_types=_as_list(doc_types), date_from=_norm_date(date_from, False),
                                  date_to=_norm_date(date_to, True), kinds=["table"], candidates=40)
    if not hits:
        return "No tables match. Loosen the filters or describe the measure differently."
    lines = [f"Tables most relevant to {query!r} (best first):"]
    for h in hits:
        t = ctx.index.tables.catalog.get(h.chunk["table"])
        if t is None:
            continue
        d = ctx.index.docs[t["doc_id"]]
        cols = ", ".join(c["name"] for c in t["columns"][:12]) + (" ..." if len(t["columns"]) > 12 else "")
        rel = f"rel {h.rerank:.2f}" if h.rerank is not None else f"rrf {h.score:.3f}"
        lines.append(f"- `{t['table']}` ({t['kind']}, {t['n_rows']} rows, {t['layout']}) | {t['title'][:160]} | "
                     f"from: {d['title']} [{d['date']}] | columns: {cols} | {rel}")
    return "\n".join(lines)


async def list_tables(ctx: RunContext, doc_id: str | None = None, title_contains: str | None = None) -> str:
    cat = ctx.index.tables.catalog
    if doc_id and doc_id not in ctx.index.docs:
        return f"ERROR: unknown doc_id {doc_id!r}; use list_fed_documents."
    items = [t for t in cat.values()
             if (not doc_id or t["doc_id"] == doc_id or (t["kind"] == "view" and doc_id in t["source"]))
             and (not title_contains or title_contains.lower() in (t["title"] + " " + t["table"]).lower())]
    if not items:
        return "No tables match."
    lines = [f"{len(items)} table(s):"]
    for t in items[:120]:
        lines.append(f"- `{t['table']}` ({t['n_rows']} rows, {t['layout']}) {t['title'][:140]}")
    if len(items) > 120:
        lines.append(f"... {len(items) - 120} more; add title_contains")
    return "\n".join(lines)


async def describe_table(ctx: RunContext, table: str) -> str:
    return await asyncio.to_thread(ctx.index.tables.describe, table.strip().strip("`\""))


async def query_data(ctx: RunContext, sql: str, max_rows: int = 60) -> str:
    store = ctx.index.tables
    res = await asyncio.to_thread(store.query, sql, max(1, min(int(max_rows), 200)))
    if res.error:
        return f"ERROR: {res.error}"
    tables = [store.catalog[n] for n in res.tables if n in store.catalog]
    doc_ids: list[str] = []
    for t in tables:  # a stacked view cites the releases it covers
        ids = [t["doc_id"]] if t["kind"] == "table" else [store.catalog[n]["doc_id"] for n in
                                                          t["source"].removeprefix("view: ").split(", ")
                                                          if n in store.catalog]
        doc_ids += [i for i in ids if i not in doc_ids]
    docs = [ctx.index.docs[i] for i in doc_ids if i in ctx.index.docs]
    docs.sort(key=lambda d: d["sort_date"], reverse=True)
    text = res.render()
    ev = ctx.evidence.add_query(res.sql, text, tables, docs)
    n = len(res.rows)
    return f"[{ev.id}] {n} row(s){' (truncated)' if res.truncated else ''} from {', '.join(f'`{t}`' for t in res.tables) or 'query'}\n{text}"


def make_data_tools() -> list[Tool]:
    return [
        Tool(
            "search_tables",
            "Find SQL tables by what they measure (e.g. 'household debt by loan type', 'projected minimum CET1 "
            "ratio by bank', 'median one-year inflation expectations', 'SEP federal funds rate projections'). "
            "Returns table names, sizes, titles, source documents and columns.",
            schema({
                "query": {"type": "string"},
                "doc_types": {"type": "array", "items": {"type": "string", "enum": DOC_TYPES}},
                "date_from": {"type": "string"}, "date_to": {"type": "string"},
                "max_tables": {"type": "integer", "description": "1-15, default 8"},
            }, ["query"]),
            search_tables,
        ),
        Tool(
            "list_tables",
            "List the tables of one document (e.g. all sheets of a workbook) or tables whose title or name "
            "contains a phrase.",
            schema({"doc_id": {"type": "string"}, "title_contains": {"type": "string"}}),
            list_tables,
        ),
        Tool(
            "describe_table",
            "Schema of one table before you query it: columns (types, original labels, examples), units and "
            "notes, row labels, header values of long tables, and the first rows.",
            schema({"table": {"type": "string", "description": "Table name, e.g. dfast_results_2013_2026"}},
                   ["table"]),
            describe_table,
        ),
        Tool(
            "query_data",
            "Run one read-only DuckDB SQL query (SELECT / WITH) over the tables. Use it to look up exact values, "
            "filter, sort, rank, aggregate, join tables and compute differences or growth rates. Quote table "
            "names that contain special characters with double quotes. Returns up to max_rows rows with an "
            "evidence ID to cite.",
            schema({
                "sql": {"type": "string"},
                "max_rows": {"type": "integer", "description": "1-200, default 60"},
            }, ["sql"]),
            query_data,
        ),
    ]
