"""Layout-aware text extraction for Federal Reserve PDFs.

Plain ``page.get_text()`` has three problems on this corpus:

* table cells come out one per line, so a row such as
  ``Common equity tier 1 capital ratio 12.8 12.7 11.2`` loses its alignment;
* chart axis labels (``280``, ``2.5``, ``1998 2002 2006 ...``) are interleaved
  with real prose;
* running headers/footers and page numbers repeat on every page.

PyMuPDF already groups each table row into one block whose *lines* share a
baseline, so we rebuild visual rows per block, join far-apart cells with
`` | `` and then filter chart noise and page furniture. Blocks are read in the
PDF content-stream order, which for these publications follows the reading
order (prose column first, then figure/table elements).
"""

from __future__ import annotations

import re
import statistics
import unicodedata
from collections import Counter
from dataclasses import dataclass, field

import fitz  # PyMuPDF

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class Row:
    """One visual line of text on a page."""

    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    size: float  # dominant font size
    bold: bool
    is_table: bool  # row was assembled from several horizontally separated cells
    block: int  # source block index (rows of one block stay together)

    @property
    def height(self) -> float:
        return max(self.y1 - self.y0, 1.0)


@dataclass
class Paragraph:
    text: str
    kind: str  # "text" | "heading" | "table"
    size: float = 0.0


@dataclass
class Page:
    number: int  # 1-based page number
    rows: list[Row] = field(default_factory=list)
    paragraphs: list[Paragraph] = field(default_factory=list)

    @property
    def text(self) -> str:
        """Cleaned page text, paragraphs separated by blank lines."""
        return "\n\n".join(p.text for p in self.paragraphs)


@dataclass
class ParsedPDF:
    path: str
    pages: list[Page]
    toc: list[tuple[int, str, int]]  # (level, title, 1-based page)
    body_size: float


# ---------------------------------------------------------------------------
# Text normalisation helpers
# ---------------------------------------------------------------------------

_LIGATURES = {
    "\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl", "\ufb03": "ffi", "\ufb04": "ffl",
    "\u00ad": "",  # soft hyphen
    "\u00a0": " ", "\u2002": " ", "\u2003": " ", "\u2009": " ", "\u202f": " ", "\t": " ",
    "\u2212": "-",  # minus sign
    "\u2011": "-",  # non-breaking hyphen
}
_LIG_RE = re.compile("|".join(map(re.escape, _LIGATURES)))


def normalize_text(s: str) -> str:
    s = _LIG_RE.sub(lambda m: _LIGATURES[m.group(0)], s)
    s = unicodedata.normalize("NFC", s)
    return re.sub(r"[ ]{2,}", " ", s)


_MONTHS = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)\.?"
_NUMERIC_TOKEN = re.compile(
    rf"^(?:[-+]?[$]?\(?[-+]?\d[\d,]*(?:\.\d+)?\)?%?|{_MONTHS}|q[1-4]|h[1-2]|\d{{4}}:q[1-4]|\d{{4}}:h[1-2]|[-–—+=|]|n\.?a\.?)$",
    re.IGNORECASE,
)
# Unit / axis captions that appear as stand-alone labels inside charts.
_CHART_WORDS = {
    "percent", "percentage", "points", "basis", "billions", "millions", "trillions", "of", "dollars",
    "ratio", "index", "monthly", "weekly", "daily", "quarterly", "annual", "average", "median",
    "rate", "thousands", "jobs", "level", "change", "share", "number", "scale", "log", "right",
    "left", "years", "months", "z-score", "standard", "deviations", "%", "=", "nsa", "sa", "saar",
}


def is_numeric_noise(text: str) -> bool:
    """True if a row looks like chart axis ticks / labels rather than content."""
    toks = re.split(r"[\s|]+", text.strip().lower())
    toks = [t for t in toks if t]
    if not toks:
        return True
    if all(_NUMERIC_TOKEN.match(t) for t in toks):
        return True
    if len(toks) <= 4 and all(_NUMERIC_TOKEN.match(t) or t.strip("().,:") in _CHART_WORDS for t in toks):
        return True
    return False


# ---------------------------------------------------------------------------
# Row reconstruction
# ---------------------------------------------------------------------------

_CELL_GAP = 7.0  # points; a wider horizontal gap between lines on one baseline => new cell


def _span_text(span: dict, line_size: float) -> str:
    t = span["text"]
    # Drop superscript footnote markers ("assets1 (billions" -> "assets (billions").
    if span.get("flags", 0) & 1 and re.fullmatch(r"\s*\d{1,3}\s*", t) and span["size"] < line_size * 0.85:
        return ""
    return t


def _block_rows(block: dict, block_idx: int) -> list[Row]:
    """Group the lines of one text block into visual rows."""
    items = []
    for line in block.get("lines", []):
        spans = [s for s in line.get("spans", []) if s["text"].strip()]
        if not spans:
            continue
        sizes = Counter()
        for s in spans:
            sizes[round(s["size"], 1)] += len(s["text"])
        size = sizes.most_common(1)[0][0]
        text = "".join(_span_text(s, size) for s in line["spans"])
        text = normalize_text(text).strip()
        if not text:
            continue
        bold_chars = sum(len(s["text"]) for s in spans if (s.get("flags", 0) & 16) or re.search(r"bold|demi|-dm|heavy|black", s["font"], re.I))
        x0, y0, x1, y1 = line["bbox"]
        items.append((x0, y0, x1, y1, text, size, bold_chars > 0.6 * sum(len(s["text"]) for s in spans)))

    # Cluster lines whose vertical extent overlaps strongly -> same visual row.
    items.sort(key=lambda it: ((it[1] + it[3]) / 2, it[0]))
    clusters: list[list[tuple]] = []
    for it in items:
        if clusters:
            last = clusters[-1]
            ly0 = min(i[1] for i in last)
            ly1 = max(i[3] for i in last)
            overlap = min(ly1, it[3]) - max(ly0, it[1])
            if overlap > 0.5 * min(ly1 - ly0, it[3] - it[1]):
                last.append(it)
                continue
        clusters.append([it])

    rows = []
    for cl in clusters:
        cl.sort(key=lambda i: i[0])
        parts = [cl[0][4]]
        is_table = False
        for prev, cur in zip(cl, cl[1:]):
            gap = cur[0] - prev[2]
            if gap > _CELL_GAP:
                parts.append(" | ")
                is_table = True
            else:
                parts.append(" ")
            parts.append(cur[4])
        size = statistics.median(i[5] for i in cl)
        rows.append(
            Row(
                text="".join(parts).strip(),
                x0=min(i[0] for i in cl),
                y0=min(i[1] for i in cl),
                x1=max(i[2] for i in cl),
                y1=max(i[3] for i in cl),
                size=size,
                bold=all(i[6] for i in cl),
                is_table=is_table,
                block=block_idx,
            )
        )
    return rows


# ---------------------------------------------------------------------------
# Page furniture detection
# ---------------------------------------------------------------------------

_TOP_BAND = 62.0
_BOTTOM_BAND = 735.0


def _furniture_key(text: str) -> str:
    """Normalise header/footer text so that it repeats across pages."""
    t = re.sub(r"\d+", "#", text.lower())
    t = re.sub(r"\b[ivxlc]+\b", "#", t)  # roman page numbers
    return re.sub(r"[\s|]+", " ", t).strip()


def _find_furniture(pages_rows: list[list[Row]], page_height: float) -> set[str]:
    counts: Counter[str] = Counter()
    for rows in pages_rows:
        seen = set()
        for r in rows:
            if r.y1 <= _TOP_BAND or r.y0 >= page_height - (792 - _BOTTOM_BAND):
                k = _furniture_key(r.text)
                if k and k not in seen:
                    counts[k] += 1
                    seen.add(k)
    n = max(len(pages_rows), 1)
    return {k for k, c in counts.items() if c >= max(3, 0.15 * n)}


# ---------------------------------------------------------------------------
# Paragraph assembly
# ---------------------------------------------------------------------------

_SENT_END = re.compile(r"[.!?:;\"”)\]]$")


def _join_lines(lines: list[str]) -> str:
    out = ""
    for ln in lines:
        if not out:
            out = ln
        elif re.search(r"[A-Za-z]-$", out) and ln[:1].islower():
            out = out[:-1] + ln  # de-hyphenate "pros-" + "pects"
        else:
            out += " " + ln
    return out


_NOT_HEADING = re.compile(r"^(figure|table|source|note|notes|chart|exhibit)\b", re.I)


def _is_heading(rows: list[Row], text: str, body_size: float) -> bool:
    if len(text) > 160 or len(rows) > 3 or _NOT_HEADING.match(text):
        return False
    if text.endswith((".", ",", ";")) and not text.endswith("etc."):
        return False
    size = statistics.median(r.size for r in rows)
    return size >= body_size + 1.5 or (all(r.bold for r in rows) and size >= body_size - 0.6)


def rows_to_paragraphs(rows: list[Row], body_size: float) -> list[Paragraph]:
    """Assemble rows into typed paragraphs. Table rows are kept one per line."""
    paras: list[Paragraph] = []
    cur: list[Row] = []
    prev: Row | None = None

    def flush():
        nonlocal cur
        if cur:
            text = _join_lines([r.text for r in cur])
            kind = "heading" if _is_heading(cur, text, body_size) else "text"
            paras.append(Paragraph(text=text, kind=kind, size=statistics.median(r.size for r in cur)))
            cur = []

    for r in rows:
        if r.is_table:
            flush()
            if paras and paras[-1].kind == "table" and prev is not None and prev.is_table:
                paras[-1].text += "\n" + r.text
            else:
                paras.append(Paragraph(text=r.text, kind="table", size=r.size))
            prev = r
            continue
        new_para = prev is None or prev.is_table
        if prev is not None and not new_para:
            gap = r.y0 - prev.y1
            if r.block != prev.block and (gap > 0.9 * prev.height or gap < -2):
                new_para = True  # vertical whitespace or a jump to another column/region
            elif abs(r.size - prev.size) > 0.6 or r.bold != prev.bold:
                new_para = True  # heading / caption boundary
            elif r.x0 > prev.x0 + 12 and _SENT_END.search(prev.text):
                new_para = True  # indented first line
        if new_para:
            flush()
        cur.append(r)
        prev = r
    flush()
    return [p for p in paras if p.text.strip()]


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def parse_pdf(path: str) -> ParsedPDF:
    doc = fitz.open(path)
    page_height = doc[0].rect.height if len(doc) else 792.0

    raw_pages: list[list[Row]] = []
    size_hist: Counter[float] = Counter()
    for page in doc:
        d = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE | fitz.TEXT_MEDIABOX_CLIP)
        rows: list[Row] = []
        for bi, block in enumerate(d["blocks"]):
            if block.get("type") != 0:
                continue
            rows.extend(_block_rows(block, bi))
        for r in rows:
            size_hist[r.size] += len(r.text)
        raw_pages.append(rows)

    body_size = size_hist.most_common(1)[0][0] if size_hist else 10.0
    furniture = _find_furniture(raw_pages, page_height)

    pages: list[Page] = []
    for i, rows in enumerate(raw_pages):
        # A block made only of numeric/unit rows is a chart axis; a numeric row that
        # shares its block with real text (e.g. a "2025:Q4" table header) is kept.
        noisy_blocks = {
            b for b in {r.block for r in rows}
            if all(is_numeric_noise(r.text) for r in rows if r.block == b)
        }
        kept: list[Row] = []
        for r in rows:
            in_margin = r.y1 <= _TOP_BAND or r.y0 >= page_height - (792 - _BOTTOM_BAND)
            if in_margin and (_furniture_key(r.text) in furniture or len(r.text) <= 6 or is_numeric_noise(r.text)):
                continue
            if r.y1 <= _TOP_BAND and len(r.text) < 90:
                continue  # running header that did not repeat often enough (short docs)
            if r.block in noisy_blocks and not r.is_table:
                continue  # chart axis ticks / unit captions
            if len(r.text) <= 2 and not r.text.isalnum():
                continue
            kept.append(r)
        kept = _drop_chart_axis_rows(kept)
        pages.append(Page(number=i + 1, rows=kept, paragraphs=rows_to_paragraphs(kept, body_size)))

    toc = [(lvl, normalize_text(title).strip(), pno) for lvl, title, pno in doc.get_toc(simple=True)]
    return ParsedPDF(path=path, pages=pages, toc=toc, body_size=body_size)


def _drop_chart_axis_rows(rows: list[Row]) -> list[Row]:
    """Remove purely numeric table-like rows that are not adjacent to labelled rows.

    Within a real table, numeric-only rows (e.g. a header of years) sit next to
    rows that carry text labels. Chart axes produce runs of numeric-only rows.
    """
    out = []
    n = len(rows)
    for i, r in enumerate(rows):
        if r.is_table and is_numeric_noise(r.text):
            neighbours = rows[max(0, i - 2): i] + rows[i + 1: i + 3]
            if not any(nb.is_table and not is_numeric_noise(nb.text) for nb in neighbours):
                continue
        out.append(r)
    return out
