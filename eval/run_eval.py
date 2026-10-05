"""Evaluate the agentic RAG system on eval/questions.jsonl.

For every question it records the answer, the agents used and the full trace, then scores:

* routing   - all expected agents ran and no forbidden agent ran
* correctness - LLM judge vs. the reference answer: 2 correct, 1 partial, 0 wrong
* grounding - share of tool-based answers that carry citations
* cost      - latency, LLM calls and tokens

    python eval/run_eval.py                       # all questions, 3 at a time
    python eval/run_eval.py --ids fd01 mx02 -c 1  # a subset
    python eval/run_eval.py --rescore eval/results/run_X.jsonl   # re-judge saved answers
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fedrag.llm import LLM  # noqa: E402
from fedrag.orchestrator import Orchestrator  # noqa: E402
from fedrag.retrieval.text_format import citation_label  # noqa: E402

HERE = Path(__file__).resolve().parent
TOOL_AGENTS = {"fed_research", "data_analyst", "web_research", "market_data"}

JUDGE_SYSTEM = """You grade answers of a research assistant about the Federal Reserve, economics and finance.
Today's date is {today}. Compare the assistant's answer with the reference answer written by an expert.

Scores:
2 = correct: all key facts of the reference are present and right (wording may differ; extra correct
    detail is fine).
1 = partially correct: the main point is right but some key facts are missing or slightly wrong.
0 = wrong: the main point is missing or wrong, or the answer contains invented facts that contradict
    the reference.

Your own background knowledge is OUTDATED: it ends well before today's date. Officials, policy rates and
events may have changed since. Never mark a statement wrong because it conflicts with what you remember;
judge only against the reference answer. Details that go beyond the reference and are attributed to the
answer's sources are not errors unless they contradict the reference.

Special cases:
- Live data and current events: the reference shows values as of its date. Accept values that differ
  slightly if the answer states a plausible as-of date; penalise stale data presented as current.
  Where the reference only describes what a good answer must contain (e.g. "must give the rate with the
  date of the latest decision"), score whether the answer does that.
- Unanswerable / out-of-collection questions: the answer must clearly say the information is not available
  (it may add correctly sourced alternatives). Inventing content scores 0.
Be strict about numbers, dates and names."""

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {"explanation": {"type": "string"}, "score": {"type": "integer", "enum": [0, 1, 2]}},
    "required": ["explanation", "score"],
    "additionalProperties": False,
}


async def judge(llm: LLM, q: dict, answer: str, today: str) -> dict:
    user = (f"Question: {q['question']}\n\nReference answer: {q['reference']}\n\n"
            f"Assistant's answer:\n{answer}\n\nGrade it. Return JSON.")
    # thinking mode: in non-thinking mode the judge misread several correct answers
    return await llm.chat_json([{"role": "system", "content": JUDGE_SYSTEM.format(today=today)},
                                {"role": "user", "content": user}],
                               JUDGE_SCHEMA, agent="judge", schema_name="grade", temperature=0.0, max_tokens=8000,
                               thinking=True)


def routing_ok(q: dict, used: list[str]) -> bool:
    used_tools = set(used) & TOOL_AGENTS
    if not set(q["expected_agents"]) <= used_tools:
        return False
    if q.get("any_of_agents") and not set(q["any_of_agents"]) & used_tools:
        return False
    if set(q.get("forbidden_agents", [])) & used_tools:
        return False
    return True


async def run_one(orch: Orchestrator, q: dict, sem: asyncio.Semaphore, today: dt.date) -> dict:
    async with sem:
        t0 = time.time()
        events: list = []  # kept even when the run fails, to see where it stopped
        try:
            res = await orch.run(q["question"], today=today,
                                 listeners=[lambda e: events.append(e.__dict__) if e.type != "delta" else None])
            rec = {"id": q["id"], "category": q["category"], "question": q["question"],
                   "answer": res.answer, "agents_used": res.agents_used, "plan": res.plan,
                   "sources": [{k: s[k] for k in ("id", "kind", "source", "url")} for s in res.sources],
                   "verifications": [v["verdict"] for v in res.verifications], "usage": res.usage,
                   "seconds": res.seconds, "trace": res.trace, "error": None}
        except Exception as e:
            rec = {"id": q["id"], "category": q["category"], "question": q["question"], "answer": "",
                   "agents_used": [], "plan": {}, "sources": [], "verifications": [], "usage": {},
                   "seconds": round(time.time() - t0, 1), "trace": events, "error": f"{type(e).__name__}: {e}"}
        print(f"  {q['id']:<5} {rec['seconds']:>6.1f}s agents={','.join(a for a in rec['agents_used'] if a in TOOL_AGENTS) or '-'}"
              + (f" ERROR {rec['error']}" if rec["error"] else ""), flush=True)
        return rec


NAIVE_PROMPT = """Answer the question using the context passages from Federal Reserve publications.
Cite passages as [1], [2]. If the context does not contain the answer, say so. Today's date is {today}.

Context:
{context}

Question: {question}"""


async def run_naive(orch: Orchestrator, q: dict, sem: asyncio.Semaphore, today: dt.date) -> dict:
    """Baseline: one hybrid retrieval over the documents + one LLM call. No routing, tools or agents."""
    async with sem:
        t0 = time.time()
        hits = await orch.index.search(q["question"], k=8)
        context = "\n\n".join(f"[{i + 1}] {citation_label(h.chunk)}\n{h.chunk['text']}" for i, h in enumerate(hits))
        r = await orch.llm.chat([{"role": "user", "content": NAIVE_PROMPT.format(
            today=today.strftime("%B %d, %Y"), context=context, question=q["question"])}],
            agent="naive", temperature=0.3, max_tokens=1500)
        rec = {"id": q["id"], "category": q["category"], "question": q["question"], "answer": r.content,
               "agents_used": ["naive"], "plan": {}, "sources": [{"id": str(i + 1), "kind": "doc",
                                                                   "source": citation_label(h.chunk), "url": None}
                                                                  for i, h in enumerate(hits)],
               "verifications": [], "usage": {"llm_calls": 1, "tool_calls": {}}, "seconds": round(time.time() - t0, 1),
               "trace": [], "error": None}
        print(f"  {q['id']:<5} {rec['seconds']:>6.1f}s naive", flush=True)
        return rec


def summarize(records: list[dict]) -> dict:
    by_cat: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        by_cat[r["category"]].append(r)

    def agg(rs: list[dict]) -> dict:
        tool_rs = [r for r in rs if set(r["agents_used"]) & (TOOL_AGENTS | {"naive"})]
        return {
            "n": len(rs),
            "routing_acc": (round(sum(r["routing_ok"] for r in rs) / len(rs), 3)
                            if all(r["routing_ok"] is not None for r in rs) else None),
            "correct_rate": round(sum(r["score"] == 2 for r in rs) / len(rs), 3),
            "mean_score": round(sum(r["score"] for r in rs) / len(rs) / 2, 3),
            "cited_rate": round(sum(bool(r["sources"]) for r in tool_rs) / len(tool_rs), 3) if tool_rs else None,
            "median_seconds": round(statistics.median(r["seconds"] for r in rs), 1),
            "mean_llm_calls": round(statistics.mean(r["usage"].get("llm_calls", 0) for r in rs), 1),
            "mean_tool_calls": round(statistics.mean(sum(r["usage"].get("tool_calls", {}).values()) for r in rs), 1),
        }

    return {"overall": agg(records), "by_category": {c: agg(rs) for c, rs in by_cat.items()}}


def print_table(summary: dict) -> None:
    hdr = f"{'category':<20}{'n':>4}{'routing':>9}{'correct':>9}{'score':>8}{'cited':>8}{'med s':>8}{'llm':>6}{'tools':>7}"
    print("\n" + hdr + "\n" + "-" * len(hdr))
    rows = list(summary["by_category"].items()) + [("OVERALL", summary["overall"])]
    for c, a in rows:
        cited = f"{a['cited_rate']:.2f}" if a["cited_rate"] is not None else "-"
        routing = f"{a['routing_acc']:.2f}" if a["routing_acc"] is not None else "-"
        print(f"{c:<20}{a['n']:>4}{routing:>9}{a['correct_rate']:>9.2f}{a['mean_score']:>8.2f}"
              f"{cited:>8}{a['median_seconds']:>8.1f}{a['mean_llm_calls']:>6.1f}{a['mean_tool_calls']:>7.1f}")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", nargs="*")
    ap.add_argument("--category")
    ap.add_argument("-c", "--concurrency", type=int, default=3)
    ap.add_argument("--no-verify", action="store_true")
    ap.add_argument("--thinking", action="store_true", help="enable the LLM's thinking mode for specialist agents")
    ap.add_argument("--rescore", help="re-judge answers from a saved results file")
    ap.add_argument("--naive", action="store_true", help="naive RAG baseline: one retrieval + one LLM call")
    ap.add_argument("--tag", default="")
    args = ap.parse_args()

    questions = [json.loads(line) for line in open(HERE / "questions.jsonl")]
    qmap = {q["id"]: q for q in questions}
    if args.ids:
        questions = [q for q in questions if q["id"] in args.ids]
    if args.category:
        questions = [q for q in questions if q["category"] == args.category]
    today = dt.date.today()
    today_s = today.strftime("%B %d, %Y")

    if args.rescore:
        records = [json.loads(line) for line in open(args.rescore)]
        llm = LLM()
    else:
        orch = Orchestrator(verify=not args.no_verify, thinking=args.thinking)
        llm = orch.llm
        print(f"running {len(questions)} questions (concurrency {args.concurrency})")
        sem = asyncio.Semaphore(args.concurrency)
        t0 = time.time()
        runner = run_naive if args.naive else run_one
        records = await asyncio.gather(*(runner(orch, q, sem, today) for q in questions))
        print(f"wall time {time.time() - t0:.0f}s")

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = HERE / "results" / f"run_{stamp}{('_' + args.tag) if args.tag else ''}.jsonl"

    def save() -> None:  # answers are saved before judging so a judge failure loses nothing
        with open(out, "w") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    if not args.rescore:
        save()

    async def safe_judge(r: dict) -> dict:
        try:
            return await judge(llm, qmap[r["id"]], r["answer"] or "(no answer)", today_s)
        except Exception as e:
            return {"score": 0, "explanation": f"JUDGE FAILED: {type(e).__name__}: {e}"[:300]}

    grades = await asyncio.gather(*(safe_judge(r) for r in records))
    for r, g in zip(records, grades):
        q = qmap[r["id"]]
        r["score"] = g.get("score", 0) if not r.get("error") else 0
        r["judge"] = g.get("explanation", "")
        r["routing_ok"] = None if r["agents_used"] == ["naive"] else routing_ok(q, r["agents_used"])
    save()
    summary = summarize(records)
    json.dump(summary, open(out.with_suffix(".summary.json"), "w"), indent=2)
    print_table(summary)
    print("\nper question:")
    for r in sorted(records, key=lambda r: r["id"]):
        flag = "  [routing]" if r["routing_ok"] is False else ""
        print(f"  {r['id']:<5} score={r['score']} {r['seconds']:>5.1f}s{flag}  {r['judge'][:150]}")
    print(f"\nsaved {out}")


if __name__ == "__main__":
    asyncio.run(main())
