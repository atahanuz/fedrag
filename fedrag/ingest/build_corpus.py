"""Parse every document in the collection into pages, sections, chunks and SQL tables.

Usage::

    python -m fedrag.ingest.build_corpus            # writes data/corpus/

Inputs: ``federal_reserve/metadata.csv`` and the files it lists (PDF, HTML, Word, Excel, CSV).

Outputs (one JSON object per line, plus a DuckDB database):

* ``docs.jsonl``    one record per document: metadata + outline
* ``pages.jsonl``   cleaned text of every page (PDF) or part (web page, Word file) or table (spreadsheet)
* ``chunks.jsonl``  retrieval units with section path and page range; data tables get a *table card*
* ``tables.jsonl``  catalog of SQL tables: columns, labels, notes, row labels, header values
* ``tables.duckdb`` every table found in spreadsheets, CSV files, web pages and Word files, plus
  ``*__all`` views that stack a recurring table across releases (e.g. SEP Table 1 for every meeting)
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from ..config import CORPUS_DIR, DOCS_DIR
from .chunker import Chunk, assign_sections, chunk_document
from .tables import TableBlock, TableInfo, describe_frame

SERIES_LABELS = {
    "meeting_minutes": "FOMC minutes",
    "fomc_statement": "FOMC statement",
    "economic_projections": "Summary of Economic Projections",
    "press_conference": "FOMC press conference",
    "economic_conditions_report": "Beige Book",
    "monetary_policy_report": "Monetary Policy Report",
    "financial_stability_report": "Financial Stability Report",
    "supervisory_report": "Supervision and Regulation Report",
    "stress_test": "Stress test",
    "annual_report": "Annual Report",
    "working_paper": "FEDS working paper",
    "feds_note": "FEDS Notes",
    "speech": "Speech",
    "testimony": "Testimony",
    "supervisory_letter": "SR letter",
    "loan_officer_survey": "Senior Loan Officer Opinion Survey",
    "press_release": "Press release",
    "household_survey": "Household survey",
    "forecaster_survey": "Survey of Professional Forecasters",
}
PAGE_UNIT = {"pdf": "page", "html": "part", "docx": "part", "xlsx": "table", "xls": "table", "csv": "table"}
CARD_ROWS_MAX_CELLS = 400  # small tables carry their rows in the card, so search can return the values
_MONTHS = r"january|february|march|april|may|june|july|august|september|october|november|december"


def sort_date(date: str, doc_type: str) -> str:
    """Normalise the mixed-precision ``date`` column to YYYY-MM-DD for ordering/filtering."""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        return date
    if re.fullmatch(r"\d{4}-\d{2}", date):
        return f"{date}-15"
    if re.fullmatch(r"\d{4}", date):
        # annual reports cover a calendar year and are published the following spring
        return f"{date}-12-31"
    return date


def parse_any(path: str, fmt: str):
    if fmt == "pdf":
        from .pdf_parser import parse_pdf

        return parse_pdf(path)
    if fmt == "html":
        from .html_parser import parse_html

        return parse_html(path)
    if fmt == "docx":
        from .docx_parser import parse_docx

        return parse_docx(path)
    if fmt in ("xlsx", "xls", "csv"):
        from .sheet_parser import parse_sheets

        return parse_sheets(path)
    raise ValueError(f"unsupported format {fmt!r} for {path}")


# ---------------------------------------------------------------------------
# Tables: names, catalog entries, cards
# ---------------------------------------------------------------------------


def _slug(s: str, n: int = 40) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")[:n].strip("_")


_ABBREV = [("supervisory_", ""), ("international", "intl"), ("domestic", "dom"), ("historical", "hist"),
           ("historic", "hist"), ("household_debt_credit", "household_debt"), ("consumer_expectations", "sce"),
           ("interview_instrument", "instrument")]


def doc_slug(doc_id: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", re.sub(r"^fed_", "", doc_id.lower())).strip("_")
    for a, b in _ABBREV:
        s = s.replace(a, b)
    return s[:56].strip("_")


def unique_slugs(doc_ids: list[str]) -> dict[str, str]:
    import hashlib

    out: dict[str, str] = {}
    taken: set[str] = set()
    for d in doc_ids:
        s = doc_slug(d)
        if s in taken:
            s = f"{s[:50]}_{hashlib.sha1(d.encode()).hexdigest()[:5]}"
        taken.add(s)
        out[d] = s
    return out


def table_part_name(b: TableBlock, k: int, n_tables: int) -> str:
    m = re.match(r"^(table|figure|exhibit|chart)\s+([0-9]+(?:\.[0-9a-z]+)?)", b.title or "", re.I)
    if m:
        return _slug(f"{m.group(1)}_{m.group(2)}")
    sheet = re.match(r'^sheet "([^"]+)"(?:, table (\d+))?', b.source or "")
    if sheet:
        return _slug(sheet.group(1), 30) + (f"_{sheet.group(2)}" if sheet.group(2) else "")
    return "" if n_tables == 1 else f"t{k + 1}"


def build_tables(meta: dict, parsed) -> list[tuple[str, object, TableInfo, TableBlock]]:
    """[(sql name, DataFrame, catalog entry, block)] for every table block of one document."""
    out = []
    used: set[str] = set()
    tables = getattr(parsed, "tables", []) or []
    for k, b in enumerate(tables):
        try:
            df = b.frame()
        except Exception as e:  # a malformed block must not stop the build
            print(f"  skipped table {k} of {meta['doc_id']}: {e}")
            continue
        if df.empty or len(df.columns) < 2:
            continue
        part = table_part_name(b, k, len(tables))
        name = meta["slug"] + (f"__{part}" if part else "")
        base, i = name, 2
        while name in used:
            name = f"{base}_{i}"
            i += 1
        used.add(name)
        cols, row_labels, header_values = describe_frame(df, b)
        title = b.title or (meta["title"] if len(tables) == 1 else re.sub(r'^sheet "([^"]+)".*', r"\1", b.source))
        info = TableInfo(table=name, doc_id=meta["doc_id"], title=title, source=b.source, layout=b.layout,
                         n_rows=len(df), columns=cols, notes=b.notes[:8], row_labels=row_labels,
                         header_values=header_values, part=parsed.table_pages[k] if k < len(parsed.table_pages) else 1)
        out.append((name, df, info, b))
    return out


def card_text(info: TableInfo, meta: dict, block: TableBlock, with_rows: bool) -> str:
    text = info.card(meta["title"], meta["date"], meta.get("description", ""))
    if with_rows:  # small tables in full; long series show their first and latest rows
        small = len(block.rows) * max(block.n_cols, 1) <= CARD_ROWS_MAX_CELLS
        text += "\nRows:\n" + block.render(max_rows=60 if small else 12, meta=False)
    return text


def card_chunk(info: TableInfo, meta: dict, text: str, pos: int) -> dict:
    return {
        "chunk_id": f"{meta['doc_id']}#T{pos:03d}",
        **{k: meta[k] for k in ("doc_id", "doc_type", "series", "title", "date", "sort_date")},
        "position": 10000 + pos, "section": f"Data table {info.table}: {info.title}"[:300],
        "page_start": info.part, "page_end": info.part, "n_words": len(text.split()), "text": text,
        "kind": "table", "table": info.table, "unit": meta["page_unit"],
    }


# ---------------------------------------------------------------------------
# One document
# ---------------------------------------------------------------------------


def _process(row: dict):
    fmt = (row.get("format") or Path(row["filename"]).suffix.lstrip(".")).lower()
    path = DOCS_DIR / row["filename"]
    parsed = parse_any(str(path), fmt)
    doc_id = row["id"]
    meta = {
        "doc_id": doc_id,
        "doc_type": row["doc_type"],
        "series": SERIES_LABELS.get(row["doc_type"], row["doc_type"]),
        "title": row["title"],
        "date": row["date"],
        "sort_date": sort_date(row["date"], row["doc_type"]),
        "pages": len(parsed.pages),
        "url": row["url"],
        "filename": row["filename"],
        "format": fmt,
        "page_unit": PAGE_UNIT.get(fmt, "page"),
        "publisher": row.get("publisher") or "Board of Governors of the Federal Reserve System",
        "speaker": row.get("speaker") or "",
        "slug": row.get("slug") or doc_slug(doc_id),
        "description": row.get("description") or "",
    }
    is_data = getattr(parsed, "kind", "text") == "data"
    if is_data:  # prose comes only from text sheets; tables are represented by their cards
        text_pages = {pg.number for pg in parsed.pages if not any(p.kind == "table" for p in pg.paragraphs)}
        paras = [p for p in assign_sections(parsed) if p.page in text_pages]
    else:
        paras = assign_sections(parsed)
    chunks: list[Chunk] = chunk_document(paras) if paras else []

    page_section: dict[int, str] = {}
    for p in paras:
        page_section.setdefault(p.page, " > ".join(p.path))
    for pg in parsed.pages:
        if pg.number not in page_section and pg.paragraphs and pg.paragraphs[0].kind == "heading":
            page_section[pg.number] = pg.paragraphs[0].text
    pages = [{"doc_id": doc_id, "page": pg.number, "section": page_section.get(pg.number, ""), "text": pg.text}
             for pg in parsed.pages]
    chunk_rows = [
        {
            "chunk_id": f"{doc_id}#{c.position:04d}",
            **{k: meta[k] for k in ("doc_id", "doc_type", "series", "title", "date", "sort_date")},
            "position": c.position, "section": c.section, "page_start": c.page_start, "page_end": c.page_end,
            "n_words": c.n_words, "text": c.text, "kind": "text", "unit": meta["page_unit"],
        }
        for c in chunks
    ]
    tables = build_tables(meta, parsed)
    for i, (_, _, info, block) in enumerate(tables):
        chunk_rows.append(card_chunk(info, meta, card_text(info, meta, block, with_rows=is_data), i))
    meta["toc"] = [{"level": lvl, "title": t, "page": pg} for lvl, t, pg in parsed.toc]
    meta["n_chunks"] = len(chunk_rows)
    meta["tables"] = [info.table for _, _, info, _ in tables]
    return meta, pages, chunk_rows, [(name, df, info) for name, df, info, _ in tables]


# ---------------------------------------------------------------------------
# Views that stack a recurring table across releases
# ---------------------------------------------------------------------------


def _norm_title(t: str) -> str:
    t = re.sub(rf"\b({_MONTHS})\b", " ", t.lower())
    t = re.sub(r"\b(19|20)\d{2}(\s*[–-]\s*\d{2,4})?\b", " ", t)
    return re.sub(r"[^a-z]+", " ", t).strip()


def stacked_views(results) -> list[tuple[str, str, TableInfo, str]]:
    """Group tables that recur across documents of one series (same normalised title, same columns) and
    define a UNION ALL view over them with doc_id and doc_date columns: (name, sql, info, doc_id)."""
    groups: dict[tuple, list[tuple[dict, TableInfo]]] = defaultdict(list)
    for meta, _, _, tables in results:
        for name, df, info in tables:
            key = (meta["doc_type"], _norm_title(info.title), tuple(df.columns), info.layout)
            groups[key].append((meta, info))
    views = []
    taken: set[str] = set()
    for (doc_type, _, columns, _), members in groups.items():
        if len({m["doc_id"] for m, _ in members}) < 3:
            continue
        members.sort(key=lambda mi: mi[0]["sort_date"])
        latest_meta, latest = members[-1]
        # the latest member's name without its date: sep_2026_09_16__table_1 -> sep__table_1__all
        name = "__".join(re.sub(r"_+", "_", re.sub(r"(19|20)\d{2}(_\d{2}){0,2}|\d{4}q\d", "", part)).strip("_")
                         for part in latest.table.split("__")) + "__all"
        while name in taken:
            name = name.replace("__all", "_2__all")
        taken.add(name)
        sql = " UNION ALL ".join(f"SELECT '{m['doc_id']}' AS doc_id, DATE '{m['sort_date']}' AS doc_date, * "
                                 f"FROM {i.table}" for m, i in members)
        cols = [{"name": "doc_id", "label": "source document", "type": "text",
                 "examples": [m["doc_id"] for m, _ in members[-3:]]},
                {"name": "doc_date", "label": "release date", "type": "date",
                 "examples": [members[0][0]["sort_date"], latest_meta["sort_date"]]}] + latest.columns
        header_values: dict[str, list[str]] = {}
        for _, i in members:
            for h, vals in i.header_values.items():
                header_values.setdefault(h, [])
                header_values[h] += [v for v in vals if v not in header_values[h]]
        info = TableInfo(table=name, doc_id=latest_meta["doc_id"],
                         title=f"{latest.title} - every release stacked ({len(members)} releases, "
                               f"{members[0][0]['sort_date']} to {latest_meta['sort_date']})",
                         source="view: " + ", ".join(i.table for _, i in members), layout=latest.layout,
                         n_rows=sum(i.n_rows for _, i in members), columns=cols, notes=latest.notes,
                         row_labels=latest.row_labels, header_values=header_values, part=latest.part)
        views.append((name, sql, info, latest_meta["doc_id"]))
    return views


# ---------------------------------------------------------------------------


def main() -> None:
    import duckdb

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=CORPUS_DIR)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--only", help="substring filter on document id (debugging)")
    args = ap.parse_args()

    rows = list(csv.DictReader(open(DOCS_DIR / "metadata.csv", encoding="utf-8")))
    rows = [r for r in rows if (DOCS_DIR / r["filename"]).exists()]
    slugs = unique_slugs([r["id"] for r in rows])
    for r in rows:
        r["slug"] = slugs[r["id"]]
    if args.only:
        rows = [r for r in rows if args.only in r["id"]]
    args.out.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        results = list(ex.map(_process, rows, chunksize=4))

    results.sort(key=lambda r: (r[0]["doc_type"], r[0]["sort_date"], r[0]["doc_id"]))
    views = stacked_views(results)
    view_cards: dict[str, list[dict]] = defaultdict(list)
    metas = {r[0]["doc_id"]: r[0] for r in results}
    for k, (name, _, info, doc_id) in enumerate(views):
        m = metas[doc_id]
        view_cards[doc_id].append(card_chunk(info, m, info.card(m["title"], m["date"], m.get("description", "")),
                                             900 + k))

    db_path = args.out / "tables.duckdb"
    if db_path.exists():
        db_path.unlink()
    con = duckdb.connect(str(db_path))
    catalog: list[dict] = []
    with open(args.out / "docs.jsonl", "w", encoding="utf-8") as fd, \
         open(args.out / "pages.jsonl", "w", encoding="utf-8") as fp, \
         open(args.out / "chunks.jsonl", "w", encoding="utf-8") as fc:
        for meta, pages, chunks, tables in results:
            for name, df, info in tables:
                con.register("_df", df)
                con.execute(f'CREATE TABLE "{name}" AS SELECT * FROM _df')
                con.unregister("_df")
                catalog.append({**info.__dict__, "kind": "table"})
            chunks = chunks + view_cards.get(meta["doc_id"], [])
            meta["n_chunks"] = len(chunks)
            fd.write(json.dumps(meta, ensure_ascii=False) + "\n")
            for p in pages:
                fp.write(json.dumps(p, ensure_ascii=False) + "\n")
            for c in chunks:
                fc.write(json.dumps(c, ensure_ascii=False) + "\n")
    for name, sql, info, _ in views:
        con.execute(f'CREATE VIEW "{name}" AS {sql}')
        catalog.append({**info.__dict__, "kind": "view", "sql": sql})
    con.close()
    with open(args.out / "tables.jsonl", "w", encoding="utf-8") as ft:
        for t in catalog:
            ft.write(json.dumps(t, ensure_ascii=False, default=str) + "\n")

    n_chunks = sum(len(r[2]) for r in results) + sum(len(v) for v in view_cards.values())
    n_pages = sum(len(r[1]) for r in results)
    words = [c["n_words"] for r in results for c in r[2] if c.get("kind") == "text"]
    by_fmt: dict[str, int] = defaultdict(int)
    for r in results:
        by_fmt[r[0]["format"]] += 1
    print(f"{len(results)} documents ({', '.join(f'{v} {k}' for k, v in sorted(by_fmt.items()))}), {n_pages} pages/parts, "
          f"{n_chunks} chunks (text words/chunk: mean {sum(words) / max(len(words), 1):.0f}, max {max(words, default=0)}), "
          f"{sum(1 for t in catalog if t['kind'] == 'table')} SQL tables + {len(views)} stacked views "
          f"in {time.time() - t0:.1f}s -> {args.out}")


if __name__ == "__main__":
    main()
