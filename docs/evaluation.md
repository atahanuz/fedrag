# Evaluation

Benchmarks, baselines and model comparisons. Every run's answers, judge verdicts and full agent traces are committed in [`eval/results/`](../eval/results); [findings.md](findings.md) is the dated log of what was changed, measured and learned.

## The main benchmark (52 questions)

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

## Harder questions: many sources, ambiguous and broad (725-document collection)

`eval/questions_hard.jsonl` adds 24 questions the first set barely tests: 8 that need many documents (the
target range after each 2025 meeting, the dissents at each 2025 meeting, the overall activity in each of the
eight 2024 Beige Books, asset valuations in seven FSRs, four Governors' stablecoin speeches), 8 ambiguous
ones ("What did the Fed decide at its last meeting?", "What's the current interest rate?", "Who
dissented?", "What has Powell said recently about inflation?") and 8 broad ones ("What are the Fed's main
concerns right now?", "What does the Fed say about AI?", "How has policy evolved since 2023?"). The
references were written from the documents; for ambiguous questions they name the acceptable readings,
and for broad ones the points a good answer covers.

```bash
.venv/bin/python eval/run_eval.py --questions questions_hard.jsonl -c 4
```

The pipeline before and after this work (the old one frozen as it was), run side by side on the same GPU
twice and judged with the same references:

| | Before | After |
| --- | ---: | ---: |
| Fully correct (two runs) | 23 / 48 (48%) | **27 / 48 (56%)** |
| Mean score: multi-source / ambiguous / broad | 0.75 / 0.75 / 0.66 | **0.81 / 0.84** / 0.66 |
| Documents cited per answer | 8.4 | 10.7 |
| Tool calls per question | 14.9 | 24.4 |
| Original 52 questions | 47-48 / 52 (391 docs) | 46 / 52 (725 docs) |

What changed, and what each change did (details and every run in [`findings.md`](findings.md)):

- **Every member of a set is read**: `search_each_document` runs one query inside each document of a set
  (each 2024 Beige Book: eight national summaries in 4.6 s), and ordinary search returns at most two
  passages per document. The set questions that the old pipeline always got partly wrong (it dropped three
  of eight Beige Books, the October meeting, three of seven FSRs) became right in every run.
- **Ambiguity is stated, not guessed silently**: the planner records how it reads an underspecified
  question, the writer says it first ("Taking 'the last meeting' to mean the September 15-16, 2026 FOMC
  meeting: ..."), and the fact-checker checks it. Two lessons from the runs: a stated interpretation can be
  wrong ("recently" read as a fixed window in which Powell had said nothing), and it must pick an item,
  never narrow the question ("the Fed's view" read as "the minutes").
- **Broad questions are planned from what the collection holds**: one search before planning lists the
  documents most related to the question, and the planner covers the official voices (Committee, Chair,
  Board reports) before Governors and research. Broad answers improved in some runs but not on average:
  their scores swing by several questions between runs.
- **Infrastructure**: slow LLM calls are kept alive through the Cloudflare tunnel (no 524 errors), every
  model is served without free whitespace in JSON (Qwen, too, sometimes padded a plan with 2,000 spaces), a
  FRED step series is summarized as its change points, and agents shorten their oldest tool results before
  they overflow the context.

The cost is more reading: 60% more tool calls and 45% more prompt tokens per question. The full log of
measurements, failures and fixes is in [`findings.md`](findings.md).

## Model comparison: Qwen3.8-27B vs Gemma 4 31B (2026-10-05)

The same pipeline (prompts, tools, retrieval, sampling settings) with `google/gemma-4-31B-it` as the LLM of
every agent, run twice on the 52 questions. Gemma ran in FP8 (quantized on load), with vLLM's native
`gemma4` tool-call and reasoning parsers. Under JSON-schema constrained decoding it padded objects with
whitespace until the token limit instead of closing them (the planner, the fact-checker and the forced
"submit findings" call all hung), so it is served with `disable_any_whitespace`. Both systems were judged
by the same judge (Qwen3.8-27B, thinking mode) against the same references; Gemma also judged all four
runs as a cross-check. (Qwen ran without the no-whitespace JSON setting that was added later; see
[`findings.md`](findings.md).)

| | Qwen3.8-27B (runs A / B) | Gemma 4 31B (runs A / B) |
| --- | --- | --- |
| Fully correct, Qwen judge | **47 / 48** of 52 (91%) | 37 / 38 of 52 (72%) |
| Fully correct, Gemma judge | **50 / 52** (98%) | 50 / 47 (93%) |
| Fully correct, my reading of every disputed answer | **47 / 48** | about 42 / 42 (81%) |
| Mean score (Qwen judge) | 0.95 | 0.86 |
| Routing | 52/52 | 50/52 (both runs) |
| Tool-based answers with citations | 100% | 100% |
| Median time per question (4 at a time) | 67 s | **45 s** |
| Tool calls / LLM calls per question | 5.6 / 7.9 | 3.4 / 7.5 |
| Prompt / generated tokens per question | 49k / 2.6k | **35k / 1.1k** |

- **Both judges rank Qwen higher**, Gemma included, so the gap is not Qwen favoring itself. The Gemma
  judge is more lenient on omissions for both systems.
- **Where Gemma loses**: it researches less (3.4 tool calls against 5.6) and writes shorter answers, so it
  drops secondary facts the reference asks for: "interagency" in SR 26-2, the bank counts in the stress-test
  comparison, "up from 3.6 percent in June" for the SEP, the FSR context of the oil question. Comparisons
  across documents (0.75 against 1.00), mixed-source questions (0.62 against 0.94) and cross-format
  questions (0.58 against 0.83) suffer most.
- **Errors**: one Gemma run reported the stress-test decline to the end of the horizon instead of the
  minimum (0.6 and 0.1 points instead of 1.8 and 1.6, the mistake the data dictionary was written to
  prevent for Qwen), one counted five April dissents while naming four, and both runs answered the oil
  question from market data alone without the FSR. It also sent the QE/QT definition to the document agent
  instead of answering directly. Qwen's errors in its two runs are listed above.
- **About 9 of Gemma's 29 partial scores are judge strictness** (it gave both rate ranges without writing
  "25 basis points higher"; it stated both survey values without the difference), hence the 81% estimate.
  The live-data questions ran about 18 hours after the Qwen runs, so the S&P 500 answer used a newer close
  than the reference; I counted those as correct.
- **Caveats**: the prompts, the planner examples and the data dictionaries were tuned on Qwen during
  development, and the sampling settings (temperature 0.3, top-k 20) are Qwen's, not Gemma's recommended
  ones (temperature 1.0, top-k 64). The comparison measures each model in this system as built, not the
  models in general.

Result files: `eval/results/final_gemma_{a,b}.jsonl` (judged by Qwen), `final_judge_gemma_*.jsonl` (all four
runs judged by Gemma), `final_model_comparison.summary.json`. To repeat it:

```bash
.venv/bin/python scripts/switch_llm.py gemma          # serve Gemma on the running VM (about 3-7 min)
.venv/bin/python eval/run_eval.py -c 4 --no-judge --tag gemmaA
.venv/bin/python scripts/switch_llm.py qwen           # back to the judge model
.venv/bin/python eval/run_eval.py --rescore eval/results/run_<stamp>_gemmaA.jsonl --tag gemmaA_judged
.venv/bin/python eval/compare.py qwen=eval/results/final_agentic.jsonl,eval/results/final_agentic_b.jsonl \
    gemma=eval/results/run_<stamp>_gemmaA_judged.jsonl
```
