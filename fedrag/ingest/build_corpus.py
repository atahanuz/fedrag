"""Parse every PDF in the document set into pages, sections and chunks.

Usage::

    python -m fedrag.ingest.build_corpus            # writes data/corpus/*.jsonl

Outputs (one JSON object per line):

* ``docs.jsonl``   one record per document: metadata + outline
* ``pages.jsonl``  cleaned text of every page (used by the page-reader tool)
* ``chunks.jsonl`` retrieval units with section path and page range
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from ..config import CORPUS_DIR, DOCS_DIR
from .chunker import assign_sections, chunk_document
from .pdf_parser import parse_pdf

SERIES_LABELS = {
    "meeting_minutes": "FOMC minutes",
    "economic_conditions_report": "Beige Book",
    "monetary_policy_report": "Monetary Policy Report",
    "financial_stability_report": "Financial Stability Report",
    "supervisory_report": "Supervision and Regulation Report",
    "stress_test": "Stress test",
    "annual_report": "Annual Report",
    "working_paper": "FEDS working paper",
}


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


def _process(row: dict) -> tuple[dict, list[dict], list[dict]]:
    path = DOCS_DIR / row["filename"]
    parsed = parse_pdf(str(path))
    paras = assign_sections(parsed)
    chunks = chunk_document(paras)

    doc_id = row["id"]
    meta = {
        "doc_id": doc_id,
        "doc_type": row["doc_type"],
        "series": SERIES_LABELS.get(row["doc_type"], row["doc_type"]),
        "title": row["title"],
        "date": row["date"],
        "sort_date": sort_date(row["date"], row["doc_type"]),
        "pages": int(row["pages"]),
        "url": row["url"],
        "filename": row["filename"],
    }
    # page -> section path at the top of that page (for the page-reader tool)
    page_section: dict[int, str] = {}
    for p in paras:
        page_section.setdefault(p.page, " > ".join(p.path))

    pages = [
        {"doc_id": doc_id, "page": pg.number, "section": page_section.get(pg.number, ""), "text": pg.text}
        for pg in parsed.pages
    ]
    chunk_rows = [
        {
            "chunk_id": f"{doc_id}#{c.position:04d}",
            **{k: meta[k] for k in ("doc_id", "doc_type", "series", "title", "date", "sort_date")},
            "position": c.position,
            "section": c.section,
            "page_start": c.page_start,
            "page_end": c.page_end,
            "n_words": c.n_words,
            "text": c.text,
        }
        for c in chunks
    ]
    meta["toc"] = [{"level": lvl, "title": t, "page": pg} for lvl, t, pg in parsed.toc]
    meta["n_chunks"] = len(chunk_rows)
    return meta, pages, chunk_rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=CORPUS_DIR)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--only", help="substring filter on document id (debugging)")
    args = ap.parse_args()

    rows = list(csv.DictReader(open(DOCS_DIR / "metadata.csv", encoding="utf-8")))
    if args.only:
        rows = [r for r in rows if args.only in r["id"]]
    args.out.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        results = list(ex.map(_process, rows))

    results.sort(key=lambda r: (r[0]["doc_type"], r[0]["sort_date"]))
    with open(args.out / "docs.jsonl", "w", encoding="utf-8") as fd, \
         open(args.out / "pages.jsonl", "w", encoding="utf-8") as fp, \
         open(args.out / "chunks.jsonl", "w", encoding="utf-8") as fc:
        for meta, pages, chunks in results:
            fd.write(json.dumps(meta, ensure_ascii=False) + "\n")
            for p in pages:
                fp.write(json.dumps(p, ensure_ascii=False) + "\n")
            for c in chunks:
                fc.write(json.dumps(c, ensure_ascii=False) + "\n")

    n_chunks = sum(len(r[2]) for r in results)
    n_pages = sum(len(r[1]) for r in results)
    words = [c["n_words"] for r in results for c in r[2]]
    print(f"{len(results)} documents, {n_pages} pages, {n_chunks} chunks "
          f"(words/chunk: mean {sum(words) / max(len(words), 1):.0f}, max {max(words, default=0)}) "
          f"in {time.time() - t0:.1f}s -> {args.out}")


if __name__ == "__main__":
    main()
