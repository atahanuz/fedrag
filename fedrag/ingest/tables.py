"""Turn messy spreadsheet-style grids into clean, queryable tables.

The same code handles an Excel sheet, a CSV file, an HTML table and a Word table. Real publications
are rarely tidy: a sheet starts with title, unit and source lines, headers span several rows and merged
cells ("2022" over "Income" over "Median | Mean"), row groups are introduced by label-only rows
("Percentile of income"), chart sheets and blank spacer rows sit in between, and several tables can be
stacked on one sheet. ``extract_tables`` finds each table block and returns its title, notes, header
levels and data rows; ``TableBlock.frame`` turns it into a DataFrame for SQL:

* one header row -> a **wide** table, one column per header (``period`` added when the row labels are
  dates or quarters, normalised to ``2003-Q1``, ``2013-06``, ``2003-03-01`` or ``2003``);
* several header rows -> a **long** table with one row per cell:
  ``row_group, row_label, h1..hN (header levels, top to bottom), value, value_text``, which keeps every
  dimension of a cross-tab filterable in SQL.
"""

from __future__ import annotations

import datetime as dt
import math
import re
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

Cell = Any

_NA = {"#n/a", "n/a", "na", "n.a.", "n.a", "nan", "none", "null", "—", "–", "-", "--", "...", "…", "*", "**",
       "(*)", "x", "s", ".", "#value!", "#div/0!", "#ref!", "(d)", "(x)", "(na)", "n.s.", "†", "‡"}
_NUM = re.compile(r"^\(?[-+−]?\$?\s?(\d{1,3}(,\d{3})+|\d+)?(\.\d+)?\)?\s?%?$")
_NOTE = re.compile(r"^(source|sources|note|notes)\b\s*[:.]", re.I)
_RANGE = re.compile(r"^[-−]?\d+(\.\d+)?\s?[–—-]\s?[-−]?\d+(\.\d+)?%?$|^\d+-\d/\d$")  # "1.3–1.8", "3-1/4"
_QUARTER = [
    (re.compile(r"^(\d{2}):Q([1-4])$"), lambda m: f"{_yy(m.group(1))}-Q{m.group(2)}"),
    (re.compile(r"^(\d{4})\s?[:\-]?\s?Q([1-4])$", re.I), lambda m: f"{m.group(1)}-Q{m.group(2)}"),
    (re.compile(r"^Q([1-4])\s?[:\-]?\s?(\d{4})$", re.I), lambda m: f"{m.group(2)}-Q{m.group(1)}"),
    (re.compile(r"^(\d{4})[-/](\d{2})$"), lambda m: f"{m.group(1)}-{m.group(2)}"),
    (re.compile(r"^(19\d\d|20\d\d)(0[1-9]|1[0-2])$"), lambda m: f"{m.group(1)}-{m.group(2)}"),
    (re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[ T]00:00:00)?$"), lambda m: f"{m.group(1)}-{m.group(2)}-{m.group(3)}"),
    (re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$"),
     lambda m: f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"),
    (re.compile(r"^(18|19|20)\d\d$"), lambda m: m.group(0)),
]


def _yy(two: str) -> str:
    y = int(two)
    return str(2000 + y if y < 60 else 1900 + y)


# ---------------------------------------------------------------------------
# Cells
# ---------------------------------------------------------------------------


def clean_cell(v: Cell) -> Cell:
    """Normalise one raw cell: '' -> None, strip text, datetime -> ISO date, int-valued floats stay floats."""
    if v is None:
        return None
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, (int, float)):
        return None if isinstance(v, float) and math.isnan(v) else float(v)
    if isinstance(v, dt.datetime):
        return v.strftime("%Y-%m-%d") if v.time() == dt.time() else v.isoformat(sep=" ")
    if isinstance(v, dt.date):
        return v.isoformat()
    s = re.sub(r"\s+", " ", str(v).replace(" ", " ")).strip()
    return s or None


def to_number(v: Cell) -> float | None:
    if isinstance(v, float):
        return v
    if not isinstance(v, str):
        return None
    s = v.strip().replace("−", "-")
    if not s or not _NUM.match(s) or not re.search(r"\d", s):
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()").replace("$", "").replace(",", "").replace("%", "").strip()
    try:
        x = float(s)
    except ValueError:
        return None
    return -x if neg else x


def is_na(v: Cell) -> bool:
    return isinstance(v, str) and v.strip().lower() in _NA


def is_value(v: Cell, ranges: bool = False) -> bool:
    """A data value: a number, a numeric string or a not-available marker; with ``ranges`` also a range
    such as '1.3–1.8' (in spreadsheets '25–49.9' is usually a column label, so ranges are opt-in)."""
    if v is None:
        return False
    if isinstance(v, float):
        return True
    return to_number(v) is not None or is_na(v) or (ranges and bool(_RANGE.match(v.strip())))


def period_of(v: Cell) -> str | None:
    """Normalised period for date-like labels ('03:Q1' -> '2003-Q1', 201306 -> '2013-06'), else None."""
    if v is None:
        return None
    if isinstance(v, float):
        if v.is_integer() and (1800 <= v <= 2100 or 180001 <= v <= 210012):
            v = str(int(v))
        else:
            return None
    s = str(v).strip()
    for pat, fmt in _QUARTER:
        m = pat.match(s)
        if m:
            return fmt(m)
    return None


def _year_like(v: Cell) -> bool:
    return isinstance(v, float) and v.is_integer() and 1800 <= v <= 2100


def fmt_cell(v: Cell) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        if v.is_integer() and abs(v) < 1e15:
            return str(int(v))
        return f"{v:.6g}" if abs(v) >= 1e-4 else f"{v:.3g}"
    return str(v)


# ---------------------------------------------------------------------------
# Table blocks
# ---------------------------------------------------------------------------


@dataclass
class TableBlock:
    title: str
    notes: list[str]
    header: list[list[Cell]]  # header rows, horizontally filled, over columns [c0, c1)
    rows: list[list[Cell]]  # data rows over the same columns
    groups: list[str | None]  # row group of each data row
    c0: int  # first column of the block in the source grid
    label_cols: int  # leading columns that hold row labels (crosstab) - 0 for record tables
    source: str = ""  # e.g. 'sheet "Page 3 Data"' or 'HTML table 3'

    @property
    def n_cols(self) -> int:
        return max((len(r) for r in self.rows), default=0)

    @property
    def layout(self) -> str:
        return "long" if len(self.header) >= 2 else "wide"

    # ------------------------------------------------------------------ naming
    def column_labels(self) -> list[str]:
        labels = []
        for j in range(self.n_cols):
            parts: list[str] = []
            for h in self.header:
                v = fmt_cell(h[j]) if j < len(h) else ""
                if v and (not parts or parts[-1] != v):
                    parts.append(v)
            labels.append(" | ".join(parts))
        return labels

    def label_header(self) -> str:
        labs = self.column_labels()
        names = [labs[j] for j in range(self.label_cols) if labs[j]]
        return " / ".join(names) or ("period" if self.periodic() else "label")

    def periodic(self) -> bool:
        """Do the row labels look like dates or quarters (a time series)?"""
        if not self.rows:
            return False
        col = [r[0] if r else None for r in self.rows]
        hits = sum(period_of(v) is not None for v in col if v is not None)
        return hits >= max(3, 0.8 * sum(v is not None for v in col))

    # ------------------------------------------------------------------ data frames
    def frame(self) -> pd.DataFrame:
        self._qualify_repeats()
        return self._long() if self.layout == "long" else self._wide()

    def _qualify_repeats(self) -> None:
        """A label that repeats ('June projection' under each variable) is ambiguous: prefix it with the
        closest preceding label that does not repeat. Labels repeated per row group (every survey year
        lists 'Wages') have no unique anchor and stay as they are."""
        if not self.rows or self.periodic() or self.label_cols != 1:
            return
        counts: dict = {}
        for r in self.rows:
            counts[r[0]] = counts.get(r[0], 0) + 1
        anchor = None
        for r in self.rows:
            if r[0] is None:
                continue
            if counts[r[0]] == 1:
                anchor = r[0]
            elif anchor is not None and isinstance(r[0], str) and not str(r[0]).startswith(f"{anchor} | "):
                r[0] = f"{anchor} | {r[0]}"

    def _wide(self) -> pd.DataFrame:
        labels = self.column_labels()
        lone = not self.header and self.n_cols == max(self.label_cols, 1) + 1  # one unlabelled series
        names = sql_names([lab or ("label" if j < max(self.label_cols, 1) else ("value" if lone else f"col_{j + 1}"))
                           for j, lab in enumerate(labels)])
        data: dict[str, list] = {}
        if any(self.groups):
            data["row_group"] = list(self.groups)
        periods = self._periods(names)
        if periods is not None:
            data["period"] = periods
        for j, name in enumerate(names):
            col = [r[j] if j < len(r) else None for r in self.rows]
            non_empty = [v for v in col if v is not None and not is_na(v)]
            if name in data:
                name = f"{name}_raw"
            if non_empty and all(to_number(v) is not None for v in non_empty) and \
                    not (j == 0 and periods is not None and labels[0] == ""):
                data[name] = [to_number(v) if v is not None and not is_na(v) else None for v in col]
            else:
                data[name] = [fmt_cell(v) if v is not None else None for v in col]
        return pd.DataFrame(data)

    def _periods(self, names: list[str]) -> list[str | None] | None:
        """A normalised ``period`` column for time series: from a 'year' + 'quarter' column pair or from
        date-like row labels in the first column."""
        if len(names) >= 2 and re.fullmatch(r"(survey_)?year", names[0]) and re.fullmatch(r"(survey_)?quarter",
                                                                                            names[1]):
            out = []
            for r in self.rows:
                y, q = to_number(r[0]), to_number(r[1]) if len(r) > 1 else None
                out.append(f"{int(y)}-Q{int(q)}" if y is not None and q is not None and 1 <= q <= 4 else None)
            return out
        if self.periodic():
            return [period_of(r[0]) if r else None for r in self.rows]
        return None

    def _long(self) -> pd.DataFrame:
        k = len(self.header)
        out: dict[str, list] = {"row_group": [], "row_label": [], **{f"h{i + 1}": [] for i in range(k)},
                                "value": [], "value_text": []}
        lc = max(self.label_cols, 1)
        for r, g in zip(self.rows, self.groups):
            label = " | ".join(fmt_cell(v) for v in r[:lc] if v is not None)
            for j in range(lc, len(r)):
                v = r[j]
                if v is None:
                    continue
                heads = [fmt_cell(h[j]) if j < len(h) else "" for h in self.header]
                if not any(heads):
                    continue
                out["row_group"].append(g)
                out["row_label"].append(label)
                for i in range(k):
                    out[f"h{i + 1}"].append(heads[i] or None)
                out["value"].append(None if is_na(v) else to_number(v))
                out["value_text"].append(fmt_cell(v))
        df = pd.DataFrame(out)
        if not any(self.groups):
            df = df.drop(columns=["row_group"])
        return df

    # ------------------------------------------------------------------ text
    def render(self, max_rows: int = 40, meta: bool = True) -> str:
        """The block as text: title and notes (``meta``), header rows, then the rows as 'a | b | c' lines.
        A long table shows its first 5 and last ``max_rows - 5`` rows (the latest periods of a series)."""
        lines = ([self.title] if self.title else []) + self.notes[:4] if meta else []
        for h in self.header:
            lines.append(" | ".join(fmt_cell(v) for v in h).strip(" |"))
        pairs = list(zip(self.rows, self.groups))
        if len(pairs) > max_rows:
            head, tail = pairs[:5], pairs[-(max_rows - 5):]
            shown = head + [(None, None)] + tail
        else:
            shown = pairs
        group = None
        for r, g in shown:
            if r is None:
                lines.append(f"... ({len(pairs) - len(shown) + 1} rows omitted)")
                continue
            if g and g != group:
                lines.append(f"[{g}]")
                group = g
            lines.append(" | ".join(fmt_cell(v) for v in r).rstrip(" |"))
        return "\n".join(x for x in lines if x.strip())


def sql_names(labels: list[str]) -> list[str]:
    """Unique snake_case SQL identifiers for column labels."""
    out: list[str] = []
    for lab in labels:
        n = re.sub(r"[^0-9a-zA-Z]+", "_", lab.lower().replace("%", " pct ")).strip("_")[:60] or "col"
        if n[0].isdigit():
            n = f"c_{n}"
        base, i = n, 2
        while n in out:
            n = f"{base}_{i}"
            i += 1
        out.append(n)
    return out


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def _fill_merged(grid: list[list[Cell]], merged: list[tuple[int, int, int, int]]) -> None:
    """Copy each merged range's top-left value into every cell of the range (0-based, inclusive)."""
    for r0, c0, r1, c1 in merged:
        if r0 >= len(grid):
            continue
        v = grid[r0][c0] if c0 < len(grid[r0]) else None
        if v is None:
            continue
        for r in range(r0, min(r1, len(grid) - 1) + 1):
            for c in range(c0, c1 + 1):
                if c < len(grid[r]) and grid[r][c] is None:
                    grid[r][c] = v


def _cells(row: list[Cell]) -> list[int]:
    return [j for j, v in enumerate(row) if v is not None]


def _is_data(row: list[Cell], first_value_col: int | None = None) -> bool:
    idx = _cells(row)
    if not idx:
        return False
    vals = [j for j in idx if is_value(row[j])]
    if first_value_col is not None:
        vals = [j for j in vals if j >= first_value_col]
    if not vals:
        return False
    # a header row of years (2001 ... 2004 ...) is not data: years over spanned columns leave gaps
    if all(_year_like(row[j]) for j in vals) and len(vals) >= 2 and not is_value(row[idx[0]]):
        return False
    if all(_year_like(row[j]) for j in idx) and len(idx) >= 3:
        return False
    return len(vals) >= max(1, 0.5 * len([j for j in idx if j >= (first_value_col or 0)]))


def extract_tables(grid: list[list[Cell]], merged: list[tuple[int, int, int, int]] | None = None,
                   header_rows: int | None = None, source: str = "") -> list[TableBlock]:
    """Find the table blocks in a grid of raw cells (rows of values; None for empty).

    ``header_rows`` forces the header depth (HTML tables say it with <th>/<thead>)."""
    g = [[clean_cell(v) for v in row] for row in grid]
    if merged:
        _fill_merged(g, merged)
    width = max((len(r) for r in g), default=0)
    g = [r + [None] * (width - len(r)) for r in g]
    # drop rows that only hold navigation text
    g = [[None if isinstance(v, str) and v.lower().startswith("return to table of contents") else v for v in r]
         for r in g]
    if header_rows is not None:
        return _forced(g, header_rows, source)

    blocks: list[TableBlock] = []
    i, n = 0, len(g)
    while i < n:
        d = next((k for k in range(i, n) if _is_data(g[k])), None)
        if d is None:
            if blocks and any(_cells(r) for r in g[i:]):  # trailing notes belong to the last block
                blocks[-1].notes += [" ".join(fmt_cell(r[j]) for j in _cells(r)) for r in g[i:] if _cells(r)]
            break
        # label columns: leading columns that hold text (or dates/periods) in the data rows that follow
        first = min(_cells(g[d]))
        value_cols = [j for j in _cells(g[d]) if is_value(g[d][j])]
        fvc = min(value_cols)
        if fvc == first and len(value_cols) > 1 and _period_column(g, d, first):
            fvc = value_cols[1]  # e.g. 201306 or 2003 in the first column labels the row
        # 'Source: ...' / 'Note: ...' cells above the data are notes, whatever column they sit in
        pre_notes = []
        for r in range(i, d):
            for j in _cells(g[r]):
                if isinstance(g[r][j], str) and _NOTE.match(g[r][j]) and len(_cells(g[r])) > 1:
                    pre_notes.append(g[r][j])
                    g[r][j] = None
        # header rows: the non-blank rows directly above the first data row with a cell over a value column.
        # A label-only row right above the data is the first row group ("1989 Survey of Consumer Finances");
        # a long note spanning the value columns ("Warning! Because ...") is skipped.
        h_start, k, gap, lead_group, skipped = d, d - 1, 0, None, set()
        while k >= i:
            cells = _cells(g[k])
            if not cells:
                gap += 1
                if gap > 2 or (gap > 1 and h_start < d):
                    break
                k -= 1
                continue
            distinct = list(dict.fromkeys(str(g[k][j]) for j in cells))
            if len(distinct) == 1 and min(cells) < fvc:  # a caption in the label column
                if h_start == d and lead_group is None and len(distinct[0]) <= 100:
                    lead_group, gap = distinct[0], 0
                    skipped.add(k)
                    k -= 1
                    continue
                break
            if len(distinct) == 1 and len(distinct[0]) > 60:  # a note line over the value columns
                skipped.add(k)
                k -= 1
                continue
            if any(j >= fvc for j in cells) and not any(isinstance(g[k][j], str) and len(g[k][j]) > 120
                                                       for j in cells):
                h_start, gap = k, 0
                k -= 1
                continue
            break
        if lead_group and h_start == d:  # no header above: the caption was a title, not a group
            lead_group = None
            skipped.clear()
        header_idx = [r for r in range(h_start, d) if _cells(g[r]) and r not in skipped]
        title_idx = [r for r in range(i, h_start) if _cells(g[r])]
        header_notes = pre_notes + [str(g[r][j]) for r in skipped for j in _cells(g[r])[:1]
                                    if str(g[r][j]) != lead_group]
        # 'Source: ...' / 'Note: ...' cells inside header rows are notes, not column labels
        for r in header_idx:
            for j in _cells(g[r]):
                if isinstance(g[r][j], str) and _NOTE.match(g[r][j]):
                    header_notes.append(g[r][j])
                    g[r][j] = None
        header_idx = [r for r in header_idx if _cells(g[r])]
        # data rows, group rows and blank spacers until the table ends
        rows, groups, group, end, blanks = [], [], lead_group, d, 0
        k = d
        while k < n:
            row = g[k]
            cells = _cells(row)
            if not cells:
                blanks += 1
                if blanks >= 3:
                    break
                k += 1
                continue
            if _is_data(row, fvc):
                rows.append(row)
                groups.append(group)
                end, blanks = k + 1, 0
                k += 1
                continue
            # a label-only row followed by more data starts a row group
            nxt = next((m for m in range(k + 1, min(k + 4, n)) if _cells(g[m])), None)
            label_only = all(j < fvc for j in cells) and len(cells) <= 2
            if label_only and nxt is not None and _is_data(g[nxt], fvc) and len(str(row[cells[0]])) <= 100:
                group = " ".join(fmt_cell(row[j]) for j in cells)
                k += 1
                continue
            break
        if not rows:
            i = d + 1
            continue
        # trailing note lines (single text cells) directly after the table
        notes_after, m = [], end
        while m < n:
            cells = _cells(g[m])
            if not cells:
                m += 1
                if m < n and not _cells(g[m]):
                    break
                continue
            if len(cells) <= 2 and not any(is_value(g[m][j]) for j in cells) and not _starts_table(g, m):
                notes_after.append(" ".join(fmt_cell(g[m][j]) for j in cells))
                m += 1
                continue
            break
        c0 = min([first] + [min(_cells(g[r])) for r in header_idx])
        c1 = max(max(_cells(r)) for r in rows + [g[x] for x in header_idx]) + 1
        # drop columns that are empty in every data row (spacers between column groups)
        keep = [j for j in range(c0, c1) if j < fvc or any(r[j] is not None for r in rows)]
        header = [_hfill([g[r][j] for j in keep], last=(r == header_idx[-1])) for r in header_idx]
        header = _hfill_levels(header)
        title_lines = []
        for r in title_idx:
            parts = list(dict.fromkeys(fmt_cell(g[r][j]) for j in _cells(g[r])))
            header_notes += [x for x in parts[1:] if _NOTE.match(x)]
            title_lines.append(" ".join(x for x in parts if x == parts[0] or not _NOTE.match(x)))
        title, notes = _split_title(title_lines)
        label_cols = len([j for j in keep if j < fvc])
        blocks.append(TableBlock(title=title, notes=notes + header_notes + notes_after, header=header,
                                 rows=[[r[j] for j in keep] for r in rows], groups=groups, c0=c0,
                                 label_cols=label_cols, source=source))
        i = max(m, end)
    blocks = [b for b in blocks if b.rows]
    for prev, b in zip(blocks, blocks[1:]):  # a header-less continuation ("MEMO" rows) shares the header above
        if not b.header and prev.header and prev.n_cols == b.n_cols and prev.label_cols == b.label_cols:
            b.header = [list(h) for h in prev.header]
            b.title = f"{prev.title} (continued: {b.title})" if b.title else prev.title
    return blocks


def _title_like(row: list[Cell], cells: list[int], fvc: int) -> bool:
    """A caption or note line rather than a header level: one distinct value (merged cells repeat it)
    that starts in the label column or is a sentence."""
    distinct = {str(row[j]) for j in cells}
    if len(distinct) != 1:
        return False
    text = next(iter(distinct))
    return min(cells) < fvc or len(text) > 60


def _period_column(g: list[list[Cell]], d: int, col: int) -> bool:
    """Does column ``col`` hold period labels (2003, 201306, 2003-03-01) in the rows from ``d`` on?"""
    vals = [g[k][col] for k in range(d, min(d + 12, len(g))) if g[k][col] is not None]
    return len(vals) >= 2 and all(period_of(v) is not None for v in vals) and len({str(v) for v in vals}) > 1


def _starts_table(g: list[list[Cell]], k: int) -> bool:
    nxt = next((m for m in range(k + 1, min(k + 4, len(g))) if _cells(g[m])), None)
    return nxt is not None and len(_cells(g[nxt])) >= 2 and not _is_data(g[k])


def _split_title(lines: list[str]) -> tuple[str, list[str]]:
    """The first line that reads like a title (not 'Source:', 'Note:', a copyright line or a paragraph)."""
    for i, line in enumerate(lines):
        if not re.match(r"^(source|sources|note|notes|©|\*|return to)", line.strip(), re.I) and len(line) <= 200:
            return line, lines[:i] + lines[i + 1:]
    return "", lines


def _hfill(row: list[Cell], last: bool) -> list[Cell]:
    """Spread a spanning header label ('2022' over four columns) to the right, except on the last row."""
    if last:
        return list(row)
    out, cur = [], None
    for v in row:
        if v is not None:
            cur = v
        out.append(cur)
    return out


def _hfill_levels(header: list[list[Cell]]) -> list[list[Cell]]:
    """An upper label never spans past the end of the label above it: refill per parent span."""
    if len(header) < 2:
        return header
    out = [header[0]]
    for row in header[1:]:
        parent = out[-1]
        filled, cur, cur_parent = [], None, object()
        for j, v in enumerate(row):
            if parent[j] != cur_parent:
                cur, cur_parent = None, parent[j]
            if v is not None:
                cur = v
            filled.append(v if v is not None else (cur if row is not header[-1] else None))
        out.append(filled)
    return out


def _forced(g: list[list[Cell]], header_rows: int, source: str) -> list[TableBlock]:
    rows = [r for r in g if _cells(r)]
    if len(rows) <= header_rows:
        return []
    header = [_hfill(r, last=(i == header_rows - 1)) for i, r in enumerate(rows[:header_rows])]
    header = _hfill_levels(header)
    body = rows[header_rows:]
    # leading label columns: columns whose body cells are mostly text
    label_cols = 0
    for j in range(len(body[0])):
        col = [r[j] for r in body if r[j] is not None]
        if col and sum(is_value(v, ranges=True) for v in col) < 0.5 * len(col):
            label_cols += 1
        else:
            break
    groups, data, group = [], [], None
    for r in body:
        cells = _cells(r)
        spanning = len(cells) > 1 and len({str(r[j]) for j in cells}) == 1  # one cell with a full colspan
        if cells and (spanning or (label_cols and all(j < label_cols for j in cells) and len(body[0]) > label_cols)):
            group = fmt_cell(r[cells[0]]) if spanning else " ".join(fmt_cell(r[j]) for j in cells)
            continue
        data.append(r)
        groups.append(group)
    return [TableBlock(title="", notes=[], header=header, rows=data, groups=groups, c0=0,
                       label_cols=label_cols, source=source)] if data else []


# ---------------------------------------------------------------------------
# Cards: what retrieval and the agents see about a table
# ---------------------------------------------------------------------------


@dataclass
class TableInfo:
    """Catalog entry of one SQL table."""

    table: str  # SQL name
    doc_id: str
    title: str
    source: str
    layout: str
    n_rows: int
    columns: list[dict] = field(default_factory=list)  # name, label, type, examples
    notes: list[str] = field(default_factory=list)
    row_labels: list[str] = field(default_factory=list)
    header_values: dict[str, list[str]] = field(default_factory=dict)
    part: int = 1  # virtual page of the document that shows this table
    about: str = ""  # data dictionary of the source file (metadata.csv description)

    def periodic_labels(self) -> bool:
        return len(self.row_labels) >= 6 and sum(period_of(x) is not None for x in self.row_labels) >= 0.8 * len(
            self.row_labels)

    def card(self, doc_title: str, doc_date: str, description: str = "") -> str:
        cols = "; ".join(
            f"{c['name']}" + (f" ({c['label']})" if c.get("label") and c["label"] != c["name"] else "")
            + f" [{c['type']}" + (f", e.g. {', '.join(c['examples'][:3])}" if c.get("examples") else "") + "]"
            for c in self.columns)
        parts = [f"Data table `{self.table}`: {self.title or '(untitled)'}",
                 f"From: {doc_title} ({doc_date}), {self.source}",
                 f"Shape: {self.n_rows} rows, layout {self.layout}"
                 + (" (one row per cell: row_label x header levels h1..hN -> value)" if self.layout == "long" else ""),
                 f"Columns: {cols}"]
        if description:
            parts.append(f"About the data: {description}")
        if self.notes:
            parts.append("Notes: " + " ".join(self.notes)[:600])
        if self.row_labels and self.periodic_labels():
            parts.append(f"Periods: {self.row_labels[0]} to {self.row_labels[-1]} ({len(self.row_labels)} rows)")
        elif self.row_labels:
            parts.append("Row labels: " + "; ".join(self.row_labels[:60]))
        for h, vals in self.header_values.items():
            parts.append(f"{h} values: " + "; ".join(vals[:40]))
        return "\n".join(parts)


def describe_frame(df: pd.DataFrame, block: TableBlock) -> tuple[list[dict], list[str], dict[str, list[str]]]:
    """Column catalog (name, label, type, examples), distinct row labels and header values."""
    labels = dict(zip(sql_names(block.column_labels()), block.column_labels())) if block.layout == "wide" else {}
    cols = []
    for name in df.columns:
        s = df[name].dropna()
        typ = "number" if pd.api.types.is_numeric_dtype(df[name]) else "text"
        if typ == "number":
            ex = [fmt_cell(float(x)) for x in s.head(3)]
            if len(s):
                ex.append(f"range {fmt_cell(float(s.min()))} to {fmt_cell(float(s.max()))}")
        else:
            ex = [str(x)[:40] for x in pd.unique(s)[:4]]
        cols.append({"name": name, "label": labels.get(name, ""), "type": typ, "examples": ex})
    label_col = "row_label" if "row_label" in df.columns else ("period" if "period" in df.columns else next(
        (c["name"] for c in cols if c["type"] == "text" and c["name"] != "row_group"), None))
    labels = [str(x) for x in pd.unique(df[label_col].dropna())] if label_col else []
    row_labels = labels[:80] if len(labels) <= 80 else labels[:40] + labels[-40:]
    header_values = {h: [str(x) for x in pd.unique(df[h].dropna())][:60] for h in df.columns if re.fullmatch(r"h\d", h)}
    return cols, row_labels, header_values
