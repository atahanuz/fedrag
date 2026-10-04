"""Offline tests for the multi-format ingestion (HTML, Word, Excel, CSV), the table engine and SQL access."""

from __future__ import annotations

import asyncio
import datetime as dt
import json

import numpy as np
import pytest

from fedrag.config import CORPUS_DIR
from fedrag.ingest.document import Block, paginate
from fedrag.ingest.tables import TableBlock, extract_tables, period_of

HAVE_CORPUS = (CORPUS_DIR / "tables.duckdb").exists()


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- table engine


def scf_like_grid() -> tuple[list[list], list[tuple[int, int, int, int]]]:
    """Title, units, a 3-level header with merged year cells, row groups, blank spacers, a note."""
    grid = [
        ["4. Family net worth, by selected characteristics", None, None, None, None],
        ["Thousands of 2022 dollars", None, None, None, None],
        [None, None, None, None, None],
        ["Family characteristic", 2019, None, 2022, None],
        [None, "Median", "Mean", "Median", "Mean"],
        [None, None, None, None, None],
        ["All families", 141.14, 865.72, 192.7, 1059.47],
        [None, None, None, None, None],
        ["Percentile of income", None, None, None, None],
        ["Less than 20", 13.7, 99.0, 14.4, 101.0],
        ["90–100", 1800.0, 4800.0, 2600.0, 5900.0],
        [None, None, None, None, None],
        ["Note: See note to table 1.", None, None, None, None],
    ]
    merged = [(3, 1, 3, 2), (3, 3, 3, 4)]  # "2019" and "2022" each span two columns
    return grid, merged


def test_crosstab_becomes_long_table_with_groups_and_notes():
    grid, merged = scf_like_grid()
    (b,) = extract_tables(grid, merged)
    assert b.title.startswith("4. Family net worth")
    assert "Thousands of 2022 dollars" in b.notes and any("See note to table 1" in n for n in b.notes)
    assert b.layout == "long" and b.label_cols == 1
    df = b.frame()
    assert list(df.columns) == ["row_group", "row_label", "h1", "h2", "value", "value_text"]
    row = df[(df.row_label == "90–100") & (df.h1 == "2022") & (df.h2 == "Median")]
    assert row.value.item() == 2600.0 and row.row_group.item() == "Percentile of income"
    assert df[(df.row_label == "All families") & (df.h1 == "2019") & (df.h2 == "Mean")].value.item() == 865.72


def test_time_series_sheet_gets_normalised_period():
    grid = [["Total Debt Balance", None, None], ["Trillions of $", None, "Source: New York Fed CCP/Equifax"],
            [None, "Mortgage", "Total"], ["25:Q4", 13.17, 18.7757], ["26:Q1", 13.191, 18.784], ["26:Q2", 13.117, 18.7705]]
    (b,) = extract_tables(grid)
    df = b.frame()
    assert list(df.period) == ["2025-Q4", "2026-Q1", "2026-Q2"]
    assert df.loc[df.period == "2026-Q2", "total"].item() == 18.7705
    assert any(n.startswith("Source:") for n in b.notes) and "Source" not in b.title


def test_year_quarter_columns_and_na_markers():
    grid = [["YEAR", "QUARTER", "UNEMP2", "UNEMPA"], [2026, 2, 4.36, "#N/A"], [2026, 3, 4.2, 4.275]]
    (b,) = extract_tables(grid)
    df = b.frame()
    assert list(df.period) == ["2026-Q2", "2026-Q3"]
    assert df.unempa.isna().iloc[0] and df.unempa.iloc[1] == 4.275


@pytest.mark.parametrize("raw,norm", [("03:Q1", "2003-Q1"), (201306.0, "2013-06"), ("2026 Q1", "2026-Q1"),
                                      ("2003-03-01", "2003-03-01"), (2022.0, "2022"), ("Median", None)])
def test_period_of(raw, norm):
    assert period_of(raw) == norm


def test_forced_header_ranges_and_repeated_labels():
    grid = [["Variable", "Median", "Median", "Central Tendency"], ["Variable", "2026", "2027", "2026"],
            ["Unemployment rate", "4.1", "4.1", "4.1–4.2"], ["June projection", "4.3", "4.3", "4.3–4.4"],
            ["PCE inflation", "3.7", "2.3", "3.5–3.7"], ["June projection", "3.6", "2.3", "3.5–3.7"]]
    (b,) = extract_tables(grid, header_rows=2)
    df = b.frame()
    assert "Unemployment rate | June projection" in set(df.row_label)
    ct = df[(df.row_label == "PCE inflation") & (df.h1 == "Central Tendency")]
    assert ct.value.isna().item() and ct.value_text.item() == "3.5–3.7"


def test_paginate_breaks_at_headings_and_size():
    blocks = [Block("heading", "Intro", level=1)] + [Block("text", "word " * 200) for _ in range(4)] + \
             [Block("heading", "Second", level=1), Block("text", "word " * 100)]
    pages, toc, _ = paginate(blocks, part_words=600)
    assert len(pages) >= 2 and all(len(p.text.split()) <= 900 for p in pages)
    assert [t[1] for t in toc] == ["Intro", "Second"]


# --------------------------------------------------------------------------- formats


def test_html_parser(tmp_path):
    from fedrag.ingest.html_parser import parse_html

    html = """<html><body><div id="article">
      <p class="article__time">September 16, 2026</p><h3 class="title">Federal Reserve issues FOMC statement</h3>
      <div class="shareDL">Share</div>
      <p>The Committee decided to raise the target range<sup>1</sup> to 3-3/4 to 4 percent.</p>
      <h4>Table 1. Projections</h4>
      <table><thead><tr><th>Variable</th><th colspan="2">Median</th></tr>
                    <tr><th>Variable</th><th>2026</th><th>2027</th></tr></thead>
        <tbody><tr><td>Unemployment rate</td><td>4.1</td><td>4.1</td></tr>
               <tr><td>PCE inflation</td><td>3.7</td><td>2.3</td></tr></tbody></table>
      <h5>Related Content</h5><ul><li><a href="x">Implementation note</a></li></ul>
    </div></body></html>"""
    path = tmp_path / "page.html"
    path.write_text(html)
    d = parse_html(str(path))
    text = "\n".join(p.text for p in d.pages)
    assert "raise the target range to 3-3/4 to 4 percent" in text
    assert "Share" not in text and "Implementation note" not in text
    (t,) = d.tables
    assert t.title == "Table 1. Projections" and t.layout == "long"
    df = t.frame()
    assert df[(df.row_label == "PCE inflation") & (df.h2 == "2027")].value.item() == 2.3


def test_docx_parser(tmp_path):
    import docx

    doc = docx.Document()
    doc.add_heading("Reproducing Bulletin Tables", level=1)
    doc.add_paragraph("Follow these steps.")
    doc.add_paragraph("Click the Analysis button", style="List Bullet")
    table = doc.add_table(rows=4, cols=2)
    for r, (a, b) in enumerate([("Label", "SDA Variable"), ("Percentile of income", "NINCCAT"),
                                ("Age of head", "AGECL"), ("Education of head", "EDCL")]):
        table.cell(r, 0).text, table.cell(r, 1).text = a, b
    box = doc.add_table(rows=1, cols=1)
    box.cell(0, 0).text = "If SAMPTYPE = 1 Then\nQ1.Ask()\nEnd If"  # interview-program code: skipped
    path = tmp_path / "guide.docx"
    doc.save(path)

    from fedrag.ingest.docx_parser import parse_docx

    d = parse_docx(str(path))
    text = "\n".join(p.text for p in d.pages)
    assert "• Click the Analysis button" in text and "Age of head | AGECL" in text and "Ask()" not in text
    assert d.toc[0][1] == "Reproducing Bulletin Tables"
    (t,) = d.tables
    assert set(t.frame()["sda_variable"]) == {"NINCCAT", "AGECL", "EDCL"}


def test_excel_parser_skips_chart_sheets_and_expands_merges(tmp_path):
    import openpyxl
    from openpyxl.chart import BarChart, Reference

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Page 3 Data"
    ws.append(["Total Debt Balance and Its Composition"])
    ws.append(["Trillions of $"])
    ws.append([None, "Balance", None])
    ws.merge_cells("B3:C3")
    ws.append([None, "Mortgage", "Total"])
    for row in (["26:Q1", 13.191, 18.784], ["26:Q2", 13.117, 18.7705]):
        ws.append(row)
    cs = wb.create_chartsheet("Chart3")
    chart = BarChart()
    chart.add_data(Reference(ws, min_col=2, min_row=4, max_row=6))
    cs.add_chart(chart)
    path = tmp_path / "hhd.xlsx"
    wb.save(path)

    from fedrag.ingest.sheet_parser import parse_sheets

    d = parse_sheets(str(path))
    assert d.kind == "data" and len(d.tables) == 1
    df = d.tables[0].frame()
    assert d.tables[0].layout == "long"  # "Balance" spans two sub-headers
    assert df[(df.h2 == "Total") & (df.row_label == "26:Q2")].value.item() == 18.7705


# --------------------------------------------------------------------------- SQL access


def _store(tmp_path):
    import duckdb
    import pandas as pd

    from fedrag.retrieval.tables import TableStore

    df = pd.DataFrame({"bank": ["A", "B"], "cet1_min": [6.7, 9.9]})
    con = duckdb.connect(str(tmp_path / "tables.duckdb"))
    con.register("df_view", df)
    con.execute("CREATE TABLE results AS SELECT * FROM df_view")
    con.close()
    entry = {"table": "results", "doc_id": "d1", "title": "Results", "source": "CSV file", "layout": "wide",
             "n_rows": 2, "columns": [{"name": "bank", "type": "text"}, {"name": "cet1_min", "type": "number"}],
             "kind": "table"}
    (tmp_path / "tables.jsonl").write_text(json.dumps(entry) + "\n")
    return TableStore(tmp_path)


def test_table_store_reads_and_refuses_everything_else(tmp_path):
    store = _store(tmp_path)
    res = store.query("SELECT bank FROM results ORDER BY cet1_min LIMIT 1")
    assert res.error is None and res.rows == [("A",)] and res.tables == ["results"]
    for bad in ["DROP TABLE results", "SELECT 1; SELECT 2", "SELECT * FROM read_csv('/etc/hosts')",
                "ATTACH 'x.db' AS x", "COPY results TO 'out.csv'"]:
        assert store.query(bad).error, bad
    assert "results" in store.describe("results") and store.describe("nope").startswith("ERROR")


def test_incremental_embeddings(tmp_path, monkeypatch):
    from fedrag import config
    from fedrag.retrieval import index as idx_mod
    from fedrag.retrieval.text_format import embedding_text

    corpus, index = tmp_path / "corpus", tmp_path / "index"
    corpus.mkdir()
    index.mkdir()
    chunks = [{"chunk_id": f"d#{i}", "title": "T", "date": "2026", "section": "", "text": f"text {i}"} for i in range(3)]
    (corpus / "chunks.jsonl").write_text("".join(json.dumps(c) + "\n" for c in chunks))
    old = {"id": "d#0", "text": embedding_text(chunks[0])}  # chunk 0 was embedded before
    (index / "embed_input.jsonl").write_text(json.dumps(old) + "\n")
    np.save(index / "embeddings.npy", np.ones((1, 4), dtype=np.float16))
    json.dump({"model": "m", "ids": ["d#0"]}, open(index / "ids.json", "w"))
    monkeypatch.setattr(config, "CORPUS_DIR", corpus)
    monkeypatch.setattr(config, "INDEX_DIR", index)

    idx_mod._prepare()
    todo = [json.loads(line)["id"] for line in open(index / "embed_input.jsonl")]
    assert todo == ["d#1", "d#2"]
    new = index / "new"
    new.mkdir()
    np.save(new / "embeddings.npy", np.full((2, 4), 2, dtype=np.float16))
    json.dump({"model": "m", "ids": todo}, open(new / "ids.json", "w"))
    idx_mod._merge(new)
    emb = np.load(index / "embeddings.npy")
    meta = json.load(open(index / "ids.json"))
    assert meta["ids"] == ["d#0", "d#1", "d#2"] and emb[0, 0] == 1 and emb[2, 0] == 2


@pytest.mark.skipif(not HAVE_CORPUS, reason="corpus not built")
def test_data_tools_on_the_collection():
    from fedrag.retrieval.index import CorpusIndex
    from fedrag.tools.base import RunContext
    from fedrag.tools.data_tools import describe_table, list_tables, query_data

    ctx = RunContext(index=CorpusIndex(gpu=None), llm=None, today=dt.date(2026, 10, 5))
    out = run(query_data(ctx, "SELECT disclosure_legal_name, common_equity_tier1_min_rat FROM dfast_results_2013_2026 "
                              "WHERE exercise_name = '2026 Stress Test' AND scenario_name ILIKE '%severely%' "
                              "ORDER BY common_equity_tier1_min_rat LIMIT 1"))
    assert out.startswith("[D1]") and "First Citizens" in out and "6.7" in out
    ev = ctx.evidence.get("D1")
    assert ev.meta["unit"] == "query" and ev.meta["doc_ids"] == ["fed_dfast_results_2013-2026"]
    assert "Long layout" in run(describe_table(ctx, "sep__table_1__all"))
    assert "page_3_data" in run(list_tables(ctx, doc_id="nyfed_household_debt_credit_2026q2"))


def test_table_block_render_shows_latest_rows():
    rows = [[f"{y}:Q1", float(y)] for y in range(2000, 2030)]
    b = TableBlock(title="t", notes=[], header=[["", "v"]], rows=rows, groups=[None] * len(rows), c0=0, label_cols=1)
    text = b.render(max_rows=10)
    assert "2029:Q1" in text and "2000:Q1" in text and "2010:Q1" not in text and "rows omitted" in text
