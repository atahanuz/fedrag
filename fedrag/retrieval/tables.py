"""SQL access to every table in the collection (DuckDB, read-only and sandboxed).

The database is opened read-only with external access disabled and the configuration locked, so a
query can read the collection's tables and nothing else: no files, no network, no settings, no writes.
Each query runs on its own cursor with a timeout and a row cap.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

from .. import config

READ_ONLY_START = re.compile(r"^\s*(\(\s*)*(select|with|from|describe|summarize|pivot|unpivot|values|show)\b", re.I)
# the connection is read-only and locked anyway; this only turns write attempts into a clear message
FORBIDDEN = re.compile(r"\b(insert|update|delete|create|drop|alter|attach|detach|copy|export|import|install|load|"
                       r"pragma|call|checkpoint|vacuum)\b", re.I)


@dataclass
class QueryResult:
    sql: str
    columns: list[str] = field(default_factory=list)
    rows: list[tuple] = field(default_factory=list)
    truncated: bool = False
    tables: list[str] = field(default_factory=list)
    error: str | None = None

    def render(self, max_cell: int = 100) -> str:
        if self.error:
            return f"ERROR: {self.error}"
        if not self.rows:
            return "(no rows)"
        lines = [" | ".join(self.columns)]
        for r in self.rows:
            lines.append(" | ".join(_fmt(v, max_cell) for v in r))
        if self.truncated:
            lines.append("... (more rows not shown: add filters, aggregate or LIMIT/OFFSET)")
        return "\n".join(lines)


def _fmt(v, max_cell: int) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, float):
        return f"{v:.10g}"
    s = str(v)
    return s if len(s) <= max_cell else s[: max_cell - 1] + "…"


def _strip_comments_and_strings(sql: str) -> str:
    sql = re.sub(r"--[^\n]*", " ", sql)
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    return re.sub(r"'(?:[^']|'')*'", "''", sql)


class TableStore:
    def __init__(self, corpus_dir: Path = config.CORPUS_DIR):
        self.catalog: dict[str, dict] = {}
        self.by_doc: dict[str, list[str]] = {}
        cat_path, db_path = corpus_dir / "tables.jsonl", corpus_dir / "tables.duckdb"
        self.con = None
        if cat_path.exists() and db_path.exists():
            import duckdb

            for line in open(cat_path, encoding="utf-8"):
                t = json.loads(line)
                self.catalog[t["table"]] = t
                if t["kind"] == "table":
                    self.by_doc.setdefault(t["doc_id"], []).append(t["table"])
            self.con = duckdb.connect(str(db_path), read_only=True, config={
                "enable_external_access": False, "lock_configuration": True, "threads": 4,
                "memory_limit": "2GB"})
        self._names = sorted(self.catalog, key=len, reverse=True)
        self._lock = threading.Lock()  # cursors are created one at a time; each then runs on its own

    @property
    def available(self) -> bool:
        return self.con is not None

    def tables_in(self, sql: str) -> list[str]:
        """Catalog tables named in a query (longest names first, so 'x__all' wins over 'x')."""
        found, rest = [], sql
        for name in self._names:
            if re.search(rf'(?<![\w"]){re.escape(name)}(?![\w"])|"{re.escape(name)}"', rest):
                found.append(name)
                rest = re.sub(rf'(?<![\w"]){re.escape(name)}(?![\w"])|"{re.escape(name)}"', " ", rest)
        return found

    def query(self, sql: str, max_rows: int = 60, timeout: float = 20.0) -> QueryResult:
        sql = sql.strip().rstrip(";").strip()
        res = QueryResult(sql=sql)
        if self.con is None:
            res.error = "no tables available (run python -m fedrag.ingest.build_corpus)"
            return res
        bare = _strip_comments_and_strings(sql)
        if ";" in bare:
            res.error = "one statement per call (remove ';')"
            return res
        if not READ_ONLY_START.match(bare) or FORBIDDEN.search(bare):
            res.error = "only read-only queries are allowed (SELECT / WITH / DESCRIBE / SUMMARIZE / PIVOT)"
            return res
        res.tables = self.tables_in(sql)
        with self._lock:
            cur = self.con.cursor()
        timer = threading.Timer(timeout, cur.interrupt)
        timer.start()
        try:
            cur.execute(sql)
            res.columns = [d[0] for d in cur.description or []]
            rows = cur.fetchmany(max_rows + 1)
            res.truncated = len(rows) > max_rows
            res.rows = rows[:max_rows]
        except Exception as e:  # report DuckDB's message so the agent can fix the query
            msg = str(e).split("\n")[0][:400]
            if "INTERRUPT" in msg.upper():
                msg = f"query took longer than {timeout:.0f}s; filter or aggregate more"
            res.error = f"{type(e).__name__}: {msg}"
            if "does not exist" in msg and "Table" in msg:
                res.error += " (use search_tables / list_tables for valid table names)"
        finally:
            timer.cancel()
            cur.close()
        return res

    def describe(self, name: str, sample_rows: int = 5) -> str:
        t = self.catalog.get(name)
        if t is None:
            close = [n for n in self.catalog if name.lower() in n][:8]
            return f"ERROR: unknown table {name!r}." + (f" Similar: {', '.join(close)}" if close else "")
        lines = [f"Table `{name}` ({t['kind']}): {t['title']}", f"Source: {t['source']} (doc_id={t['doc_id']})",
                 f"Rows: {t['n_rows']}, layout: {t['layout']}"]
        if t["layout"] == "long":
            lines.append("Long layout: one row per cell. Filter row_group/row_label and the header levels "
                         "h1, h2, ... (top to bottom); `value` is the number (NULL when the cell is text such as a "
                         "range; the original text is in `value_text`).")
        if t.get("about"):
            lines.append("About the data: " + t["about"])
        if t.get("notes"):
            lines.append("Notes: " + " | ".join(t["notes"])[:1200])
        lines.append("Columns:")
        for c in t["columns"]:
            label = f" = {c['label']!r}" if c.get("label") and c["label"] != c["name"] else ""
            ex = f"; e.g. {', '.join(map(str, c.get('examples', [])[:4]))}" if c.get("examples") else ""
            lines.append(f"  - {c['name']} [{c['type']}]{label}{ex}")
        if t.get("row_labels"):
            lines.append(f"Row labels ({len(t['row_labels'])} shown): " + "; ".join(t["row_labels"]))
        for h, vals in (t.get("header_values") or {}).items():
            lines.append(f"{h} values: " + "; ".join(vals[:80]))
        sample = self.query(f'SELECT * FROM "{name}" LIMIT {int(sample_rows)}')
        if not sample.error:
            lines.append(f"First {len(sample.rows)} rows:\n{sample.render(40)}")
        return "\n".join(lines)
