"""Word (.docx) documents -> headings, paragraphs and tables in reading order.

Paragraph styles give the structure: ``Title`` and ``Heading N`` become headings, ``toc N`` lines (a
table of contents) are dropped, list styles become bullets. Tables are read in place: their text goes
into the page as ``a | b | c`` rows, and in documents with a modest number of tables each one with a
header becomes a SQL table as well. One-cell tables are boxes; those holding program code (the SCF
interview instrument stores its routing logic that way) are skipped.
"""

from __future__ import annotations

import re

from .document import Block, ParsedDoc, paginate
from .pdf_parser import normalize_text
from .tables import TableBlock, extract_tables, fmt_cell

MAX_SQL_TABLES = 50  # questionnaires hold thousands of small answer-code tables: text only
_CODE = re.compile(r"(\.Response\b|\.Ask\(|\bIOM\.|^\s*(Dim|If|Else|ElseIf|End If|Goto|Select Case|End Select|Case)\b|"
                   r"=\s*\{|\bThen\s*$|\(\")", re.M)


def _clean(s: str) -> str:
    return normalize_text(re.sub(r"[ \t\r\f\v]+", " ", s)).strip()


def parse_docx(path: str) -> ParsedDoc:
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph as DocxParagraph

    d = docx.Document(path)
    body = d.element.body
    n_tables = sum(1 for el in body.iterchildren() if el.tag.endswith("}tbl"))
    register = n_tables <= MAX_SQL_TABLES
    blocks: list[Block] = []
    tables: list[TableBlock] = []
    for el in body.iterchildren():
        tag = el.tag.rsplit("}", 1)[-1]
        if tag == "p":
            p = DocxParagraph(el, d)
            text = _clean(p.text)
            if not text:
                continue
            style = (p.style.name if p.style is not None else "").lower()
            if style.startswith("toc"):
                continue
            m = re.match(r"heading (\d)", style)
            if m or style == "title":
                level = int(m.group(1)) if m else 1
                if blocks and blocks[-1].kind == "heading" and style == "title" and blocks[-1].level == 1:
                    blocks[-1].text += " " + text  # a title set over two lines
                else:
                    blocks.append(Block("heading", text, level=level))
            elif "list" in style:
                blocks.append(Block("text", f"• {text}"))
            else:
                blocks.append(Block("text", text))
        elif tag == "tbl":
            t = Table(el, d)
            grid = [[_clean(c.text) or None for c in row.cells] for row in t.rows]
            # merged cells come back repeated; collapse horizontal repeats for the text rendering
            width = max((len(r) for r in grid), default=0)
            if width == 0:
                continue
            if width == 1 and len(grid) == 1:
                box = grid[0][0] or ""
                if box and not _CODE.search(box):
                    blocks.append(Block("text", box))
                continue
            lines = []
            for row in grid:
                cells = [fmt_cell(c) for i, c in enumerate(row) if i == 0 or c != row[i - 1]]
                line = " | ".join(cells).strip(" |")
                if line:
                    lines.append(line)
            idx = None
            if register and len(grid) >= 3:
                found = extract_tables(grid) or extract_tables(grid, header_rows=1)
                caption = next((b.text for b in reversed(blocks[-3:]) if b.kind == "heading"), "")
                for k, tb in enumerate(found):
                    tb.title = tb.title or caption or f"Table {len(tables) + 1}"
                    tb.source = f"Word table {len(tables) + 1}"
                    tables.append(tb)
                    idx = len(tables) - 1 if idx is None else idx
            blocks.append(Block("table", "\n".join(lines), table=idx))
    pages, toc, table_part = paginate(blocks)
    table_pages = [1] * len(tables)
    for ti, part in sorted(table_part.items()):
        for k in range(ti, len(tables)):
            table_pages[k] = part
    return ParsedDoc(path=path, pages=pages, toc=toc, tables=tables, table_pages=table_pages)
