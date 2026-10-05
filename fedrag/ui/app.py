"""Gradio web UI: chat with the agents and watch them work.

    python -m fedrag.ui.app            # http://127.0.0.1:7860

Each agent appears as a collapsible panel listing its tool calls while it runs;
the final answer links every citation to its source (PDF page, web page, data API).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re

import gradio as gr

from ..orchestrator import Orchestrator, RunResult
from ..trace import Event

ICONS = {"planner": "🧭", "fed_research": "🏛️", "data_analyst": "🧮", "web_research": "🌐", "market_data": "📈",
         "synthesizer": "✍️", "verifier": "🔍", "direct": "💬", "orchestrator": "⚙️"}
LABELS = {"planner": "Planner", "fed_research": "Fed document research", "data_analyst": "Fed data analyst (SQL)",
          "web_research": "Web research",
          "market_data": "Market & economic data", "synthesizer": "Writer", "verifier": "Fact-checker",
          "direct": "Direct answer", "orchestrator": "Orchestrator"}

EXAMPLES = [
    "What did the FOMC decide at its July 2026 meeting, and who dissented?",
    "Which banks had a projected minimum CET1 ratio below 10 percent in the 2026 stress test?",
    "How did the median 2026 fed funds projection change across the SEPs since September 2025?",
    "What was total household debt in 2026:Q2 according to the New York Fed, and how much is credit card debt?",
    "What task forces did Chairman Warsh describe in his July 2026 testimony?",
    "How did the Beige Book's description of labor markets change between January and September 2026?",
    "Which banks had the lowest projected CET1 ratios in the 2026 stress test?",
    "Did the Fed raise rates in September 2026? How does today's fed funds range compare with July?",
    "What is the euro worth in dollars today, and how has it moved since July 1, 2026?",
    "What are the main vulnerabilities in the May 2026 Financial Stability Report?",
    "Explain the difference between IORB and the ON RRP rate.",
]


def _fmt_args(args: dict) -> str:
    s = json.dumps(args, ensure_ascii=False)
    return s if len(s) < 160 else s[:157] + "…"


def link_citations(answer: str, sources: list[dict]) -> str:
    urls = {s["id"]: s.get("url") for s in sources}
    return re.sub(r"\[([DWM]\d+)\]", lambda m: f"[[{m.group(1)}]]({urls[m.group(1)]})" if urls.get(m.group(1))
                  else m.group(0), answer)


def sources_markdown(res: RunResult) -> str:
    if not res.sources:
        return "_No sources: answered from general knowledge._"
    kinds = {"doc": "Federal Reserve documents and data tables", "web": "Web", "data": "Market & economic data"}
    out = []
    for kind, label in kinds.items():
        items = [s for s in res.sources if s["kind"] == kind]
        if not items:
            continue
        out.append(f"**{label}**")
        for s in items:
            ref = f"[{s['source']}]({s['url']})" if s.get("url") else s["source"]
            out.append(f"- **{s['id']}** {ref}")
    return "\n".join(out)


def stats_markdown(res: RunResult) -> str:
    u = res.usage
    tools = ", ".join(f"{k}×{v}" for k, v in u["tool_calls"].items()) or "none"
    verdicts = " → ".join(v["verdict"] for v in res.verifications) or "skipped"
    return (f"**Intent:** {res.plan.get('intent')}  \n**Agents:** {', '.join(res.agents_used)}  \n"
            f"**Tools:** {tools}  \n**Fact-check:** {verdicts}  \n"
            f"**Time:** {res.seconds}s · **LLM calls:** {u['llm_calls']} "
            f"({u['prompt_tokens']:,} in / {u['completion_tokens']:,} out tokens)")


class TraceView:
    """Turns trace events into Gradio chat messages (one collapsible panel per agent run)."""

    def __init__(self) -> None:
        self.msgs: list[gr.ChatMessage] = []
        self.open: dict[str, gr.ChatMessage] = {}
        self.calls: dict[int, list[list[str]]] = {}  # panel id -> [[tool, call line, result ids], ...]
        self.drafts: dict[str, str] = {}  # streamed writer text per panel key
        self.n = 0

    def _render_calls(self, m: gr.ChatMessage, tail: str = "") -> None:
        lines = [f"- `{tool}` {args}" + (f" → {ids}" if ids else "") for tool, args, ids in self.calls.get(m.metadata["id"], [])]
        m.content = "\n".join(lines) + tail

    def _panel(self, key: str, agent: str, title: str) -> gr.ChatMessage:
        self.n += 1
        m = gr.ChatMessage(role="assistant", content="", metadata={"title": f"{ICONS.get(agent, '•')} {title}",
                                                                   "status": "pending", "id": self.n})
        self.msgs.append(m)
        self.open[key] = m
        self.drafts.pop(key, None)
        return m

    def add(self, ev: Event) -> None:
        d, a = ev.data, ev.agent
        key = f"{a}:{d.get('task_id', '')}"
        if ev.type == "agent_start":
            if a == "planner":
                self._panel("planner:", a, "Planning which agents to use")
            elif a in ("fed_research", "web_research", "market_data"):
                self._panel(key, a, f"{LABELS[a]} · {d.get('task_id')}: {d.get('task', '')[:90]}")
            elif a == "verifier":
                self._panel("verifier:", a, "Fact-checking the draft")
            elif a == "synthesizer":
                self._panel("synthesizer:", a, d.get("task", "Writing the answer").capitalize())
            elif a == "direct":
                self._panel("direct:", a, "Answering from general knowledge")
        elif ev.type == "plan":
            m = self.open.get("planner:")
            if m:
                lines = [f"**Intent:** {d['intent']} — {d['reasoning']}"]
                for t in d["tasks"]:
                    lines.append(f"- {ICONS.get(t['agent'], '')} **{t['agent']}** ({t['id']}): {t['instruction']}")
                if not d["tasks"]:
                    lines.append("- No tools needed.")
                m.content = "\n".join(lines)
                m.metadata["status"] = "done"
                m.metadata["duration"] = ev.t
        elif ev.type == "tool_call":
            m = self.open.get(key) or self._find(a)
            if m is not None:
                self.calls.setdefault(m.metadata["id"], []).append([d["tool"], _fmt_args(d["args"]), ""])
                self._render_calls(m)
        elif ev.type == "tool_result":
            m = self.open.get(key) or self._find(a)
            if m is not None:
                # results of parallel calls arrive in call order: fill the first open slot for this tool
                for entry in self.calls.get(m.metadata["id"], []):
                    if entry[0] == d["tool"] and not entry[2]:
                        entry[2] = ", ".join(d.get("ids", [])[:8]) or "(no new evidence)"
                        break
                self._render_calls(m)
        elif ev.type == "agent_finish":
            m = self.open.get(key)
            if m is not None:
                self._render_calls(m, f"\n\n**Findings** (confidence {d['confidence']}): {d['answer']}"
                                      + (f"\n\n_Gaps: {d['gaps']}_" if d.get("gaps") else ""))
                m.metadata["status"] = "done"
        elif ev.type == "verification":
            m = self.open.get("verifier:")
            if m is not None:
                issues = "\n".join(f"- {i}" for i in d.get("issues", [])) or "- none"
                m.content = f"**Verdict: {d['verdict']}**\n{issues}"
                m.metadata["status"] = "done"
        elif ev.type == "delta":
            k = "synthesizer:" if a == "synthesizer" else "direct:"
            m = self.open.get(k)
            if m is not None:
                self.drafts[k] = self.drafts.get(k, "") + d["text"]
                m.content = self.drafts[k]
        elif ev.type == "synthesis":
            m = self.open.get("synthesizer:")
            if m is not None:
                m.content = self.drafts.get("synthesizer:") or ("Revised draft ready." if d.get("revised")
                                                               else "Draft ready.")
                m.metadata["status"] = "done"
        elif ev.type in ("info", "error"):
            self.n += 1
            self.msgs.append(gr.ChatMessage(role="assistant", content=d.get("message") or d.get("error", ""),
                                            metadata={"title": "⚙️ Orchestrator", "id": self.n}))

    def _find(self, agent: str) -> gr.ChatMessage | None:
        for k in reversed(list(self.open)):
            if k.startswith(agent + ":") and self.open[k].metadata.get("status") == "pending":
                return self.open[k]
        return None

    def close_all(self) -> None:
        for m in self.msgs:
            if m.metadata.get("status") == "pending":
                m.metadata["status"] = "done"


def build_app(orch: Orchestrator) -> gr.Blocks:
    async def respond(message: str, chat: list, turns: list, verify: bool):
        message = (message or "").strip()
        if not message:
            yield chat, turns, gr.update(), gr.update(), ""
            return
        chat = list(chat or []) + [gr.ChatMessage(role="user", content=message)]
        view = TraceView()
        queue: asyncio.Queue[Event] = asyncio.Queue()
        task = asyncio.create_task(orch.run(message, history=turns, listeners=[queue.put_nowait], verify=verify))
        yield chat, turns, gr.update(), gr.update(), ""
        while not task.done() or not queue.empty():
            try:
                ev = await asyncio.wait_for(queue.get(), timeout=0.5)
                view.add(ev)
                while not queue.empty():
                    view.add(queue.get_nowait())
                yield chat + view.msgs, turns, gr.update(), gr.update(), ""
            except asyncio.TimeoutError:
                continue
        view.close_all()
        try:
            res: RunResult = task.result()
        except Exception as e:  # surface failures in the chat instead of crashing the UI
            err = gr.ChatMessage(role="assistant", content=f"⚠️ The run failed: {type(e).__name__}: {e}")
            yield chat + view.msgs + [err], turns, gr.update(), gr.update(), ""
            return
        answer = gr.ChatMessage(role="assistant", content=link_citations(res.answer, res.sources))
        turns = list(turns or []) + [{"question": message, "answer": res.answer}]
        yield chat + view.msgs + [answer], turns, sources_markdown(res), stats_markdown(res), ""

    with gr.Blocks(title="Fed Agentic RAG", fill_height=True) as app:
        gr.Markdown("## 🏛️ Federal Reserve Agentic RAG\nAsk about Fed publications (FOMC minutes, Beige Books, "
                    "stress tests, reports 2022–2026), live markets and economic data, or anything else. "
                    "A planner routes your question to specialist agents; their work is shown step by step.")
        turns = gr.State([])
        with gr.Row(equal_height=False):
            with gr.Column(scale=3):
                chat = gr.Chatbot(height=640, buttons=["copy"], render_markdown=True, label="Conversation")
                with gr.Row():
                    box = gr.Textbox(placeholder="Ask a question…", show_label=False, scale=8, autofocus=True)
                    send = gr.Button("Ask", variant="primary", scale=1)
                gr.Examples(EXAMPLES, inputs=box, label="Try")
            with gr.Column(scale=2):
                verify = gr.Checkbox(value=True, label="Fact-check answers (verifier agent)")
                stats = gr.Markdown("_Run statistics appear here._")
                gr.Markdown("### Sources")
                sources = gr.Markdown("_Sources of the last answer appear here._")
                clear = gr.Button("New conversation")

        outs = [chat, turns, sources, stats, box]
        box.submit(respond, [box, chat, turns, verify], outs)
        send.click(respond, [box, chat, turns, verify], outs)
        clear.click(lambda: ([], [], "_Sources of the last answer appear here._", "_Run statistics appear here._"),
                    None, [chat, turns, sources, stats])
    return app


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=7860)
    args = ap.parse_args()
    app = build_app(Orchestrator())
    app.queue(default_concurrency_limit=4).launch(server_name=args.host, server_port=args.port)


if __name__ == "__main__":
    main()
