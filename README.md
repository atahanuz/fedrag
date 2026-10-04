# fedrag — Agentic RAG over Federal Reserve publications

A multi-agent research assistant that answers questions from **93 Federal Reserve Board publications**
(FOMC minutes, Beige Books, Monetary Policy and Financial Stability Reports, stress tests, supervision
reports, annual reports and FEDS working papers, 2022 to September 2026). It also handles questions that
are only partly about those documents, or not at all:

| Question type | What happens |
| --- | --- |
| What a Fed publication says | `fed_research` agent searches, filters, reads pages and tables, and cites them |
| After the collection ends, or outside it | `web_research` agent searches the web and reads pages |
| Live numbers (FX, fed funds, yields, CPI, prices) | `market_data` agent calls FX / FRED / market APIs and a calculator |
| Several of the above | the planner runs several agents in parallel and the writer merges their findings |
| General knowledge or chit-chat | answered directly by the LLM, no tools |

All models are open-weight and self-hosted on a Colab A100 80GB. No proprietary API keys are needed.

## How it works

```mermaid
flowchart LR
    Q[Question + chat history] --> P[Planner / router]
    P -- no tools needed --> D[Direct answer]
    P -- tasks --> X{{Parallel task waves}}
    X --> F[fed_research agent]
    X --> W[web_research agent]
    X --> M[market_data agent]
    F & W & M --> E[(Evidence store<br/>D# / W# / M# IDs)]
    E --> S[Synthesizer]
    S --> V{Verifier}
    V -- accept --> A[Cited answer + sources + trace]
    V -- revise --> S
    V -- research --> X
```

1. **Planner (router).** Classifies the question, rewrites follow-ups into standalone questions, and
   decomposes the work into self-contained tasks for specialist agents. It is given today's date and a
   description of the collection (which meetings and reports exist), so it knows when the documents
   cannot be enough. It returns a JSON-schema-constrained plan. Comparisons over time become parallel
   tasks (one per meeting or report), and tasks may depend on each other's results.
2. **Specialist agents.** Each one is a ReAct loop over native function calling. In each turn the model
   may call several tools at once; they run concurrently. The agent finishes by calling
   `submit_findings` (answer, cited key facts, confidence, gaps).
   - `fed_research`: `search_fed_documents` (hybrid search with type, date and document filters),
     `list_fed_documents` (resolves "latest", "the June meeting" and so on), `get_document_outline`,
     `read_document_pages` (whole pages and tables), `expand_context` (neighbouring passages), `calculator`.
   - `web_research`: `web_search` (DuckDuckGo, with news and recency filters), `fetch_webpage`
     (HTML or PDF, query-focused extraction), `calculator`.
   - `market_data`: `get_exchange_rate` and `get_exchange_rate_history` (ECB via Frankfurter, with a
     fallback source), `get_economic_series` (FRED, with year-over-year and difference transforms),
     `search_economic_series`, `get_market_prices` (Yahoo via the local `stockcache`), `calculator`.
3. **Corrective step.** If no agent found any evidence, a web-research task is added automatically.
4. **Synthesizer.** Writes the answer from the findings and the evidence excerpts, with an inline
   citation (`[D3]`, `[W2]`, `[M1]`) on every fact.
5. **Verifier.** Checks support, completeness and date consistency. It returns `accept`, `revise` (the
   synthesizer fixes the listed problems) or `research` (follow-up tasks run, then the answer is
   rewritten).
6. **Output.** The answer, its source list (document citations link to the exact PDF page on
   federalreserve.gov), the plan, every agent's findings and a full trace of tool calls.

### Retrieval

- **Parsing** (`fedrag/ingest/pdf_parser.py`): a layout-aware PyMuPDF extractor. It rebuilds table rows
  from span geometry (`First Citizens | 6.7`), drops chart axis labels and running headers, removes
  footnote markers and fixes hyphenation. Two-column report pages keep their reading order.
- **Sections** (`chunker.py`): every paragraph gets a section path from the PDF outline plus headings
  detected from the font, for example `Federal Reserve Bank of Chicago > Manufacturing`. FOMC minutes
  have no outline, so their standard headings are recognised from formatting.
- **Chunks:** 6,368 overlapping chunks of about 260 words that never cross a top-level section, so a
  Beige Book chunk belongs to exactly one District. Each chunk is embedded with a contextual header
  (document title, date, section).
- **Search** (`fedrag/retrieval/index.py`): metadata filter, then BM25 (sparse matrix) and dense search
  with **Qwen3-Embedding-8B** (4096-d), fused with reciprocal rank fusion, then the top 40 reranked by
  **Qwen3-Reranker-4B**. Without the GPU it falls back to BM25 only.

### Models (all on one A100 80GB)

| Role | Model | Notes |
| --- | --- | --- |
| LLM for every agent | `Qwen/Qwen3.8-27B-FP8` via vLLM 0.30 | tool calling (`qwen3_xml` parser), JSON-schema output, MTP speculative decoding, about 95 tok/s per stream, 64K context, thinking mode off |
| Embeddings | `Qwen/Qwen3-Embedding-8B` | corpus embedded once on the GPU (about 6 min); queries use an instruction prefix |
| Reranker | `Qwen/Qwen3-Reranker-4B` | P("yes") relevance; about 0.2 s for 40 passages |

Qwen3.8-27B was chosen because it was the strongest instruction-following and agentic model that fits
in 80GB next to the retrieval models (IFBench 79.5, Terminal-Bench 73). FP8 weights run on the A100
through vLLM's Marlin kernels.

## Setup

```bash
# 1. Environment (Python 3.11+)
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -e ".[ui,dev]"

# 2. Parse the PDFs into pages, sections and chunks (about 6 s; writes data/corpus/)
.venv/bin/python -m fedrag.ingest.build_corpus

# 3. Start the GPU server on Colab (see COLAB_GUIDE.md for the high-RAM A100 patch).
#    Creates the session, installs vLLM, downloads models, embeds the corpus if data/index/ is missing,
#    starts gateway + vLLM + tunnel, writes FEDRAG_GPU_URL / FEDRAG_API_KEY to .env, then keeps the VM
#    alive while it is used.
.venv/bin/python scripts/colab_up.py          # about 12 min on a fresh VM
.venv/bin/python -m fedrag status             # gateway, LLM and index check
```

`scripts/colab_up.py --stop` releases the VM. Colab reclaims a VM about 20 minutes after the last
kernel execution, even while the servers in the background are busy, so `colab_up.py` keeps running a
tiny status cell every 4 minutes. It also repairs a dropped tunnel and updates `.env` with the new URL.
It stops after 60 minutes without gateway requests, and Colab then releases the VM.

## Usage

```bash
.venv/bin/python -m fedrag ask "What did the FOMC decide in July 2026, and who dissented?"
.venv/bin/python -m fedrag ask -v --save run.json "..."   # show tool results and save the full trace
.venv/bin/python -m fedrag chat                           # multi-turn, follow-up questions work
.venv/bin/python -m fedrag search "CET1 ratio" --type stress_test   # raw retrieval, no agents
.venv/bin/python -m fedrag.ui.app                         # web UI on http://127.0.0.1:7860
```

The web UI streams every agent as a collapsible panel listing its tool calls and findings, and links each
citation to its source.

Example CLI trace:

```
     planner    6.1s intent=mixed tasks=3 — three parts: September decision (after the collection) ...
                     • web_research t1: Find the outcome of the September 2026 FOMC meeting ...
                     • market_data t2: Retrieve the current fed funds target range (DFEDTARL/DFEDTARU) ...
                     • fed_research t3: July 28-29, 2026 FOMC minutes: target range and vote ...
web_research    6.1s ▶ Find the outcome of the September 2026 FOMC meeting ...
 market_data    6.1s ▶ Retrieve the current fed funds target range ...
fed_research    6.1s ▶ July 28-29, 2026 FOMC minutes: target range and vote ...
 market_data    7.9s   ↳ get_economic_series({"series_id": "DFEDTARU", "start_date": "2026-06-01"})
fed_research    8.0s   ↳ list_fed_documents({"doc_type": "meeting_minutes", "date_from": "2026-07-01"})
web_research    8.2s   ↳ web_search({"query": "FOMC September 2026 decision", "news": true})
...
    verifier   58.3s verdict=accept
```

## Evaluation

`eval/questions.jsonl` has 32 questions in 7 categories, with reference answers taken from the
documents, from live data on 2026-10-04, or from the web. `eval/run_eval.py` scores:

- **routing:** every expected agent ran and no forbidden agent ran;
- **correctness:** an LLM judge compares the answer with the reference (2 correct, 1 partial, 0 wrong);
- **grounding:** the share of tool-based answers that carry citations;
- **cost:** latency, LLM calls and tool calls.

RESULTS_PLACEHOLDER

```bash
.venv/bin/python eval/run_eval.py -c 4              # all questions
.venv/bin/python eval/run_eval.py --ids fd01 mx02   # a subset
.venv/bin/python -m pytest -q                       # offline unit tests (parser, chunker, BM25, tools)
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
  ingest/        pdf_parser.py (layout-aware extraction), chunker.py (sections + chunks), build_corpus.py
  retrieval/     bm25.py, index.py (hybrid search, filters, navigation), text_format.py
  tools/         fed_docs.py, web.py, market.py, base.py (Tool, RunContext)
  agents/        base.py (ReAct ToolAgent), planner.py, specialists.py, writer.py (synth/verify), prompts.py
  orchestrator.py  the pipeline;  evidence.py  citation registry;  llm.py  OpenAI-compatible client
  cli.py, ui/app.py
gpu_server/      models.py, embed_corpus.py, gateway.py (FastAPI: auth, embeddings, rerank, LLM proxy),
                 launch.py (vLLM + gateway + tunnel), setup_vm.py
scripts/         colab_up.py
eval/            questions.jsonl, run_eval.py
tests/           offline unit tests
federal_reserve/ the source PDFs (not in git; metadata.csv lists their URLs)
```

## Notes and limitations

- The gateway is reachable through a Cloudflare quick tunnel protected by a random bearer token. The
  URL changes whenever the tunnel restarts; `colab_up.py` keeps `.env` current.
- `data/` (corpus and embeddings) is generated, not committed: run step 2, and step 3 embeds on first use.
- Web pages that block bots (paywalls, Wikipedia without `FEDRAG_HTTP_CONTACT`) cannot be fetched. The
  web agent then relies on search snippets and other sources.
- FRED data and ECB rates have publication lags; the data agent reports as-of dates.
- The LLM judge is the same model as the agents, so its scores are a guide; the per-question outputs in
  `eval/results/` are meant to be read.
