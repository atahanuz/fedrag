"""Shared registry of everything the agents have looked at.

Every passage a tool returns (document chunk, document page, web page, search
snippet, API data) is registered once and gets a short stable ID: ``D`` for
Federal Reserve documents, ``W`` for the web, ``M`` for market/API data. Agents
cite these IDs, the synthesizer writes ``[D3]``-style citations, and the final
answer's source list is rendered from the cited IDs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .retrieval.text_format import citation_label


@dataclass
class Evidence:
    id: str
    kind: str  # "doc" | "web" | "data"
    title: str
    text: str
    source: str  # human-readable reference (document + pages, URL, API)
    url: str | None = None
    meta: dict = field(default_factory=dict)

    def render(self, max_chars: int = 2500) -> str:
        if self.meta.get("unit") == "page":  # a full page (often a table) the agent chose to read
            max_chars *= 2
        body = self.text if len(self.text) <= max_chars else self.text[:max_chars] + " ..."
        return f"[{self.id}] {self.source}\n{body}"


class EvidenceStore:
    PREFIX = {"doc": "D", "web": "W", "data": "M"}

    def __init__(self) -> None:
        self.items: dict[str, Evidence] = {}
        self._keys: dict[str, str] = {}
        self._counters = {"D": 0, "W": 0, "M": 0}

    def _add(self, key: str, kind: str, **kw) -> Evidence:
        if key in self._keys:
            ev = self.items[self._keys[key]]
            # keep the longest version of the text (e.g. snippet upgraded to the full page)
            if len(kw.get("text", "")) > len(ev.text):
                ev.text = kw["text"]
            return ev
        p = self.PREFIX[kind]
        self._counters[p] += 1
        ev = Evidence(id=f"{p}{self._counters[p]}", kind=kind, **kw)
        self.items[ev.id] = ev
        self._keys[key] = ev.id
        return ev

    def add_chunk(self, c: dict) -> Evidence:
        return self._add(
            f"chunk:{c['chunk_id']}", "doc", title=c["title"], text=c["text"], source=citation_label(c),
            meta={k: c[k] for k in ("chunk_id", "doc_id", "doc_type", "date", "section", "page_start", "page_end")},
        )

    def add_page(self, doc: dict, page: int, text: str, section: str = "") -> Evidence:
        return self._add(
            f"page:{doc['doc_id']}:{page}", "doc", title=doc["title"], text=text,
            source=f"{doc['title']}, p. {page}", url=doc.get("url"),
            meta={"doc_id": doc["doc_id"], "doc_type": doc["doc_type"], "date": doc["date"], "section": section,
                  "page_start": page, "page_end": page, "unit": "page"},
        )

    def add_web(self, url: str, title: str, text: str, published: str | None = None, kind: str = "page") -> Evidence:
        src = f"{title} ({url})" + (f", {published}" if published else "")
        return self._add(f"web:{url}", "web", title=title, text=text, source=src, url=url,
                         meta={"published": published, "type": kind})

    def add_data(self, key: str, title: str, text: str, source: str, url: str | None = None,
                 meta: dict | None = None) -> Evidence:
        return self._add(f"data:{key}", "data", title=title, text=text, source=source, url=url, meta=meta or {})

    def get(self, eid: str) -> Evidence | None:
        return self.items.get(eid)

    def render(self, ids: list[str], max_chars: int = 2500) -> str:
        return "\n\n".join(self.items[i].render(max_chars) for i in ids if i in self.items)


_CITE = re.compile(r"\[((?:[DWM]\d+)(?:\s*[,;]\s*[DWM]\d+)*)\]")


def cited_ids(text: str) -> list[str]:
    """IDs cited as [D3] / [D3, W1] in order of first appearance."""
    out: list[str] = []
    for m in _CITE.finditer(text):
        for eid in re.split(r"\s*[,;]\s*", m.group(1)):
            if eid not in out:
                out.append(eid)
    return out
