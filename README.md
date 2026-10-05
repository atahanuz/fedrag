# fedrag — Agentic RAG over a mixed-format Federal Reserve collection

A multi-agent research assistant that answers questions from a collection of **391 Federal Reserve
documents in five formats**: PDF reports, web pages, Word files, Excel workbooks and CSV files, dated 2022 to
October 2026. They include FOMC minutes, statements, projections and press conferences, Beige Books,
Monetary Policy and Financial Stability Reports, stress tests with bank-level results, speeches, testimony,
FEDS Notes and working papers, supervision letters and household and forecaster surveys. The **846 tables**
inside the spreadsheets, CSV files, web pages and Word files are extracted into a SQL database that an agent
queries. The system also handles questions that are only partly about the collection, or not at all:

| Question type | What happens |
| --- | --- |
| What a Fed document says | `fed_research` agent searches, filters, reads pages and tables, and cites them |
| Numbers in the collection's tables (stress-test results by bank, SEP projections across meetings, household debt, survey data) | `data_analyst` agent finds the table, inspects its schema and answers with SQL |
| After the collection ends, or outside it | `web_research` agent searches the web and reads pages |
| Live numbers (FX, fed funds, yields, CPI, prices) | `market_data` agent calls FX / FRED / market APIs and a calculator |
| Several of the above | the planner runs several agents in parallel and the writer merges their findings |
| General knowledge or chit-chat | answered directly by the LLM, no tools |

All models are open-weight and self-hosted on a Colab A100 80GB. No proprietary API keys are needed. A web
GUI, the [pipeline explorer](#pipeline-explorer-web-gui), shows each run live: which agents the planner
summons, every tool call and SQL query, the passages they cite and the fact-check.

## How it works

```mermaid
flowchart LR
    Q[Question + chat history] --> P[Planner / router]
    P -- no tools needed --> D[Direct answer]
    P -- tasks --> X{{Parallel task waves}}
    X --> F[fed_research agent]
    X --> T[data_analyst agent]
    X --> W[web_research agent]
    X --> M[market_data agent]
    F & T & W & M --> E[(Evidence store<br/>D# / W# / M# IDs)]
    E --> S[Synthesizer]
    S --> V{Verifier}
    V -- accept --> A[Cited answer + sources + trace]
    V -- revise --> S
    V -- research --> X
```

1. **Planner (router).** Classifies the question, rewrites follow-ups into standalone questions, and
   decomposes the work into self-contained tasks for specialist agents. It is given today's date, a compact
   description of the collection (types, formats, dates, speakers) and of the main datasets, so it knows
   which agent owns which part and when the collection cannot be enough. It returns a
   JSON-schema-constrained plan. Comparisons over time become parallel tasks, and tasks may depend on each
   other's results.
2. **Specialist agents.** Each one is a ReAct loop over native function calling. In each turn the model
   may call several tools at once; they run concurrently. The agent finishes by calling
   `submit_findings` (answer, cited key facts, confidence, gaps).
   - `fed_research`: `search_fed_documents` (hybrid search with type, date and document filters; it also
     points to related data tables), `find_documents` (which documents discuss a topic),
     `list_fed_documents` (resolves "latest", "the June meeting", "Waller's speeches" and so on),
     `get_document_outline`, `read_document_pages` (whole pages of a PDF, ~600-word parts of a web page or
     Word file, a table preview of a spreadsheet), `expand_context` (neighbouring passages), `calculator`.
   - `data_analyst`: `search_tables` (finds tables by what they measure), `list_tables` (the sheets of a
     workbook, the tables of a page), `describe_table` (columns, units, notes, row labels, header values,
     first rows), `query_data` (read-only DuckDB SQL), plus document search and page reading for
     definitions (the SPF variable codes are only explained in its PDF documentation) and `calculator`.
   - `web_research`: `web_search` (DuckDuckGo, with news and recency filters), `fetch_webpage`
     (HTML or PDF, query-focused extraction), `calculator`.
   - `market_data`: `get_exchange_rate` and `get_exchange_rate_history` (ECB via Frankfurter, with a
     fallback source), `get_economic_series` (FRED, with year-over-year and difference transforms),
     `search_economic_series`, `get_market_prices` (Yahoo via the local `stockcache`), `calculator`.
3. **Corrective step.** If no agent found any evidence, a web-research task is added automatically.
4. **Synthesizer.** Writes the answer from the findings and the evidence excerpts, with an inline
   citation (`[D3]`, `[W2]`, `[M1]`) on every fact. A SQL result is evidence too: the writer and the
   verifier see the query and its rows, and the source list names the table and its document.
5. **Verifier.** Checks support, completeness, date consistency and arithmetic. It returns `accept`,
   `revise` (the synthesizer fixes the listed problems) or `research` (follow-up tasks run, then the answer
   is rewritten).
6. **Output.** The answer, its source list (PDF citations link to the exact page; web pages, Word files
   and data files to their source URL), the plan, every agent's findings and a full trace of tool calls.

### The collection

`federal_reserve/README.md` describes it in detail: 103 PDFs, 264 web pages, 2 Word files, 5 Excel
workbooks (310 sheets, 36 of them charts) and 17 CSV files; 387 from the Board, 4 from the New York and
Philadelphia Feds.
`scripts/fetch_corpus.py --discover` crawled the Board's indexes (press releases, speeches, testimony,
FEDS Notes, SR letters, SLOOS, the FOMC calendar) and a curated list of data files to grow the original 93
PDFs; `metadata.csv` records each file's URL, SHA-256, format, publisher, speaker and, for data files whose
columns are codes, a short data dictionary.

### Ingestion (`fedrag/ingest/`)

Each format has its own parser; all of them produce the same structure (pages of typed paragraphs, an
outline, table blocks), so sectioning, chunking, retrieval and citation work the same way for every file.

- **PDF** (`pdf_parser.py`): a layout-aware PyMuPDF extractor. It rebuilds table rows from span geometry
  (`First Citizens | 6.7`), drops chart axis labels and running headers, removes footnote markers and
  fixes hyphenation. Two-column report pages keep their reading order.
- **Web pages** (`html_parser.py`): finds the article in both federalreserve.gov templates, removes
  navigation, share and video widgets and "Related Content" lists, keeps headings, lists and footnotes in
  reading order, and expands every `<table>` (colspan, rowspan, `<thead>` header rows) into a grid whose
  caption is the preceding "Table N." heading.
- **Word** (`docx_parser.py`): paragraph styles give headings and bullets, tables of contents are dropped,
  tables are read in place. The SCF interview program keeps its routing logic in thousands of one-cell
  code boxes, which are skipped; its answer-category tables become text.
- **Excel and CSV** (`sheet_parser.py`, `tables.py`): chart sheets and license sheets are skipped and
  merged cells are expanded. A table engine then finds each table block on a sheet: title, unit and
  source lines above it, header rows (a label spanning several columns, such as a survey year over
  "Median | Mean", is spread over them), row groups introduced by label-only rows ("Percentile of income"),
  continuation blocks, and notes below. A one-row header gives a **wide** table; a multi-row header gives a
  **long** table (`row_group, row_label, h1..hN, value, value_text`) so that every dimension of a cross-tab
  can be filtered in SQL. Date-like row labels (`26:Q2`, `201306`, year + quarter columns) get a normalised
  `period` column, and repeated labels such as "June projection" are qualified with the row above.
- **Pages and parts.** PDFs keep their pages. Web pages and Word files are split into parts of about
  600 words that break at headings; a spreadsheet has one part per table. Citations say "p. 12" or
  "part 3" accordingly.
- **Sections and chunks** (`chunker.py`): every paragraph gets a section path from the outline plus
  headings detected from the font or markup, for example `Federal Reserve Bank of Chicago > Manufacturing`.
  Chunks of about 260 words never cross a top-level section, so a Beige Book chunk belongs to exactly one
  District: 9,575 prose chunks in all.
- **Tables** (`build_corpus.py`): the 846 table blocks become DuckDB tables, and 20 views stack a table
  that recurs across releases (SEP Table 1 for all 15 meetings since March 2023, stress-test scenarios for
  2024-2026) with `doc_id` and `doc_date` columns. Each table also gets a **table card** for retrieval: its
  title, source, columns with labels and examples, units and notes, the data dictionary, row labels or
  period range, and for small tables the rows themselves (866 cards).

### Retrieval and SQL (`fedrag/retrieval/`)

- **Search** (`index.py`): metadata filter (type, date, document, chunk kind, format), then BM25 (sparse
  matrix) and dense search with **Qwen3-Embedding-8B** (4096-d), fused with reciprocal rank fusion, then
  the top 40 reranked by **Qwen3-Reranker-4B**. Without the GPU it falls back to BM25 only. Document search
  returns prose passages and lists matching table cards as pointers; table search ranks only cards.
- **Embeddings are incremental**: vectors are keyed by the hash of the text they embed, so rebuilding
  the corpus re-embeds only new or changed chunks (the 6,368 chunks of the original PDFs were reused when
  the collection grew to 10,441 chunks). `colab_up.py` runs the batch job for large updates; with the
  gateway already running, `python -m fedrag.retrieval.index embed-missing` embeds a few changed chunks
  through it in seconds.
- **SQL** (`tables.py`): DuckDB opened read-only, with external access disabled and the configuration
  locked, so a query can read the collection's tables and nothing else (no files, network, settings or
  writes). One statement per call, a 20-second timeout and a row cap; DuckDB's error messages go back to
  the agent so it can fix its query.

### Models (all on one A100 80GB)

| Role | Model | Notes |
| --- | --- | --- |
| LLM for every agent | `Qwen/Qwen3.8-27B-FP8` via vLLM 0.30 | tool calling (`qwen3_xml` parser), JSON-schema output, MTP speculative decoding, about 95 tok/s per stream, 64K context, thinking mode off |
| Embeddings | `Qwen/Qwen3-Embedding-8B` | chunks embedded on the GPU once, then only new or changed ones (4,073 new chunks: about 5 min); queries use an instruction prefix |
| Reranker | `Qwen/Qwen3-Reranker-4B` | P("yes") relevance; about 2 s for 40 passages |

Qwen3.8-27B was chosen because it was the strongest instruction-following and agentic model that fits
in 80GB next to the retrieval models (IFBench 79.5, Terminal-Bench 73). FP8 weights run on the A100
through vLLM's Marlin kernels.

## Setup

```bash
# 1. Environment (Python 3.11+)
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -e ".[ui,dev]"

# 2. Download the collection (391 files, 199 MB; checks each SHA-256 in federal_reserve/metadata.csv)
.venv/bin/python scripts/fetch_corpus.py            # --discover also looks for new documents

# 3. Parse every document into pages, sections, chunks and SQL tables (about 20 s; writes data/corpus/)
.venv/bin/python -m fedrag.ingest.build_corpus

# 4. Start the GPU server on Colab (see COLAB_GUIDE.md for the high-RAM A100 patch).
#    Creates the session, installs vLLM, downloads models, embeds the chunks that have no embedding yet,
#    starts gateway + vLLM + tunnel + keeper, writes FEDRAG_GPU_URL / FEDRAG_API_KEY to .env, and leaves
#    a heartbeat running in the background.
.venv/bin/python scripts/colab_up.py          # 13-20 min on a fresh VM (20 with 4,073 chunks to embed)
.venv/bin/python -m fedrag status             # gateway, LLM and index check
```

`scripts/colab_up.py --stop` ends the heartbeat and releases the VM. `--status` shows the serving
stack, the keeper and the heartbeat.

Keeping a Colab VM alive needed measurements (data in
[COLAB_GUIDE.md](COLAB_GUIDE.md#why-sessions-die-and-how-to-keep-them-alive)):

- Colab releases a GPU VM 20-22 minutes after the last kernel-websocket traffic through its proxy.
  Busy servers on the VM do not count, and neither do a busy kernel, HTTP requests through the proxy or
  the CLI's own keep-alive pings. A websocket that the VM opens to its own kernel through its public
  proxy URL does count, so `gpu_server/keeper.py` holds one while the gateway is in use. The VM
  therefore survives the laptop sleeping. After 60 minutes without gateway requests (`--idle-minutes`)
  the keeper lets go, and Colab releases the VM about 20 minutes later.
- `colab-cli` 0.6.0 never refreshes its runtime-proxy token, which expires after an hour. The next
  `colab exec` then gets a 401, and the CLI forgets the session although the VM keeps running.
  `scripts/colab_keepalive.py` refreshes the token before it expires and re-adopts a VM that the CLI
  has already dropped.
- The background heartbeat runs the status cell every 4 minutes. It restarts crashed components,
  hands the keeper fresh tokens, updates `.env` when the tunnel URL changes, and is a second keep-alive
  path. `colab_up.py --keepalive-only` restarts it; its log is `~/.cache/colab-keepalive/fedrag.log`.

For other colab-cli sessions, `python scripts/colab_keepalive.py -s NAME --detach` runs the same
heartbeat and token refresh (without the keeper).

## Usage

```bash
.venv/bin/python -m fedrag ask "What did the FOMC decide in July 2026, and who dissented?"
.venv/bin/python -m fedrag ask -v --save run.json "..."   # show tool results and save the full trace
.venv/bin/python -m fedrag chat                           # multi-turn, follow-up questions work
.venv/bin/python -m fedrag search "CET1 ratio" --type stress_test   # raw retrieval, no agents
.venv/bin/python -m fedrag tables household_debt          # list SQL tables (add --describe NAME for a schema)
.venv/bin/python -m fedrag sql "SELECT period, total FROM nyfed_household_debt_2026q2__page_3_data ORDER BY period DESC LIMIT 4"
.venv/bin/python -m fedrag ui                             # pipeline explorer on http://127.0.0.1:7860
```

### Pipeline explorer (web GUI)

`fedrag ui` serves a page where you type a question, or pick one of 15 examples (simple ones need one agent and
one source, complex ones several agents and formats), and watch the pipeline run:

![Pipeline explorer during a run](docs/explorer.png)

- **Pipeline graph**: the planner, each agent task it creates (parallel tasks side by side, dependent tasks
  below their inputs), the writer, the fact-checker and the answer. Running steps glow and the edges into
  them animate; a web fallback, a research round or a revision adds its own row.
- **Timeline**: when each step ran, with tool calls as solid segments, so parallel work and LLM time show.
- **Inspector**: click any step. An agent shows its task and every tool call grouped by step: search queries
  and filters, SQL (highlighted) with its result table, pages read, web pages, data series. Each call lists
  the passages it returned, and the agent's findings follow, with confidence and gaps. The planner shows its
  reasoning and tasks; the fact-checker shows its verdict and issues.
- **Answer**: streams in while the writer types. Each citation is a chip: hover to preview the passage, click
  to read it in full (for a SQL result, the query and its rows) and see which agent retrieved it.

Every run is saved to `data/ui_runs/`: the History tab reopens it, and Replay animates it again at 1×, 4× or
16× speed. The Eval tab opens the 52 evaluation runs with the judge's score and the reference answer, so the
explorer is useful while the GPU is off. Options: fact-checking on or off, thinking mode for the specialist
agents, and follow-up questions that send the previous turns to the planner. The **Details** switch in the
top bar adds to every step what is executing: the LLM and its role, or each running tool and the service
behind it (BM25 + embedder + reranker for search, DuckDB for SQL, FRED, ECB, Yahoo, DuckDuckGo). The server reads `.env` when it
checks the GPU, so it picks up the new tunnel address after `scripts/colab_up.py` restarts the backend.

Real trace (abridged, question `cf01` in `eval/results/final_agentic.jsonl`): a cross-format question. The
planner gives the Bulletin (PDF) to `fed_research` and the Excel tables to `data_analyst`; they run in
parallel, and the SQL result is cited like a page.

```
     planner    9.4s intent=fed_documents tasks=2 — The question asks for two specific data points from the SCF ...
                     • fed_research t1: In the document 'Changes in U.S. Family Finances from 2019 to 2022' ...
                     • data_analyst t2: Query the SCF historical tables in 2022 dollars ...
fed_research   15.8s   ↳ search_fed_documents({"query": "median family net worth 2019 2022 change", "doc_types": ["household_survey"]})
data_analyst   15.9s   ↳ search_tables({"query": "median family net worth by year Survey of Consumer Finances", "max_tables": 10})
data_analyst   15.9s   ↳ list_tables({"title_contains": "net worth"})
data_analyst   31.4s   ↳ describe_table({"table": "scf_2022_hist_tables_real__table_4"})
fed_research   34.9s   ↳ get_document_outline({"doc_id": "fed_scf_bulletin_2023-10"})
data_analyst   36.6s   ↳ query_data({"sql": "SELECT row_group, row_label, h1, h2, value, value_text FROM scf_2022_hist_tables_real__table_4 WHE…"})
data_analyst   45.0s   ↳ calculator({"expression": "(192.7 - 141.14) / 141.14 * 100"})
fed_research   49.4s   ↳ read_document_pages({"doc_id": "fed_scf_bulletin_2023-10", "start_page": 17, "end_page": 19})
data_analyst   57.2s ✔ confidence=high evidence=1 steps=5 tools=6
fed_research   62.3s ✔ confidence=high evidence=3 steps=5 tools=6
 synthesizer   62.3s ▶ write the cited answer
    verifier   78.7s verdict=accept
```

> Median family net worth rose from $141,100 in 2019 to $192,900 in 2022, a 37 percent increase, according
> to the October 2023 SCF Bulletin [D8][D9]. The SCF historical Excel tables in 2022 dollars show a very
> similar comparison: $141,140 in 2019 and $192,700 in 2022, a 36.53 percent increase [D7]. ...

## Evaluation

`eval/questions.jsonl` has 52 questions in 11 categories, with reference answers taken from the
documents, from the data files (each value checked with SQL against the source file), from live data on
2026-10-04, or from the web. The 20 questions added with the expanded collection cover structured data
(rankings and time series over the stress-test CSVs, the SEP tables, the NY Fed and SPF workbooks), Word
files, web pages (speeches, testimony, SLOOS, SR letters, FEDS Notes, press releases) and answers that
need several formats at once (the SCF Bulletin PDF against its Excel tables; the SEP against the press
conference). `eval/run_eval.py` scores:

- **routing:** every expected agent ran and no forbidden agent ran;
- **correctness:** an LLM judge compares the answer with the reference (2 correct, 1 partial, 0 wrong);
- **grounding:** the share of tool-based answers that carry citations;
- **cost:** latency, LLM calls and tool calls.

### Results (two final runs, 2026-10-05, 391-document collection)

The agentic system is compared with a **naive RAG baseline** that uses the same retrieval stack (hybrid
search and reranker over the same chunks and table cards) and the same LLM, but makes one retrieval and one
LLM call (`run_eval.py --naive`). The final system was run twice on all 52 questions (runs A and B):

| Category | n | Routing | Fully correct: run A / run B | Naive RAG | Median latency* (A) | LLM calls / tool calls (A) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Fed documents, single fact | 10 | 100% | 90% / 100% | 70% | 87 s | 8.1 / 5.4 |
| Fed documents, multi-document comparison | 4 | 100% | 100% / 100% | 25% | 126 s | 14.0 / 13.0 |
| **Structured data (Excel / CSV via SQL)** | 9 | 100% | **100% / 100%** | 33% | 36 s | 6.8 / 4.0 |
| **Word documents** | 2 | 100% | **100% / 100%** | 100% | 53 s | 6.0 / 3.0 |
| **Web pages (speeches, testimony, SLOOS, SR letters, notes)** | 6 | 100% | 67% / 83% | 33% | 87 s | 6.8 / 3.8 |
| **Across formats** | 3 | 100% | 67% / 67% | 33% | 70 s | 10.3 / 8.0 |
| Live data (FX, FRED, prices) | 4 | 100% | 100% / 100% | 0% | 28 s | 5.8 / 2.2 |
| Web / current events | 4 | 100% | 100% / 100% | 25% | 63 s | 8.2 / 6.0 |
| Mixed (documents + web/data) | 4 | 100% | 100% / 75% | 50% | 126 s | 11.0 / 11.0 |
| General knowledge / chit-chat | 4 | 100% | 100% / 100% | 50% | 10 s | 2.0 / 0.0 |
| Outside the collection | 2 | 100% | 50% / 50% | 50% | 92 s | 8.5 / 7.0 |
| **Overall** | **52** | **100%** | **90% / 92%** (mean score 0.95 / 0.95) | 42% (0.59) | 64 s | 7.8 / 5.5 |

\*Measured with 4 questions running at once on one A100. Every tool-based answer carried citations. Run A
used `fed_research` in 26 questions, `data_analyst` in 14, `web_research` in 7 and `market_data` in 7. The
answers, judge verdicts and full agent traces are in `eval/results/final_agentic.jsonl` (run A),
`final_agentic_b.jsonl` (run B) and `final_naive_rag.jsonl`; the previous evaluation on the 93-PDF
collection is in `final_93docs_*`.

What the numbers show:

- **The new questions** (data files, Word, web pages, cross-format): 17/20 and 18/20 fully correct, naive
  RAG 8/20. All nine structured-data questions were right in both runs: rankings over the bank-level
  stress-test CSV, time series across 13 SEP releases, the NY Fed and SPF workbooks (where `UNEMPB` is only
  explained in the SPF's PDF documentation, which the agent looked up). Naive RAG answers a structured
  question only when a small table's rows happen to be in a retrieved card.
- **No regression from a 4x larger collection.** The original 32 questions scored 30/32 in both runs,
  against 31/32 on the 93-PDF collection (naive RAG: 14/32 against 12/32).
- **Remaining misses are partial answers.** Every partial and zero score was read by hand; in all of them
  the main point is right. Typical partials leave out a secondary fact (the 2025 investment growth figure,
  the "GENIUS Act" reporting source) or summarise a detail differently (the SLOOS CRE demand). In three
  answers a detail was wrong: the gap between the SCF Bulletin ($192,900) and the public Excel tables
  ($192,700) was put down to rounding (the Word guide says it is public versus internal data), a web source
  gave May 16 instead of May 15 for Powell's appointment as chair pro tempore, and a live-data answer called
  an oil price move from $68.55 to $102.48 a 36.2% gain (it is 49.5%), which the verifier missed. The one
  score of 0 (`oc02` in run B) is a judge error: the answer said that no December 2026 Beige Book exists and
  then summarised the September Dallas report, labelled as such; every detail matches the source.
- **What the evaluation changed.** The first run on the new collection exposed a run that failed when an
  LLM's JSON output was invalid (now the planner and verifier fall back), planner routing gaps (PDF-only
  numbers went to the data agent; a Jackson Hole question went to the web), and a data agent that took the
  first projection quarter (PQ1) of the stress-test paths as the starting value. The fix for the last one
  was a data dictionary shown with each table's schema. Two reference answers were relaxed to mark context
  the question did not ask for as optional (`wb02`, `cf01`).
- Earlier runs showed that the LLM judge "corrects" 2026 facts with its outdated training knowledge (for
  example insisting Powell is chair). The judge prompt says its knowledge is outdated, and judging runs in
  thinking mode. Re-judging the same answers changed one of 52 scores.

The development history, including the regressions found and fixed, is in the git log.

```bash
.venv/bin/python eval/run_eval.py -c 4              # all questions
.venv/bin/python eval/run_eval.py --ids fd01 mx02   # a subset
.venv/bin/python eval/run_eval.py --naive -c 4      # naive RAG baseline
.venv/bin/python -m pytest -q                       # offline unit tests (parsers, table engine, SQL, tools)
```

## Configuration (`.env` or environment)

| Variable | Default | Purpose |
| --- | --- | --- |
| `FEDRAG_GPU_URL`, `FEDRAG_API_KEY` | written by `colab_up.py` | GPU gateway (LLM, embeddings, rerank) |
| `FEDRAG_LLM_BASE_URL`, `FEDRAG_LLM_API_KEY`, `FEDRAG_LLM_MODEL` | gateway, `qwen3.8-27b` | any OpenAI-compatible LLM endpoint |
| `FEDRAG_LLM_THINKING_FLAG` | `1` | send Qwen's `enable_thinking`; set to `0` for other backends |
| `FEDRAG_HTTP_CONTACT` | empty | contact info added to the web User-Agent (Wikimedia requires one) |
| `FEDRAG_LLM_GPU_UTIL`, `FEDRAG_LLM_MAX_LEN`, `FEDRAG_LLM_MTP` | `0.62`, `65536`, `1` | vLLM settings (on the VM) |

## Repository layout

```
fedrag/
  ingest/        pdf_parser.py, html_parser.py, docx_parser.py, sheet_parser.py (one per format),
                 tables.py (grid -> table blocks -> wide/long tables), document.py (parts), chunker.py,
                 build_corpus.py (pages, chunks, table cards, DuckDB tables and views)
  retrieval/     bm25.py, index.py (hybrid search, filters, cards, incremental embeddings),
                 tables.py (sandboxed DuckDB access), text_format.py
  tools/         fed_docs.py, data_tools.py (SQL tools), web.py, market.py, base.py (Tool, RunContext)
  agents/        base.py (ReAct ToolAgent), planner.py, specialists.py, writer.py (synth/verify), prompts.py
  orchestrator.py  the pipeline;  evidence.py  citation registry;  llm.py  OpenAI-compatible client
  cli.py         fedrag ask / chat / search / tables / sql / ui
  ui/            app.py (explorer server: runs the pipeline, streams its trace as NDJSON, saves runs),
                 static/ (the page: pipeline graph, timeline, inspector; no build step)
gpu_server/      models.py, embed_corpus.py, gateway.py (FastAPI: auth, embeddings, rerank, LLM proxy),
                 launch.py (vLLM + gateway + tunnel + keeper), keeper.py, setup_vm.py
scripts/         fetch_corpus.py (discover + download the collection), colab_up.py (Colab bring-up),
                 colab_keepalive.py (heartbeat + token refresh)
eval/            questions.jsonl, run_eval.py
tests/           offline unit tests
docs/            explorer.png (the screenshot above)
federal_reserve/ the source files (not in git; metadata.csv lists their URLs and hashes)
```

## Notes and limitations

- The gateway is reachable through a Cloudflare quick tunnel protected by a random bearer token. The
  URL changes whenever the tunnel restarts; `colab_up.py` keeps `.env` current.
- `data/` (corpus, tables and embeddings) is generated, not committed: run steps 2-3, and step 4 embeds
  what is missing.
- The table engine is heuristic. It handles the layouts in this collection (multi-row merged headers,
  row groups, notes, chart sheets, stacked blocks); a few complex sheets (parts of the stress-test market
  shock workbook) come out with generic column names, and their text preview stays readable. Tables inside
  PDFs are extracted as text rows, not as SQL tables; the same data are in the CSV files where it matters
  (bank-level stress-test results).
- Speeches, FEDS Notes and press releases cover 2026; FOMC statements and SEPs cover 2023-2026.
- Web pages that block bots (paywalls, Wikipedia without `FEDRAG_HTTP_CONTACT`) cannot be fetched. The
  web agent then relies on search snippets and other sources.
- FRED data and ECB rates have publication lags; the data agent reports as-of dates.
- The LLM judge is the same model as the agents, so its scores are a guide; read the per-question
  outputs in `eval/results/final_*.jsonl` as well.
