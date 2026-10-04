"""Hybrid retrieval over the Federal Reserve corpus.

``search`` = metadata filter -> BM25 + dense (Qwen3-Embedding) -> reciprocal-rank
fusion -> cross-encoder rerank (Qwen3-Reranker). It degrades gracefully: without
the GPU gateway it falls back to BM25 only.

CLI::

    python -m fedrag.retrieval.index prepare   # export chunk texts for the GPU embedding job
    python -m fedrag.retrieval.index search "query" [--type meeting_minutes] [--from 2026-01-01]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .. import config
from ..gpu_client import GPUClient, GPUUnavailable
from .bm25 import BM25
from .text_format import embedding_text, rerank_text

log = logging.getLogger(__name__)

RRF_K = 60


@dataclass
class Hit:
    chunk: dict
    score: float
    rerank: float | None = None
    bm25_rank: int | None = None
    dense_rank: int | None = None


def _read_jsonl(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f]


class CorpusIndex:
    def __init__(self, corpus_dir: Path = config.CORPUS_DIR, index_dir: Path = config.INDEX_DIR,
                 gpu: GPUClient | None = None):
        self.gpu = gpu
        self.docs: dict[str, dict] = {d["doc_id"]: d for d in _read_jsonl(corpus_dir / "docs.jsonl")}
        self.chunks: list[dict] = _read_jsonl(corpus_dir / "chunks.jsonl")
        self.chunk_idx: dict[str, int] = {c["chunk_id"]: i for i, c in enumerate(self.chunks)}
        self.by_doc: dict[str, list[int]] = defaultdict(list)
        for i, c in enumerate(self.chunks):
            self.by_doc[c["doc_id"]].append(i)
        self.pages: dict[tuple[str, int], dict] = {
            (p["doc_id"], p["page"]): p for p in _read_jsonl(corpus_dir / "pages.jsonl")
        }
        self.bm25 = BM25([embedding_text(c) for c in self.chunks])

        # arrays for vectorised filtering
        self._types = np.array([c["doc_type"] for c in self.chunks])
        self._dates = np.array([c["sort_date"] for c in self.chunks])
        self._doc_ids = np.array([c["doc_id"] for c in self.chunks])

        self.emb: np.ndarray | None = None
        emb_path, ids_path = index_dir / "embeddings.npy", index_dir / "ids.json"
        if emb_path.exists() and ids_path.exists():
            meta = json.load(open(ids_path))
            if meta["ids"] == [c["chunk_id"] for c in self.chunks]:
                self.emb = np.load(emb_path).astype(np.float32)
                self.emb_model = meta.get("model")
            else:
                log.warning("embeddings do not match chunks.jsonl; dense retrieval disabled (re-run embedding)")
        self._qcache: OrderedDict[str, np.ndarray] = OrderedDict()

    # ------------------------------------------------------------------ filters
    def mask(self, doc_types: list[str] | None = None, date_from: str | None = None,
             date_to: str | None = None, doc_ids: list[str] | None = None) -> np.ndarray:
        m = np.ones(len(self.chunks), dtype=bool)
        if doc_types:
            m &= np.isin(self._types, doc_types)
        if date_from:
            m &= self._dates >= date_from
        if date_to:
            m &= self._dates <= date_to
        if doc_ids:
            m &= np.isin(self._doc_ids, doc_ids)
        return m

    # ------------------------------------------------------------------ search
    async def _query_vec(self, query: str) -> np.ndarray | None:
        if self.emb is None or self.gpu is None:
            return None
        if query in self._qcache:
            self._qcache.move_to_end(query)
            return self._qcache[query]
        try:
            vec = (await self.gpu.embed_queries([query]))[0]
        except GPUUnavailable as e:
            log.warning("query embedding unavailable (%s); BM25 only", e)
            return None
        self._qcache[query] = vec
        if len(self._qcache) > 512:
            self._qcache.popitem(last=False)
        return vec

    async def search(self, query: str, k: int = 8, doc_types: list[str] | None = None,
                     date_from: str | None = None, date_to: str | None = None,
                     doc_ids: list[str] | None = None, candidates: int = 40, rerank: bool = True,
                     depth: int = 100) -> list[Hit]:
        m = self.mask(doc_types, date_from, date_to, doc_ids)
        if not m.any():
            return []
        allowed = np.flatnonzero(m)

        bm = self.bm25.scores(query)[allowed]
        bm_order = allowed[np.argsort(-bm)[:depth]]
        bm_order = [i for i in bm_order if bm[np.searchsorted(allowed, i)] > 0]
        fused: dict[int, float] = defaultdict(float)
        ranks_bm = {int(i): r for r, i in enumerate(bm_order)}
        for r, i in enumerate(bm_order):
            fused[int(i)] += 1.0 / (RRF_K + r)

        ranks_dense: dict[int, int] = {}
        qv = await self._query_vec(query)
        if qv is not None:
            ds = self.emb[allowed] @ qv
            d_order = allowed[np.argsort(-ds)[:depth]]
            ranks_dense = {int(i): r for r, i in enumerate(d_order)}
            for r, i in enumerate(d_order):
                fused[int(i)] += 1.0 / (RRF_K + r)

        cand = sorted(fused, key=fused.get, reverse=True)[:candidates]
        hits = [Hit(chunk=self.chunks[i], score=fused[i], bm25_rank=ranks_bm.get(i), dense_rank=ranks_dense.get(i))
                for i in cand]
        if rerank and self.gpu is not None and hits:
            try:
                scores = await self.gpu.rerank(query, [rerank_text(h.chunk) for h in hits])
                for h, s in zip(hits, scores):
                    h.rerank = s
                    h.score = s
                hits.sort(key=lambda h: h.score, reverse=True)
            except GPUUnavailable as e:
                log.warning("rerank unavailable (%s); using fused ranking", e)
        return hits[:k]

    # ------------------------------------------------------------------ navigation
    def get_chunk(self, chunk_id: str) -> dict | None:
        i = self.chunk_idx.get(chunk_id)
        return None if i is None else self.chunks[i]

    def neighbors(self, chunk_id: str, before: int = 1, after: int = 1) -> list[dict]:
        c = self.get_chunk(chunk_id)
        if c is None:
            return []
        idxs = self.by_doc[c["doc_id"]]
        pos = c["position"]
        return [self.chunks[i] for i in idxs if pos - before <= self.chunks[i]["position"] <= pos + after]

    def page(self, doc_id: str, page: int) -> dict | None:
        return self.pages.get((doc_id, page))

    def list_docs(self, doc_types: list[str] | None = None, date_from: str | None = None,
                  date_to: str | None = None, title_contains: str | None = None) -> list[dict]:
        out = []
        for d in self.docs.values():
            if doc_types and d["doc_type"] not in doc_types:
                continue
            if date_from and d["sort_date"] < date_from:
                continue
            if date_to and d["sort_date"] > date_to:
                continue
            if title_contains and title_contains.lower() not in d["title"].lower():
                continue
            out.append(d)
        return sorted(out, key=lambda d: d["sort_date"])

    def corpus_card(self) -> str:
        """Compact description of the collection for agent prompts."""
        by_type: dict[str, list[dict]] = defaultdict(list)
        for d in self.docs.values():
            by_type[d["doc_type"]].append(d)
        lines = []
        for t, ds in sorted(by_type.items()):
            ds.sort(key=lambda d: d["sort_date"])
            label = ds[0]["series"]
            if t in ("meeting_minutes", "economic_conditions_report", "monetary_policy_report",
                     "financial_stability_report", "supervisory_report"):
                dates = ", ".join(d["date"] for d in ds)
                lines.append(f"- {t} ({label}, {len(ds)} docs): {dates}")
            elif t == "working_paper":
                titles = "; ".join(d["title"] for d in ds)
                lines.append(f"- {t} ({label}, {len(ds)} docs, {ds[0]['date']} to {ds[-1]['date']}): {titles}")
            else:
                lines.append(f"- {t} ({label}, {len(ds)} docs): " + "; ".join(f"{d['title']} [{d['date']}]" for d in ds))
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _prepare() -> None:
    config.INDEX_DIR.mkdir(parents=True, exist_ok=True)
    out = config.INDEX_DIR / "embed_input.jsonl"
    n = 0
    with open(out, "w", encoding="utf-8") as f:
        for c in _read_jsonl(config.CORPUS_DIR / "chunks.jsonl"):
            f.write(json.dumps({"id": c["chunk_id"], "text": embedding_text(c)}, ensure_ascii=False) + "\n")
            n += 1
    print(f"wrote {n} texts -> {out}")


async def _search_cli(args) -> None:
    idx = CorpusIndex(gpu=GPUClient() if config.GPU_URL else None)
    hits = await idx.search(args.query, k=args.k, doc_types=args.type, date_from=args.date_from,
                            date_to=args.date_to, rerank=not args.no_rerank)
    for h in hits:
        c = h.chunk
        print(f"[{h.score:.3f}] bm25#{h.bm25_rank} dense#{h.dense_rank} {c['chunk_id']} | {c['section'][:70]} "
              f"| p{c['page_start']}\n    {c['text'][:220]!r}")
    if idx.gpu:
        await idx.gpu.aclose()


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("prepare")
    s = sub.add_parser("search")
    s.add_argument("query")
    s.add_argument("-k", type=int, default=8)
    s.add_argument("--type", action="append")
    s.add_argument("--from", dest="date_from")
    s.add_argument("--to", dest="date_to")
    s.add_argument("--no-rerank", action="store_true")
    args = ap.parse_args()
    if args.cmd == "prepare":
        _prepare()
    else:
        asyncio.run(_search_cli(args))


if __name__ == "__main__":
    main()
