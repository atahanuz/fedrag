"""Command-line interface.

    fedrag ask "What did the July 2026 FOMC minutes say about inflation?"
    fedrag chat                      # multi-turn session with follow-up questions
    fedrag status                    # check the GPU gateway / LLM / index
    fedrag search "query" [--type meeting_minutes]
    fedrag tables [pattern]          # list the SQL tables (or describe one: fedrag tables NAME --describe)
    fedrag sql "SELECT ..."          # run a read-only query over the tables
    fedrag ui                        # web GUI: watch the pipeline run (http://127.0.0.1:7860)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from . import config
from .trace import Event

console = Console()

AGENT_STYLE = {
    "planner": "bold magenta", "fed_research": "bold cyan", "data_analyst": "bold bright_cyan",
    "web_research": "bold green",
    "market_data": "bold yellow", "synthesizer": "bold blue", "verifier": "bold red",
    "direct": "bold white", "orchestrator": "dim",
}


def _short(v, n: int = 90) -> str:
    s = json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
    return s if len(s) <= n else s[: n - 1] + "…"


def live_printer(verbose: bool = False):
    def show(ev: Event) -> None:
        st = AGENT_STYLE.get(ev.agent, "bold")
        tag = f"[{st}]{ev.agent:>12}[/{st}] [dim]{ev.t:6.1f}s[/dim]"
        d = ev.data
        if ev.type == "plan":
            console.print(f"{tag} intent=[bold]{d['intent']}[/bold] tasks={len(d['tasks'])} — {_short(d['reasoning'], 120)}")
            for t in d["tasks"]:
                dep = f" (after {', '.join(t['depends_on'])})" if t["depends_on"] else ""
                console.print(f"{'':>21}• [{AGENT_STYLE.get(t['agent'], '')}]{t['agent']}[/] {t['id']}: "
                              f"{_short(t['instruction'], 110)}{dep}")
        elif ev.type == "agent_start" and ev.agent not in ("planner",):
            console.print(f"{tag} ▶ {_short(d.get('task', ''), 120)}")
        elif ev.type == "tool_call":
            console.print(f"{tag}   ↳ {d['tool']}({_short(d['args'], 110)})")
        elif ev.type == "tool_result" and verbose:
            console.print(f"{tag}     ← {d['chars']} chars, ids {d['ids'][:8]}")
        elif ev.type == "thought" and verbose:
            console.print(f"{tag}   💭 {_short(d['text'], 140)}")
        elif ev.type == "agent_finish":
            console.print(f"{tag} ✔ confidence={d['confidence']} evidence={len(d['evidence'])} "
                          f"steps={d['steps']} tools={d['tool_calls']}" + (f" gaps: {_short(d['gaps'], 80)}" if d['gaps'] else ""))
        elif ev.type == "verification":
            issues = "; ".join(d.get("issues", []))[:160]
            console.print(f"{tag} verdict=[bold]{d['verdict']}[/bold]" + (f" — {issues}" if issues else ""))
        elif ev.type in ("info", "error"):
            console.print(f"{tag} {d.get('message') or d.get('error')}")
    return show


def print_result(res, show_sources: bool = True) -> None:
    console.print()
    console.print(Panel(Markdown(res.answer or "_(no answer)_"), title="Answer", border_style="green"))
    if show_sources and res.sources:
        t = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
        t.add_column("ID", style="cyan", no_wrap=True)
        t.add_column("Source")
        for s in res.sources:
            t.add_row(s["id"], f"{s['source']}" + (f"\n[dim]{s['url']}[/dim]" if s.get("url") else ""))
        console.print(t)
    u = res.usage
    console.print(f"[dim]agents: {', '.join(res.agents_used)} | {res.seconds}s | LLM calls {u['llm_calls']} "
                  f"({u['prompt_tokens']:,} in / {u['completion_tokens']:,} out) | tools {u['tool_calls']}[/dim]")


async def _ask(question: str, verbose: bool, no_verify: bool, save: str | None, thinking: bool) -> None:
    from .orchestrator import Orchestrator

    orch = Orchestrator(verify=not no_verify, thinking=thinking)
    console.print(f"[bold]Q:[/bold] {question}\n")
    res = await orch.run(question, listeners=[live_printer(verbose)])
    print_result(res)
    if save:
        Path(save).write_text(json.dumps(res.to_dict(), indent=2, ensure_ascii=False))
        console.print(f"[dim]saved run to {save}[/dim]")
    await orch.gpu.aclose()


async def _chat(verbose: bool) -> None:
    from .orchestrator import Orchestrator

    orch = Orchestrator()
    history: list[dict] = []
    console.print("[bold]fedrag chat[/bold] — ask about Fed publications, markets, or anything. Ctrl-D to quit.\n")
    while True:
        try:
            q = console.input("[bold green]you>[/bold green] ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not q:
            continue
        res = await orch.run(q, history=history, listeners=[live_printer(verbose)])
        print_result(res)
        history.append({"question": q, "answer": res.answer})
    await orch.gpu.aclose()


async def _status() -> None:
    from .gpu_client import GPUClient
    from .llm import LLM
    from .retrieval.index import CorpusIndex

    console.print(f"gateway: {config.GPU_URL or '(FEDRAG_GPU_URL not set)'}")
    g = GPUClient()
    try:
        console.print(f"  health: {await g.health()}")
    except Exception as e:
        console.print(f"  [red]unreachable: {e}[/red]")
    try:
        r = await LLM().chat([{"role": "user", "content": "Reply with: ok"}], max_tokens=5)
        console.print(f"  LLM ({config.LLM_MODEL}): {r.content!r} in {r.latency:.2f}s")
    except Exception as e:
        console.print(f"  [red]LLM error: {e}[/red]")
    idx = CorpusIndex(gpu=g)
    console.print(f"index: {len(idx.docs)} docs, {len(idx.chunks)} chunks, dense="
                  f"{'yes (' + str(idx.emb.shape[1]) + 'd)' if idx.emb is not None else 'no'}, "
                  f"{len(idx.tables.catalog)} SQL tables and views")
    await g.aclose()


def _tables(pattern: str | None, describe: bool) -> None:
    from .retrieval.tables import TableStore

    store = TableStore()
    if describe and pattern:
        console.print(store.describe(pattern))
        return
    rows = [t for t in store.catalog.values()
            if not pattern or pattern.lower() in (t["table"] + " " + t["title"]).lower()]
    for t in rows:
        console.print(f"[cyan]{t['table']}[/cyan] [dim]({t['kind']}, {t['n_rows']} rows, {t['layout']})[/dim] "
                      f"{t['title'][:110]}")
    console.print(f"[dim]{len(rows)} of {len(store.catalog)} tables[/dim]")


def _sql(query: str) -> None:
    from .retrieval.tables import TableStore

    res = TableStore().query(query, max_rows=200)
    console.print(res.render() if not res.error else f"[red]ERROR: {res.error}[/red]")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="fedrag", description="Agentic RAG over Federal Reserve publications")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("ask", help="answer one question")
    a.add_argument("question", nargs="+")
    a.add_argument("-v", "--verbose", action="store_true", help="show tool results and agent thoughts")
    a.add_argument("--no-verify", action="store_true", help="skip the fact-checking step")
    a.add_argument("--save", help="write the full run (answer, sources, trace) to this JSON file")
    a.add_argument("--thinking", action="store_true", help="let specialist agents use the LLM's thinking mode")
    c = sub.add_parser("chat", help="interactive multi-turn session")
    c.add_argument("-v", "--verbose", action="store_true")
    sub.add_parser("status", help="check gateway, LLM and index")
    s = sub.add_parser("search", help="raw hybrid search (no agents)")
    s.add_argument("query")
    s.add_argument("-k", type=int, default=8)
    s.add_argument("--type", action="append")
    t = sub.add_parser("tables", help="list the SQL tables extracted from the collection")
    t.add_argument("pattern", nargs="?")
    t.add_argument("--describe", action="store_true", help="show the schema of the table named by pattern")
    q = sub.add_parser("sql", help="run a read-only SQL query over the tables")
    q.add_argument("query")
    u = sub.add_parser("ui", help="web GUI: ask questions and watch the agents work")
    u.add_argument("--host", default="127.0.0.1")
    u.add_argument("--port", type=int, default=7860)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    if args.cmd == "ask":
        asyncio.run(_ask(" ".join(args.question), args.verbose, args.no_verify, args.save, args.thinking))
    elif args.cmd == "chat":
        asyncio.run(_chat(args.verbose))
    elif args.cmd == "status":
        asyncio.run(_status())
    elif args.cmd == "tables":
        _tables(args.pattern, args.describe)
    elif args.cmd == "sql":
        _sql(args.query)
    elif args.cmd == "ui":
        from .ui.app import main as ui_main

        ui_main(["--host", args.host, "--port", str(args.port)])
    elif args.cmd == "search":
        from .retrieval import index as idx_mod

        sys.argv = ["index", "search", args.query, "-k", str(args.k)] + sum((["--type", t] for t in args.type or []), [])
        idx_mod.main()


if __name__ == "__main__":
    main()
