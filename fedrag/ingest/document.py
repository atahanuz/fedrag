"""Format-independent document structure shared by the HTML, Word and spreadsheet parsers.

PDF pages are real pages. Web pages and Word files have none, so their text is split into *parts* of
about 600 words that break at headings where possible; a part plays the role of a page everywhere (page
reader tool, citations, chunk page ranges). A spreadsheet or CSV gets one part per table.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .pdf_parser import Page, Paragraph
from .tables import TableBlock

PART_WORDS = 600


@dataclass
class ParsedDoc:
    path: str
    pages: list[Page]
    toc: list[tuple[int, str, int]]  # (level, title, 1-based page/part)
    body_size: float = 10.0
    tables: list[TableBlock] = field(default_factory=list)
    table_pages: list[int] = field(default_factory=list)  # part that shows each table
    kind: str = "text"  # "text" (pages of prose) | "data" (one part per table)


@dataclass
class Block:
    """One paragraph-level element in reading order, before pagination."""

    kind: str  # "text" | "heading" | "table"
    text: str
    level: int = 0  # heading level (1 = top)
    table: int | None = None  # index into ParsedDoc.tables


def paginate(blocks: list[Block], part_words: int = PART_WORDS) -> tuple[list[Page], list[tuple[int, str, int]],
                                                                          dict[int, int]]:
    """Group blocks into parts of about ``part_words`` words, starting a new part at a heading when the
    current one is at least 40% full. Returns pages, the outline (level, title, part) and table -> part."""
    pages: list[Page] = []
    toc: list[tuple[int, str, int]] = []
    table_part: dict[int, int] = {}
    cur: list[Paragraph] = []
    words = 0

    def flush() -> None:
        nonlocal cur, words
        if cur:
            pages.append(Page(number=len(pages) + 1, paragraphs=cur))
        cur, words = [], 0

    for b in blocks:
        n = len(b.text.split())
        if cur and ((b.kind == "heading" and b.level <= 3 and words >= 0.4 * part_words)
                    or (words + n > part_words and b.kind != "heading" and words >= 0.25 * part_words)):
            flush()
        if b.kind == "heading":
            toc.append((b.level, b.text, len(pages) + 1))
        if b.table is not None:
            table_part[b.table] = len(pages) + 1
        if b.kind == "table" and n > part_words:  # a long table becomes parts of its own
            flush()
            rows = b.text.split("\n")
            chunk: list[str] = []
            for r in rows:
                chunk.append(r)
                if len(" ".join(chunk).split()) >= part_words:
                    pages.append(Page(number=len(pages) + 1, paragraphs=[Paragraph("\n".join(chunk), "table")]))
                    chunk = [rows[0]] if rows else []  # repeat the header line
            if len(chunk) > 1:
                pages.append(Page(number=len(pages) + 1, paragraphs=[Paragraph("\n".join(chunk), "table")]))
            continue
        cur.append(Paragraph(b.text, b.kind))
        words += n
    flush()
    return pages, toc, table_part
