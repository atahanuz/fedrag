"""Excel workbooks (.xlsx, .xls) and CSV files -> table blocks and one readable part per table.

Chart sheets are skipped, merged cells are expanded before table detection, and sheets that hold only
text (notes, disclaimers) become prose parts. Every table block found by ``tables.extract_tables`` is
stored as a SQL table at build time; its part shows the title, notes, header and first rows.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path

from .document import ParsedDoc
from .pdf_parser import Page, Paragraph
from .tables import TableBlock, extract_tables, fmt_cell

SKIP_SHEETS = re.compile(r"^(disclaimer|license|licence|sheet\d*|chart\d*)$", re.I)
MAX_TEXT_ROWS = 120


def read_sheets(path: str) -> list[tuple[str, list[list], list[tuple[int, int, int, int]]]]:
    """[(sheet name, grid of raw values, merged ranges as 0-based (r0, c0, r1, c1))]"""
    ext = Path(path).suffix.lower()
    if ext == ".csv":
        with open(path, encoding="utf-8-sig", errors="replace", newline="") as f:
            return [("csv", [row for row in csv.reader(f)], [])]
    if ext == ".xls":
        import xlrd

        book = xlrd.open_workbook(path, formatting_info=False)
        out = []
        for sh in book.sheets():
            grid = []
            for r in range(sh.nrows):
                row = []
                for c in range(sh.ncols):
                    cell = sh.cell(r, c)
                    if cell.ctype == xlrd.XL_CELL_DATE:
                        row.append(xlrd.xldate.xldate_as_datetime(cell.value, book.datemode))
                    elif cell.ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
                        row.append(None)
                    else:
                        row.append(cell.value)
                grid.append(row)
            out.append((sh.name, grid, [(r0, c0, r1 - 1, c1 - 1) for r0, r1, c0, c1 in sh.merged_cells]))
        return out
    import openpyxl

    wb = openpyxl.load_workbook(path, data_only=True)  # cached formula results, not formulas
    out = []
    for ws in wb.worksheets:  # chart sheets are not worksheets
        grid = [list(r) for r in ws.iter_rows(values_only=True)]
        merged = [(m.min_row - 1, m.min_col - 1, m.max_row - 1, m.max_col - 1) for m in ws.merged_cells.ranges]
        out.append((ws.title, grid, merged))
    return out


def parse_sheets(path: str) -> ParsedDoc:
    pages: list[Page] = []
    toc: list[tuple[int, str, int]] = []
    tables: list[TableBlock] = []
    table_pages: list[int] = []
    multi = Path(path).suffix.lower() != ".csv"
    for name, grid, merged in read_sheets(path):
        if not any(v not in (None, "") for row in grid for v in row) or (multi and SKIP_SHEETS.match(name.strip())):
            continue
        src = f'sheet "{name}"' if multi else "CSV file"
        blocks = extract_tables(grid, merged, source=src)
        if not blocks:  # a text sheet (notes, table of contents): keep its lines as prose
            lines = [" ".join(fmt_cell(v) for v in row if v not in (None, "")) for row in grid]
            lines = [x for x in lines if x.strip()][:MAX_TEXT_ROWS]
            if lines:
                if multi:
                    toc.append((1, name, len(pages) + 1))
                pages.append(Page(number=len(pages) + 1, paragraphs=[Paragraph(name, "heading")] * multi +
                                  [Paragraph(x, "text") for x in lines]))
            continue
        for k, b in enumerate(blocks):
            b.source = src + (f", table {k + 1}" if len(blocks) > 1 else "")
            heading = (f"{name}: " if multi else "") + (b.title or f"table {k + 1}")
            toc.append((1, heading[:200], len(pages) + 1))
            pages.append(Page(number=len(pages) + 1,
                              paragraphs=[Paragraph(heading[:200], "heading"), Paragraph(b.render(), "table")]))
            tables.append(b)
            table_pages.append(len(pages))
    return ParsedDoc(path=path, pages=pages, toc=toc, tables=tables, table_pages=table_pages, kind="data")
