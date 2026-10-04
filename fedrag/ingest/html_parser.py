"""Federal Reserve web pages -> headings, paragraphs and tables in reading order.

federalreserve.gov uses two templates: ``#article`` (press releases, statements, speeches, testimony,
SR letters, SEP) and ``#content > div.row`` (FEDS Notes, SLOOS), where the article is the row with the
most text. Navigation, share buttons, video widgets, "Back to Top" and "Last Update" lines are removed,
footnote markers are dropped, and footnotes themselves are kept. Every ``<table>`` is expanded
(colspan/rowspan) into a grid: its text goes into the page as ``a | b | c`` rows and the grid becomes a
SQL table (``tables.extract_tables``); the caption is the table's ``<caption>`` or the heading or
"Table N." paragraph just before it.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from .document import Block, ParsedDoc, paginate
from .pdf_parser import normalize_text
from .tables import TableBlock, extract_tables, fmt_cell

HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
TEXT_BLOCKS = {"p", "li", "dd", "dt", "blockquote", "pre", "figcaption", "address", "summary", "caption"}
CONTAINERS = {"div", "section", "article", "main", "ul", "ol", "dl", "figure", "details", "aside", "header",
              "footer", "center", "tbody", "body", "form"}
DROP_TAGS = ["script", "style", "nav", "button", "noscript", "iframe", "svg", "video", "audio", "input", "select",
             "img", "picture", "source", "object", "embed", "map", "link", "meta"]
DROP_SELECTORS = [".breadcrumb", "[class*=share]", "#t4_nav", ".back-top", ".icon__backTop", ".lastUpdate",
                  "#lastUpdate", "[class*=video]", ".watchLive", ".shareDL", ".hidden-sm", ".modal", ".sr-only"]
JUNK = re.compile(
    r"^(share|watch live|back to top|return to text|return to top|accessible version|last update:?.*|print|rss|"
    r"twitter|facebook|linkedin|email|e-mail|pdf|html|accessible keys for video.*|\[space bar\].*|"
    r"related content|related information|please enable javascript.*|current survey|release dates|"
    r"announcements|about|data download program|ddp|home|current survey\b.*|table 1 \| table 2\b.*|"
    r"congressional hearing transcripts|hearing transcripts are posted.*)$", re.I)
RELATED = re.compile(r"^(related content|related information|related links)$", re.I)


def _text(el: Tag | NavigableString) -> str:
    raw = el.get_text(" ") if isinstance(el, Tag) else str(el)
    return normalize_text(re.sub(r"\s+", " ", raw)).strip()


def _root(soup: BeautifulSoup) -> Tag:
    art = soup.find(id="article")
    if art is not None and len(art.get_text(" ", strip=True)) > 200:
        return art
    content = soup.find(id="content") or soup.find("main") or soup.body or soup
    rows = content.find_all("div", class_="row", recursive=False) if isinstance(content, Tag) else []
    if rows:
        return max(rows, key=lambda r: len(r.get_text(" ", strip=True)))
    return content


def _clean(root: Tag) -> None:
    for t in root.find_all(DROP_TAGS):
        t.decompose()
    for sel in DROP_SELECTORS:
        for t in root.select(sel):
            t.decompose()
    for c in root.find_all(string=lambda s: isinstance(s, Comment)):
        c.extract()
    # footnote markers in running text ("...aggregates1" / "<a href='#fn1'><sup>1</sup></a>")
    for sup in root.find_all("sup"):
        if re.fullmatch(r"\s*[\d*†‡,\s]+\s*", sup.get_text() or ""):
            sup.decompose()
    for a in root.find_all("a"):
        if re.fullmatch(r"\s*(return to text|return to top)\s*", a.get_text() or "", re.I):
            a.decompose()
    # "Related Content" link lists at the end of speeches and press releases
    for h in root.find_all(list(HEADINGS)):
        if RELATED.match(_text(h)):
            for sib in list(h.find_next_siblings()):
                sib.decompose()
            h.decompose()


def table_grid(table: Tag) -> tuple[list[list[str | None]], int]:
    """Expand colspan/rowspan into a rectangular grid; also return the number of header rows."""
    rows = [tr for tr in table.find_all("tr") if tr.find_parent("table") is table]
    grid: list[list[str | None]] = []
    pending: dict[tuple[int, int], str] = {}  # cells covered by a rowspan from above
    header_rows, in_header = 0, True
    for r, tr in enumerate(rows):
        cells = tr.find_all(["th", "td"], recursive=False)
        row: list[str | None] = []
        c = 0
        for cell in cells:
            while (r, c) in pending:
                row.append(pending.pop((r, c)))
                c += 1
            txt = _text(cell) or None
            span = max(1, min(int(cell.get("colspan", 1) or 1), 50)) if str(cell.get("colspan", "1")).isdigit() else 1
            rspan = max(1, min(int(cell.get("rowspan", 1) or 1), 200)) if str(cell.get("rowspan", "1")).isdigit() else 1
            for k in range(span):
                row.append(txt)
                for rr in range(1, rspan):
                    pending[(r + rr, c)] = txt
                c += 1
        while (r, c) in pending:
            row.append(pending.pop((r, c)))
            c += 1
        is_head = tr.find_parent("thead") is not None or (cells and all(x.name == "th" for x in cells) and len(cells) > 1)
        if in_header and is_head and r < 6:
            header_rows += 1
        else:
            in_header = False
        grid.append(row)
    return grid, header_rows


def _walk(el: Tag, out: list[Block], tables: list[TableBlock], doc_tables: list[list], title: str) -> None:
    buf: list[str] = []

    def flush() -> None:
        txt = normalize_text(re.sub(r"\s+", " ", " ".join(buf))).strip()
        buf.clear()
        if txt and not JUNK.match(txt):
            out.append(Block("text", txt))

    for child in el.children:
        if isinstance(child, NavigableString):
            if not isinstance(child, Comment):
                buf.append(str(child))
            continue
        if not isinstance(child, Tag):
            continue
        name = child.name
        if name in HEADINGS:
            flush()
            txt = _text(child)
            if txt and not JUNK.match(txt):
                out.append(Block("heading", txt, level=HEADINGS[name]))
        elif name == "table":
            flush()
            _table(child, out, tables, doc_tables)
        elif name in TEXT_BLOCKS:
            flush()
            if child.find(["table", "ul", "ol", "p", "div"] + list(HEADINGS)):
                own = "".join(str(s) for s in child.find_all(string=True, recursive=False)).strip()
                if own:
                    out.append(Block("text", ("• " if name == "li" else "") + normalize_text(re.sub(r"\s+", " ", own))))
                _walk(child, out, tables, doc_tables, title)
            else:
                txt = _text(child)
                if txt and not JUNK.match(txt):
                    out.append(Block("text", ("• " if name == "li" else "") + txt))
        elif name in CONTAINERS:
            flush()
            _walk(child, out, tables, doc_tables, title)
        elif name == "br":
            flush()
        else:  # inline element
            buf.append(child.get_text(" "))
    flush()


def _table(t: Tag, out: list[Block], tables: list[TableBlock], doc_tables: list[list]) -> None:
    grid, n_head = table_grid(t)
    if not grid or not any(any(c for c in row) for row in grid):
        return
    cap = t.find("caption")
    caption = _text(cap) if cap else _caption(out)
    width = max(len(r) for r in grid)
    if width == 1 or len(grid) == 1:  # a layout table or a one-cell box: plain text
        for row in grid:
            line = " ".join(c for c in row if c)
            if line:
                out.append(Block("text", line))
        return
    lines = [" | ".join(fmt_cell(c) for c in row).strip(" |") for row in grid]
    text = "\n".join(x for x in lines if x.strip())
    blocks = extract_tables(grid, header_rows=n_head) if n_head else extract_tables(grid)
    idx = None
    for b in blocks:
        b.title = b.title or caption
        b.source = f"HTML table {len(doc_tables) + 1}"
        tables.append(b)
        idx = len(tables) - 1 if idx is None else idx
    doc_tables.append(grid)
    out.append(Block("table", (f"{caption}\n" if caption and not (out and out[-1].text == caption) else "") + text,
                     table=idx))


def _caption(out: list[Block]) -> str:
    """Caption from the blocks before a table: the nearest heading or 'Table N.' line, prefixed by the
    figure/table title above it when the nearest one is a sub-heading ('Figure 1. ... - Unemployment rate')."""
    near, k = "", len(out) - 1
    while k >= 0 and len(out) - k <= 4 and out[k].kind != "table":
        b = out[k]
        is_cap = b.kind == "heading" or (len(b.text) < 300 and re.match(r"^(table|figure|exhibit|chart)\b", b.text, re.I))
        if is_cap and not near:
            near = b.text
            if re.match(r"^(table|figure|exhibit|chart)\b", near, re.I):
                return near
        elif is_cap and re.match(r"^(table|figure|exhibit|chart)\b", b.text, re.I):
            return f"{b.text} - {near}"
        k -= 1
    return near


def parse_html(path: str, title: str = "") -> ParsedDoc:
    raw = open(path, "rb").read().decode("utf-8", errors="replace")
    soup = BeautifulSoup(raw, "lxml")
    root = _root(soup)
    _clean(root)
    blocks: list[Block] = []
    tables: list[TableBlock] = []
    _walk(root, blocks, tables, [], title)
    # de-duplicate consecutive repeats (templates sometimes repeat the title in hidden print headers)
    deduped: list[Block] = []
    for b in blocks:
        if deduped and b.text == deduped[-1].text:
            continue
        deduped.append(b)
    # heading levels relative to the tags used on this page (h3/h4/h5 -> 1/2/3)
    used = sorted({b.level for b in deduped if b.kind == "heading"})
    rank = {lvl: i + 1 for i, lvl in enumerate(used)}
    for b in deduped:
        if b.kind == "heading":
            b.level = rank[b.level]
    pages, toc, table_part = paginate(deduped)
    table_pages = [1] * len(tables)
    for ti, part in table_part.items():  # each grid's first block -> its part; later blocks share it
        for k in range(ti, len(tables)):
            table_pages[k] = part
    return ParsedDoc(path=path, pages=pages, toc=toc, tables=tables, table_pages=table_pages)
