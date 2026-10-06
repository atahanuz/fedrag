# Findings log

Dated notes on what was changed, how it was measured and what was learned. The README summarizes the
current state; this file keeps the history and the evidence behind it.

## 2026-10-06: larger collection, harder questions

### Collection: 391 → 725 documents

`scripts/fetch_corpus.py --discover` now also collects:

| Added | Documents |
| --- | ---: |
| Speeches by the Chair and Governors, 2025 | 117 |
| FEDS Notes, 2025 | 91 |
| Board press releases, 2025 | 74 |
| Chair's press conference transcripts, 2023-2025 (PDF) | 24 |
| Beige Books, 2023-2024 (PDF) | 16 |
| FOMC minutes, 2022 (PDF) | 8 |
| SR letters, 2024, and October 2026 | 5 |

The corpus grew from 10,441 to 15,768 chunks and from 846 to 978 SQL tables (20 stacked views); parsing
all 725 files takes 22 s. One discovered item was dropped: the August 22, 2025 FOMC "press release" is the
revised Statement on Longer-Run Goals, which has no press conference.

What the expansion revealed:

- The tool and planner descriptions hard-coded the old coverage ("speeches (2026)", "press conferences
  (2026)", "391 documents"). After an expansion the agents would have been told that 2025 speeches do not
  exist. These descriptions were corrected before measuring anything; coverage that the agents are told
  about should come from the index, and most of it already does (the corpus card).
- There is no February 2026 Monetary Policy Report: the Fed's own index lists June 2025 and then July 2026.
  The collection is complete; a question about "the February 2026 MPR" is a trap, not a gap.
- Topics now have depth: about 30 documents are about AI (speeches by six officials, FEDS Notes, FSR
  boxes, the Jackson Hole speech) and about 20 about stablecoins (GENIUS Act proposals, research, four
  Governors' speeches). Broad questions on these topics have many relevant sources rather than one.

### A harder question set: `eval/questions_hard.jsonl`

24 questions in three groups that the original 52 questions barely test. Every reference was written from
the documents themselves (searched with a grep over `data/corpus/pages.jsonl`), not from model answers.

- **Multi-source (8)**: the answer needs many documents: the target range after each 2025 meeting (8
  statements), the dissents at each 2025 meeting, the overall activity in each 2024 Beige Book (8
  editions), asset valuations in seven FSRs (2023-2026), the SEP unemployment median across seven
  meetings, four Governors' stablecoin speeches, tariffs in three Monetary Policy Reports, Board leadership
  changes over 21 months (press releases).
- **Ambiguous (8)**: underspecified questions with a defensible default: "What did the Fed decide at its
  last meeting?", "What's the current interest rate?", "Who dissented?" (no context), "What has Powell said
  recently about inflation?" (he stopped being Chair in May 2026), "How did the banks do in the stress
  test?" (which year), "Is inflation coming down?" (which measure). The reference names the acceptable
  interpretations; the answer must say which one it took.
- **Broad (8)**: overview questions across document types: the Fed's main concerns now, policy since 2023,
  what the Fed says about AI, the health of the banking system, what the Fed is doing about stablecoins,
  household finances, financial-stability risks, the labor-market view over 2026. References are rubrics:
  the points a good answer covers, of which it must get most right.

The judge prompt gained two rules: an answer to an ambiguous question that does not state its
interpretation scores at most 1, and broad answers are scored on how many of the rubric's points they cover
correctly. A new metric, documents cited per answer, counts distinct source documents (several pages of one
report count once).

### Baseline: the existing pipeline on the hard set

Run A (`eval/results/run_20261006-022435_base_hardA.jsonl`), the pipeline as it was (only the coverage
descriptions corrected), 4 questions at a time:

| | Fully correct | Mean score | Documents cited | Median time | Tool calls |
| --- | ---: | ---: | ---: | ---: | ---: |
| Multi-source (8) | 2 (25%) | 0.62 | 8.8 | 235 s | 14.9 |
| Ambiguous (8) | 6 (75%) | 0.81 | 2.9 | 149 s | 10.0 |
| Broad (8) | 3 (38%) | 0.69 | 9.1 | 395 s | 31.4 |
| All 24 | 11 (46%) | 0.71 | 6.9 | 235 s | 18.8 |

For comparison, the same pipeline scores about 91% on the original 52 questions.

What went wrong, from the answers and traces:

1. **Members of a set go missing.** "Each 2025 meeting" lost October; "each 2024 Beige Book" covered the
   first five editions and dropped September, October and December; the FSR question covered four of seven
   reports. `search_fed_documents` returns the six best passages overall, and a few documents crowd out the
   rest; the agent did not notice the gaps and the writer did not flag them.
2. **Exploration is slow and wasteful.** Broad questions took 300 to 820 s with 30 to 59 tool calls. The
   policy-history question read 15 statements one page at a time while a market-data task made 24 FRED
   calls, probing date windows to locate the dates the target range changed, although its first call had
   already returned the whole series (summarized as a sample).
3. **The tunnel times out long calls.** With 40-50k-token prompts and four questions at once, some LLM
   calls passed Cloudflare's 100-second limit (8 HTTP 524 errors in one run). The client retried while
   vLLM kept generating the abandoned request, which made everything slower still.
4. **Ambiguity was handled implicitly.** Six of eight ambiguous questions were right because the
   agents happened to pick the latest document; nothing in the pipeline asked for an interpretation to be
   stated, and nothing checked one.
5. **Conflicting sources are not reconciled.** The one 0 (inflation) quoted both a live FRED core PCE of
   3.01 percent and the Chairman's "about 3.2" without saying they differ. The judge read it as invented;
   both numbers were cited. The reference was incomplete (it did not anticipate two legitimate sources);
   it now accepts either figure when attributed, and all runs are re-judged with the final references.

### Round 1 (v1): read every member of a set, state interpretations

Changes:

- **`search_each_document`**: one query run inside each document of a set (named by type and dates, title,
  speaker or ids), returning the best passages of every member, oldest first. It ranks passages within
  each document (BM25 + dense, fused) and reranks all candidates in one call: 4.6 s for the eight 2024
  Beige Books, each one's national summary found at relevance 0.97.
- **At most two passages per document** in `search_fed_documents` (unless the search is inside one
  document), so one long report no longer fills all six slots.
- **Interpretations**: the planner writes `assumptions` for underspecified questions (shown in the
  explorer); the writer must state them first; the fact-checker checks that every member of a set is
  covered and that an interpretation is stated. The writer's evidence budget went from 28 to 40 items.
- **FRED step series**: a series that changes rarely (the target range) is summarized as its list of
  change points, so "when did the rate change" is one call instead of 24.
- **Gateway keep-alive**: an LLM call still running after 60 s starts its response and sends a space every
  15 s until the JSON is ready (fast calls keep their status code). No 524 errors since.

Result (`run_20261006-030250_v1_hard.jsonl`, judged with the original references):

| | Baseline | v1 |
| --- | ---: | ---: |
| Multi-source fully correct | 2 / 8 | **6 / 8** |
| Ambiguous | 6 / 8 | 5 / 8 |
| Broad | 3 / 8 | 3 / 8 |
| All 24 | 11 (46%) | **14 (58%)** |
| Documents cited per answer | 6.9 | 9.5 |
| Median time | 235 s | 248 s |

- The set questions are now complete: all eight 2025 meetings, all eight 2024 Beige Books, all three MPRs,
  the leadership changes.
- **A stated interpretation can be wrong.** For "What has Powell said recently about inflation?" the
  planner fixed "recently" as June to October 2026, a window in which Powell (no longer Chair) said nothing;
  the answer then reported nothing from Powell instead of his latest remarks (April 2026). Relative time
  words must mean "the most recent available", never a window that may be empty.
- **Broad questions did not improve and became more expensive** (median 540 s; 51 to 67 tool calls on
  some). The planner split them by document type (speeches, notes, press releases) without knowing which
  documents discuss the topic, so the AI answer missed the Monetary Policy Report, the FSRs and the Jackson
  Hole speech, and the stablecoin answer missed the 2025 actions and two Governors.
- **Cost is context, not GPU saturation.** The AI question sent 502k prompt tokens to the document agent in
  17 calls (tool results averaged 12k characters); vLLM showed the KV cache 30-60% full with no
  preemptions, but prefill of 40-60k-token conversations at about 2,500 tokens/s, so even the planner
  waited up to a minute in the queue while four questions ran at once.

### Round 2 (v2): retrieval-aware planning, "recent" means latest available

Changes: before planning, one hybrid search lists the 12 documents most related to the question (type,
date, title, best section; 3-4 s) and the planner gets that list; "recently"/"latest" mean the most recent
items available, never a fixed window; search results show at most 1,100 characters per passage (the
evidence store keeps the whole chunk for the writer). The explorer shows the list in the planner's step.

Result (`run_20261006-034455_v2_hard.jsonl`): 13 of 24 fully correct, against 14 for v1: the Powell
question recovered (0 → 1), the inflation, policy-history and stablecoin questions reached 2, while four
others moved from 2 to 1. **One run per version is too noisy to separate versions** that differ by one or
two questions out of eight per group; the final comparison uses two runs per system.

Two causes of the persistent partial scores on broad questions, from the plans:

- **The interpretation mechanism narrowed broad questions.** For "How has the Fed's view of the labor
  market changed over 2026?" the planner wrote the assumption "'the Fed's view' = the minutes" and read
  only the minutes, missing the statements ("job gains have kept pace with the workforce"), the projections
  (4.4 → 4.1 percent) and the Chairman. Assumptions should pick which item (meeting, report, rate, year),
  never shrink a question to one document type.
- **The planner skipped the official voices.** For "What does the Fed say about AI?" it saw the Jackson
  Hole speech in its list but planned Governors' speeches and research notes only, missing the Chairman,
  the Monetary Policy Report and the FSRs.

### Round 3 (v3): official voices first; interpretations pick an item, never a scope

Changes to the planner: assumptions may only resolve which item is meant (meeting, report, rate, year,
person), never narrow a broad question to one document type; for what "the Fed" says or does, the official
voices come first (FOMC statements, minutes and projections; the Chair's press conferences and major
speeches; the Monetary Policy, Financial Stability and Supervision reports; Board actions), then Governors'
speeches, then staff research; for how a view evolved, the same kind of document at each point in time
plus the projections or data that quantify it. Two worked examples on topics outside the question set
(commercial real estate; how the FOMC described inflation over 2025) show the pattern.

Result (`run_20261006-043126_v3_hard.jsonl`): **16 of 24 fully correct (67%), mean score 0.83**:
multi-source 6/8, ambiguous 6/8, broad 4/8. The AI and banking questions reached 2 for the first time (the
Jackson Hole speech, the MPR and the FSRs were now in the plan).

### Two infrastructure problems the harder questions exposed

- **JSON whitespace runaway, with Qwen too.** Reproducing a 333-second planner call: in one of three tries
  Qwen3.8 emitted 2,500 tokens of which 810 characters were content, the rest whitespace inside the JSON
  object, until the token limit; the plan then failed to parse and was retried. The same failure had been
  found with Gemma 4 (where it hung every structured call), but it also hits Qwen intermittently under
  load, in every JSON-constrained call: the planner, the fact-checker and each agent's forced
  `submit_findings`. `gpu_server/launch.py` now serves every model with xgrammar's
  `disable_any_whitespace`; afterwards the same planner call took 4-8 s in three of three tries. (The
  Qwen-vs-Gemma comparison above ran Qwen without this setting.)
- **The gateway's memory grows.** After the heavy runs, the embedder and reranker held 31 GB instead of
  24 GB (PyTorch keeps the cache of the large rerank batches of `search_each_document`), and a vLLM restart
  failed for lack of 0.4 GB. `launch.py --restart-llm` now restarts the gateway first.

### Final comparison: the old and the new pipeline, side by side

Protocol: the old pipeline (frozen copy of the code before this work, with only the corrected coverage
descriptions) and the new one (commit `3133578`) ran at the same time on the same GPU, with the same
gateway and vLLM settings (keep-alive, no-whitespace JSON), two questions each, so both saw the same load.
Both were judged by the same judge with the same final references.

Pair A (`run_20261006-054452_final_base_A`, `run_20261006-055416_final_new_A`):

| | Old pipeline | New pipeline |
| --- | ---: | ---: |
| Multi-source fully correct | 4 / 8 | 6 / 8 |
| Ambiguous | 5 / 8 | 6 / 8 |
| Broad | 2 / 8 | 4 / 8 |
| **All 24** | **11 (46%)** | **16 (67%)** |
| Mean score | 0.71 | 0.83 |
| Documents cited per answer | 8.2 | 11.1 |
| Tool calls per question | 14.6 | 26.2 |
| Median time | 177 s | 242 s |

Nine questions improved and two got worse. The new pipeline reads more (26 tool calls against 15) and is
slower on narrow questions; on broad questions both took about 330 s at the median.

One of the two regressions shows a remaining gap: for "Who dissented?" the planner took "the most recent
meeting" to be April 2026 (four dissents) instead of September 2026: the related-documents search for a
three-word question surfaced April's documents, and the latest meeting date was buried in a long list of
dates. The collection card given to the planner and the document agent now starts with the most recent
document of each type ("FOMC meeting 2026-09-16; FOMC minutes 2026-07-29; Beige Book 2026-09-02; ...").
This change came after the final runs and is not part of the measured version.

Pair B (`final_hard_old_b`, `final_hard_new_b`) ran four questions per system at once while another
check ran, so the GPU served about ten questions at a time (median 700 s); its times are not comparable
with pair A. The new pipeline scored 11 and the old one 12. One answer of the new pipeline failed because
an agent's conversation passed the 65,536-token context of the model (the stablecoin question, after
reading many speeches whole).

**Both pairs together** (`eval/results/final_hard_comparison.summary.json`):

| | Old pipeline | New pipeline |
| --- | ---: | ---: |
| Fully correct (runs A / B) | 11 / 12 of 24 | 16 / 11 of 24 |
| Fully correct, both runs | 23 / 48 (48%) | **27 / 48 (56%)** |
| Mean score | 0.72 | **0.77** |
| Multi-source, mean score | 0.75 | 0.81 |
| Ambiguous, mean score | 0.75 | 0.84 |
| Broad, mean score | 0.66 | 0.66 |
| Documents cited per answer | 8.4 | 10.7 |
| Tool calls / LLM calls per question | 14.9 / 10.8 | 24.4 / 15.1 |
| Prompt tokens per question | 135k | 199k |

Over all runs of the new pipeline's last versions (v3, A, B: 16, 16, 11) and of the old one (the first
baseline, A, B: 11, 11, 12), the averages are 60% and 47%.

On the original 52 questions the new pipeline scored 46 of 52 (89%, mean 0.94, routing 52/52)
(`final_agentic_725docs.jsonl`), against 47 and 48 for the old pipeline on the 391-document collection:
no regression beyond run-to-run noise. Its median time rose from about 66 s to 88 s.

### Conclusions

- **What worked: reading every member of a set and stating interpretations.** The gains are in the
  multi-source and ambiguous questions, and they are consistent where the mechanism applies: the 2024 Beige
  Books, the dissents at each 2025 meeting and the current-rate question were right in every run of the new
  pipeline and wrong in every run of the old one.
- **Broad questions did not improve on average.** Their scores swung between runs (4, 4, 1 of 8 for the
  new pipeline; 3, 2, 3 for the old), because the judge scores them against a rubric of many specific
  points and both pipelines cover a different subset each time. Eight questions per group cannot separate
  a real gain of one question from noise; more questions and repeated runs would be needed.
- **The price is cost.** The new pipeline makes 60% more tool calls and sends 45% more prompt tokens; on a
  shared GPU that is latency. On narrow questions it is slower for little gain.
- **Infrastructure mattered as much as prompts.** The Cloudflare 524 timeouts and the JSON whitespace
  runaway (which also hit Qwen) cost minutes per question before they were fixed, and the run that ignored
  them (the first baseline, 8 timeouts) gave the same accuracy but much longer times.
- **The judge is part of the measurement.** Several zeros and ones on inspection were faithful answers
  marked down (an exact quote of conflicting sources; a correct answer phrased differently from the
  reference). The references were corrected once (the inflation question) and all final runs used the
  same references.

### Follow-up fixes (after the measured version)

Found in the final runs and fixed afterwards; checked on the affected questions only
(`final_hard_followup_*.jsonl`):

- **Which meeting is the latest.** The collection card now opens with the most recent document of each
  type. "Who dissented?" then resolved to September 2026 in both checks (it had picked April once).
- **Named people.** Press-conference transcripts now carry their Chair as speaker and in the title ("Chair
  Powell's FOMC press conference, April 29, 2026"), so a speaker filter finds a Chair's press conferences
  with his speeches; the planner is told not to assume a person's current role. "What has Powell said
  recently about inflation?" went from 0 to 1 (it now finds the April 2026 press conference).
- **Context overflow.** When an agent's conversation passes about 150,000 characters, its oldest tool
  results are shortened to their first lines and their evidence IDs (the writer still reads the full
  passages). The stablecoin question that had failed with a context-length error finished with 15 results
  shortened.
- **Known open issues**: the writer sometimes computes a figure itself despite the rule (a cumulative
  change given as 2.8 instead of 4.4 points), which the fact-checker missed; and the broad questions need a
  larger question set before further tuning.
