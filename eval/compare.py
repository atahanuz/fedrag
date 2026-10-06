"""Compare evaluation runs side by side (e.g. two LLMs, each run twice).

    python eval/compare.py qwen=eval/results/final_agentic.jsonl,eval/results/final_agentic_b.jsonl \
                           gemma=eval/results/final_gemma_a.jsonl,eval/results/final_gemma_b.jsonl

Each label groups one or more judged result files of the same system. Prints overall and per-category
correctness (share of answers scored 2), mean score, routing, citations, cost, and the questions where
the systems differ.
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from pathlib import Path

TOOL_AGENTS = {"fed_research", "data_analyst", "web_research", "market_data"}


def load(paths: str) -> list[list[dict]]:
    return [[json.loads(line) for line in open(p)] for p in paths.split(",")]


def stats(runs: list[list[dict]], ids: set[str] | None = None) -> dict:
    rs = [r for run in runs for r in run if ids is None or r["id"] in ids]
    tool_rs = [r for r in rs if set(r["agents_used"]) & TOOL_AGENTS]
    per_run = [sum(r["score"] == 2 for r in run if ids is None or r["id"] in ids) for run in runs]
    return {
        "n": len(rs) // len(runs),
        "correct": per_run,
        "correct_rate": sum(r["score"] == 2 for r in rs) / len(rs),
        "mean_score": sum(r["score"] for r in rs) / len(rs) / 2,
        "zeros": sum(r["score"] == 0 for r in rs) / len(runs),
        "routing": sum(bool(r.get("routing_ok")) for r in rs) / len(rs),
        "cited": sum(bool(r["sources"]) for r in tool_rs) / len(tool_rs) if tool_rs else None,
        "errors": sum(bool(r.get("error")) for r in rs) / len(runs),
        "median_s": statistics.median(r["seconds"] for r in rs),
        "docs_cited": statistics.mean(len({d for x in r["sources"] for d in (x.get("doc") or x["source"]).split("+")})
                                      for r in rs),
        "llm_calls": statistics.mean(r["usage"].get("llm_calls", 0) for r in rs if r.get("usage")),
        "tool_calls": statistics.mean(sum(r["usage"].get("tool_calls", {}).values()) for r in rs if r.get("usage")),
        "prompt_k": statistics.mean(r["usage"].get("prompt_tokens", 0) for r in rs if r.get("usage")) / 1000,
        "gen_k": statistics.mean(r["usage"].get("completion_tokens", 0) for r in rs if r.get("usage")) / 1000,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("systems", nargs="+", help="label=file1.jsonl[,file2.jsonl...]")
    ap.add_argument("--json", help="also write the comparison to this file")
    args = ap.parse_args()
    systems = {s.split("=", 1)[0]: load(s.split("=", 1)[1]) for s in args.systems}
    labels = list(systems)
    first = systems[labels[0]][0]
    cats: dict[str, set[str]] = defaultdict(set)
    for r in first:
        cats[r["category"]].add(r["id"])

    out = {"overall": {lab: stats(runs) for lab, runs in systems.items()},
           "by_category": {c: {lab: stats(runs, ids) for lab, runs in systems.items()} for c, ids in cats.items()}}

    w = 14
    print(f"{'':<22}" + "".join(f"{lab:>{w}}" for lab in labels))
    rows = [("fully correct", lambda s: f"{'/'.join(map(str, s['correct']))} of {s['n']}"),
            ("correct rate", lambda s: f"{s['correct_rate']:.0%}"), ("mean score", lambda s: f"{s['mean_score']:.2f}"),
            ("wrong (0) per run", lambda s: f"{s['zeros']:.1f}"), ("routing", lambda s: f"{s['routing']:.0%}"),
            ("cited", lambda s: f"{s['cited']:.0%}" if s["cited"] is not None else "-"),
            ("run errors per run", lambda s: f"{s['errors']:.1f}"), ("documents cited / q", lambda s: f"{s['docs_cited']:.1f}"), ("median seconds", lambda s: f"{s['median_s']:.0f}"),
            ("LLM calls / q", lambda s: f"{s['llm_calls']:.1f}"), ("tool calls / q", lambda s: f"{s['tool_calls']:.1f}"),
            ("prompt tokens / q", lambda s: f"{s['prompt_k']:.0f}k"), ("generated / q", lambda s: f"{s['gen_k']:.1f}k")]
    for name, fmt in rows:
        print(f"{name:<22}" + "".join(f"{fmt(out['overall'][lab]):>{w}}" for lab in labels))

    print(f"\n{'category (mean score)':<22}" + "".join(f"{lab:>{w}}" for lab in labels))
    for c in cats:
        print(f"{c + ' (' + str(len(cats[c])) + ')':<22}" +
              "".join(f"{out['by_category'][c][lab]['mean_score']:>{w}.2f}" for lab in labels))

    print("\nper question (scores per run):")
    diff = []
    for r in first:
        scores = {lab: [next(x["score"] for x in run if x["id"] == r["id"]) for run in runs]
                  for lab, runs in systems.items()}
        means = {lab: sum(v) / len(v) for lab, v in scores.items()}
        if len(set(means.values())) > 1:
            diff.append((r["id"], scores))
    for qid, scores in diff:
        print(f"  {qid:<6}" + "".join(f"{lab}={'/'.join(map(str, v)):<8}" for lab, v in scores.items()))
    print(f"{len(diff)} of {len(first)} questions differ")
    if args.json:
        Path(args.json).write_text(json.dumps({**out, "differing": diff}, indent=2))


if __name__ == "__main__":
    main()
