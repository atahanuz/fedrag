"""Offline tests for the deterministic parts (no GPU, no network)."""

from __future__ import annotations

import asyncio
import datetime as dt
from pathlib import Path

import pytest

from fedrag.config import CORPUS_DIR, DOCS_DIR
from fedrag.evidence import EvidenceStore, cited_ids
from fedrag.ingest.chunker import Para, chunk_document
from fedrag.ingest.pdf_parser import is_numeric_noise, normalize_text, parse_pdf
from fedrag.orchestrator import finalize
from fedrag.retrieval.bm25 import BM25, tokenize
from fedrag.tools.base import RunContext
from fedrag.tools.market import _transform, calculator

HAVE_PDFS = (DOCS_DIR / "fed_stress_test_results_2026.pdf").exists()
HAVE_CORPUS = (CORPUS_DIR / "chunks.jsonl").exists()


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- parsing


def test_normalize_text_ligatures_and_spaces():
    assert normalize_text("e" + chr(0xFB03) + "cient" + chr(0xA0) + "use") == "efficient use"
    assert normalize_text("pros" + chr(0xAD) + "pects") == "prospects"


@pytest.mark.parametrize("text,noise", [
    ("280", True), ("1998 2002 2006 2010", True), ("Percent", True), ("Median = 16.00", True),
    ("Apr.", True), ("2025:Q4", True), ("Ally | 7.8", False), ("Prices rose modestly.", False),
])
def test_numeric_noise(text, noise):
    assert is_numeric_noise(text) is noise


@pytest.mark.skipif(not HAVE_PDFS, reason="source PDFs not present")
def test_stress_test_table_rows_are_rebuilt():
    parsed = parse_pdf(str(DOCS_DIR / "fed_stress_test_results_2026.pdf"))
    page = parsed.pages[18].text  # Table 5: projected minimum CET1 ratios by bank
    assert "First Citizens | 6.7" in page
    assert "Charles Schwab Corp | 32.2" in page
    table4 = parsed.pages[17].text
    assert "Common equity tier 1 capital ratio | 12.8 | 12.7 | 11.2" in table4


@pytest.mark.skipif(not HAVE_PDFS, reason="source PDFs not present")
def test_chart_axis_noise_removed_from_fsr():
    parsed = parse_pdf(str(DOCS_DIR / "fed_financial_stability_report_2026-05-08.pdf"))
    text = parsed.pages[14].text
    assert "Measures of equity valuations remained high since the previous report." in text
    assert "1998 2002 2006" not in text


# --------------------------------------------------------------------------- chunking


def test_chunks_respect_top_level_sections_and_size():
    paras = []
    for district in ("Federal Reserve Bank of Boston", "Federal Reserve Bank of Chicago"):
        paras.append(Para(page=1, text=district, kind="heading", path=[district]))
        for i in range(12):
            paras.append(Para(page=1 + i // 4, text=f"{district} sentence {i}. " * 12, kind="text", path=[district]))
    chunks = chunk_document(paras, max_words=200, min_words=40, overlap_words=20)
    assert chunks, "expected chunks"
    for c in chunks:
        assert c.n_words <= 260
        assert not ("Boston" in c.text and "Chicago" in c.text), "a chunk crossed a District boundary"
    assert {c.section for c in chunks} == {"Federal Reserve Bank of Boston", "Federal Reserve Bank of Chicago"}


# --------------------------------------------------------------------------- retrieval


def test_bm25_ranks_relevant_text_first():
    docs = ["The Committee raised the federal funds rate target range.",
            "Manufacturing activity in the Chicago District increased slightly.",
            "Bank capital ratios under the severely adverse stress scenario."]
    bm = BM25(docs)
    assert bm.scores("Chicago manufacturing").argmax() == 1
    assert bm.scores("stress test capital").argmax() == 2
    assert tokenize("Banks' capital ratios")[:2] == ["bank", "capit"]


@pytest.mark.skipif(not HAVE_CORPUS, reason="corpus not built")
def test_index_filters_bm25_only():
    from fedrag.retrieval.index import CorpusIndex

    idx = CorpusIndex(gpu=None)
    hits = run(idx.search("Seventh District economic activity", k=3, doc_types=["economic_conditions_report"],
                          date_from="2026-09-01", rerank=False))
    assert hits and all(h.chunk["doc_id"] == "fed_beige_book_2026-09-02" for h in hits)
    assert "fed_fomc_minutes_2026-07-29" in {d["doc_id"] for d in idx.list_docs(["meeting_minutes"], "2026-07-01")}


# --------------------------------------------------------------------------- evidence & citations


def test_cited_ids_and_finalize_drop_unknown():
    store = EvidenceStore()
    ev = store.add_web("https://example.org/a", "Example", "text")
    assert ev.id == "W1"
    assert store.add_web("https://example.org/a", "Example", "longer text").id == "W1"  # deduplicated
    assert cited_ids("A [W1] and [D3, W1] and [M2].") == ["W1", "D3", "M2"]
    answer, sources = finalize("Fact one [W1]. Fact two [D9].", store)
    assert answer == "Fact one [W1]. Fact two ."
    assert [s["id"] for s in sources] == ["W1"]


# --------------------------------------------------------------------------- data tools


def test_calculator():
    ctx = RunContext(index=None, llm=None, today=dt.date(2026, 10, 4))
    assert run(calculator(ctx, "(334.131/321.5 - 1)*100")).endswith("3.928771384")
    assert run(calculator(ctx, "12,583.7 / 2 + max(1,2)")).endswith("6293.85")
    assert run(calculator(ctx, "__import__('os')")).startswith("ERROR")


def test_series_transforms():
    s = [("2025-01-01", 100.0), ("2025-02-01", 101.0), ("2026-01-01", 103.0), ("2026-02-01", 104.04)]
    yoy, _ = _transform(s, "yoy")
    assert [d for d, _ in yoy] == ["2026-01-01", "2026-02-01"]
    assert yoy[0][1] == pytest.approx(3.0)
    diff, _ = _transform(s, "diff")
    assert diff[0][1] == pytest.approx(1.0)


def test_eval_questions_file_is_valid():
    import json

    qs = [json.loads(line) for line in open(Path(__file__).parent.parent / "eval" / "questions.jsonl")]
    assert len({q["id"] for q in qs}) == len(qs)
    for q in qs:
        assert {"id", "category", "question", "expected_agents", "reference"} <= q.keys()


@pytest.mark.skipif(not HAVE_CORPUS, reason="corpus not built")
def test_fed_tools_offline():
    from fedrag.retrieval.index import CorpusIndex
    from fedrag.tools.fed_docs import find_documents, list_fed_documents, read_document_pages

    ctx = RunContext(index=CorpusIndex(gpu=None), llm=None, today=dt.date(2026, 10, 4))
    out = run(find_documents(ctx, "Financial Vulnerability Index structural vulnerabilities",
                             doc_types=["working_paper"]))
    assert "fed_feds_wp_2026-065" in out.splitlines()[1]
    out = run(list_fed_documents(ctx, doc_type="meeting_minutes", date_from="2026-07"))
    assert "fed_fomc_minutes_2026-07-29" in out
    out = run(read_document_pages(ctx, "fed_stress_test_results_2026", 19))
    assert out.startswith("[D1]") and "First Citizens | 6.7" in out


def test_writer_evidence_is_shared_fairly_between_agents():
    from fedrag.agents.base import AgentResult
    from fedrag.agents.writer import MAX_EVIDENCE_ITEMS, select_evidence

    store = EvidenceStore()
    for i in range(MAX_EVIDENCE_ITEMS + 20):
        store.add_web(f"https://example.org/{i}", "t", "x")
    verbose = AgentResult(agent="fed_research", task_id="t1", task="", answer="",
                          evidence_ids=[f"W{i}" for i in range(1, MAX_EVIDENCE_ITEMS + 5)])
    brief = AgentResult(agent="market_data", task_id="t2", task="", answer="",
                        evidence_ids=[f"W{MAX_EVIDENCE_ITEMS + 10}", f"W{MAX_EVIDENCE_ITEMS + 11}"])
    sel = select_evidence([verbose, brief], store)
    assert len(sel) == MAX_EVIDENCE_ITEMS and set(brief.evidence_ids) <= set(sel)


def test_step_series_summary_lists_every_change():
    from fedrag.tools.market import _summarize_series

    d0 = dt.date(2025, 1, 1)
    target = [((d0 + dt.timedelta(days=i)).isoformat(), 4.25 if i < 260 else 4.0 if i < 300 else 3.75)
              for i in range(400)]
    text = _summarize_series("FRED DFEDTARL", target, is_rate=True)
    assert "2 change(s)" in text and "2025-09-18: 4.25 -> 4" in text and "2025-10-28: 4 -> 3.75" in text
    noisy = [((d0 + dt.timedelta(days=i)).isoformat(), 4 + (i % 7) / 100) for i in range(400)]
    assert "observations (sampled)" in _summarize_series("FRED DGS2", noisy, is_rate=True)


def test_old_tool_results_are_shortened_when_the_context_fills_up():
    from fedrag.agents.base import compact_tool_results

    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "task"}]
    for i in range(8):
        msgs.append({"role": "assistant", "content": ""})
        msgs.append({"role": "tool", "tool_call_id": str(i), "content": f"[D{i + 1}] result {i}\n" + "x" * 30_000})
    assert compact_tool_results(msgs, budget=150_000) > 0
    tools = [m["content"] for m in msgs if m["role"] == "tool"]
    assert "shortened to save context; evidence IDs in this result: D1" in tools[0]
    assert all(len(t) > 30_000 for t in tools[-4:])  # the newest results stay whole
    assert sum(len(str(m["content"])) for m in msgs) <= 150_000
    assert compact_tool_results(msgs, budget=150_000) == 0  # nothing left to do
