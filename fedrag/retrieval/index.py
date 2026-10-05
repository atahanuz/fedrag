"""Hybrid retrieval over the Federal Reserve corpus, plus SQL access to its tables.

``search`` = metadata filter -> BM25 + dense (Qwen3-Embedding) -> reciprocal-rank
fusion -> cross-encoder rerank (Qwen3-Reranker). It degrades gracefully: without
the GPU gateway it falls back to BM25 only. Chunks are prose passages (``kind`` text)
or table cards (``kind`` table) that describe one SQL table in ``self.tables``.

CLI::

    python -m fedrag.retrieval.index prepare   # texts that still need embeddings -> embed_input.jsonl
    python -m fedrag.retrieval.index merge     # combine new and reused embeddings in chunk order
    python -m fedrag.retrieval.index search "query" [--type meeting_minutes] [--from 2026-01-01]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .. import config
from ..gpu_client import GPUClient, GPUUnavailable
from .bm25 import BM25
from .tables import TableStore
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
        self.tables = TableStore(corpus_dir)

        # arrays for vectorised filtering
        self._types = np.array([c["doc_type"] for c in self.chunks])
        self._dates = np.array([c["sort_date"] for c in self.chunks])
        self._doc_ids = np.array([c["doc_id"] for c in self.chunks])
        self._kinds = np.array([c.get("kind", "text") for c in self.chunks])
        self._formats = np.array([self.docs[c["doc_id"]].get("format", "pdf") for c in self.chunks])

        self.emb: np.ndarray | None = None
        emb_path, ids_path = index_dir / "embeddings.npy", index_dir / "ids.json"
        if emb_path.exists() and ids_path.exists():
            meta = json.load(open(ids_path))
            if meta["ids"] == [c["chunk_id"] for c in self.chunks]:
                self.emb = np.load(emb_path).astype(np.float32)
                self.emb_model = meta.get("model")
                if meta.get("hashes"):
                    stale = sum(h != _text_hash(embedding_text(c)) for h, c in zip(meta["hashes"], self.chunks))
                    if stale:
                        log.warning("%d chunks changed since they were embedded; run "
                                    "`python -m fedrag.retrieval.index embed-missing`", stale)
            else:
                log.warning("embeddings do not match chunks.jsonl; dense retrieval disabled (re-run embedding)")
        self._qcache: OrderedDict[str, np.ndarray] = OrderedDict()

    # ------------------------------------------------------------------ filters
    def mask(self, doc_types: list[str] | None = None, date_from: str | None = None,
             date_to: str | None = None, doc_ids: list[str] | None = None, kinds: list[str] | None = None,
             formats: list[str] | None = None) -> np.ndarray:
        m = np.ones(len(self.chunks), dtype=bool)
        if kinds:
            m &= np.isin(self._kinds, kinds)
        if formats:
            m &= np.isin(self._formats, formats)
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
                     depth: int = 100, kinds: list[str] | None = None, formats: list[str] | None = None) -> list[Hit]:
        m = self.mask(doc_types, date_from, date_to, doc_ids, kinds, formats)
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
                  date_to: str | None = None, title_contains: str | None = None, speaker: str | None = None,
                  formats: list[str] | None = None) -> list[dict]:
        out = []
        for d in self.docs.values():
            if doc_types and d["doc_type"] not in doc_types:
                continue
            if formats and d.get("format", "pdf") not in formats:
                continue
            if speaker and speaker.lower() not in (d.get("speaker") or d["title"]).lower():
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
        """Compact description of the collection for agent prompts: what exists, in which format, when."""
        by_type: dict[str, list[dict]] = defaultdict(list)
        for d in self.docs.values():
            by_type[d["doc_type"]].append(d)
        dated = ("meeting_minutes", "economic_conditions_report", "monetary_policy_report", "financial_stability_report",
                 "supervisory_report", "economic_projections", "press_conference", "loan_officer_survey")
        lines = []
        for t, ds in sorted(by_type.items(), key=lambda kv: -len(kv[1])):
            ds.sort(key=lambda d: d["sort_date"])
            fmts = "/".join(sorted({d.get("format", "pdf") for d in ds}))
            head = f"- {t} ({ds[0]['series']}, {len(ds)} {fmts})"
            if t in dated:
                lines.append(f"{head}: " + ", ".join(d["date"] for d in ds))
            elif t == "fomc_statement":
                stm = [d["date"] for d in ds if "longer_run" not in d["doc_id"]]
                lr = [d["date"] for d in ds if "longer_run" in d["doc_id"]]
                lines.append(f"{head}: every meeting {stm[0]} to {stm[-1]}; Statement on Longer-Run Goals: "
                             + ", ".join(lr))
            elif t in ("speech", "testimony"):
                who: dict[str, int] = defaultdict(int)
                for d in ds:
                    who[re.sub(r"^(Vice Chair for Supervision|Vice Chair|Chairman|Chair|Governor)\s+|,.*$", "",
                               d.get("speaker") or "?")] += 1
                lines.append(f"{head}, {ds[0]['date']} to {ds[-1]['date']}, by "
                             + ", ".join(f"{k} {v}" for k, v in sorted(who.items(), key=lambda kv: -kv[1])))
            elif t in ("feds_note", "press_release", "working_paper", "supervisory_letter"):
                lines.append(f"{head}, {ds[0]['date']} to {ds[-1]['date']}; find them by topic with find_documents")
            else:
                reports = [d for d in ds if d.get("format") in ("pdf", "docx", "html")]
                data = [d for d in ds if d.get("format") in ("xlsx", "xls", "csv")]
                parts = [f"{re.sub(r' [(][^)]*[)]$', '', d['title'])[:90]} [{d['date']}]" for d in reports]
                if data:
                    parts.append(f"+ {len(data)} data files ({', '.join(sorted({d['format'] for d in data}))}) "
                                 "for the data_analyst")
                lines.append(f"{head}: " + "; ".join(parts))
        return "\n".join(lines)

    def dataset_card(self) -> str:
        """The main datasets (data files and stacked views) for the data agent and the planner."""
        if not self.tables.available:
            return "(no tables)"
        groups: dict[str, list[tuple[dict, list[str]]]] = defaultdict(list)
        for doc_id, names in self.tables.by_doc.items():
            d = self.docs[doc_id]
            if d.get("format") in ("xlsx", "xls", "csv"):  # files of one series that differ only by year
                groups[re.sub(r"\b(19|20)\d{2}\b", "YYYY", d["title"])].append((d, names))
        lines = []
        for key, members in sorted(groups.items()):
            members.sort(key=lambda m: m[0]["sort_date"])
            d, names = members[-1]
            if len(members) > 1:
                years = ", ".join(re.search(r"\b(19|20)\d{2}\b", m[0]["title"]).group(0) for m in members
                                  if re.search(r"\b(19|20)\d{2}\b", m[0]["title"]))
                pattern = re.sub(r"(19|20)\d{2}", "YYYY", names[0])
                lines.append(f"- {key} [YYYY = {years}]: `{pattern}`")
                continue
            ex = ", ".join(f"`{n}`" for n in names[:2]) + (f" ... ({len(names)} tables)" if len(names) > 2 else "")
            lines.append(f"- {d['title']} [{d['date']}]: {ex}")
        views = [t for t in self.tables.catalog.values() if t["kind"] == "view"]
        if views:
            lines.append("- views stacking a recurring table across releases (columns doc_id, doc_date): "
                         + ", ".join(f"`{v['table']}`" for v in views))
        html_docs = {t["doc_id"] for t in self.tables.catalog.values() if t["kind"] == "table"
                     and self.docs[t["doc_id"]].get("format") in ("html", "docx")}
        lines.append(f"- tables from {len(html_docs)} web pages and Word files: every Summary of Economic "
                     "Projections (`sep_YYYY_MM_DD__table_1` projections, `sep_YYYY_MM_DD__figure_2` dot plot, ...), "
                     "FEDS Notes, press releases")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _text_hash(text: str) -> str:
    import hashlib

    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _known_vectors() -> dict[str, np.ndarray]:
    """Embeddings already computed, keyed by the hash of the text they embed (reused across rebuilds)."""
    emb_path, ids_path = config.INDEX_DIR / "embeddings.npy", config.INDEX_DIR / "ids.json"
    if not (emb_path.exists() and ids_path.exists()):
        return {}
    meta = json.load(open(ids_path))
    hashes = meta.get("hashes")
    if hashes is None:
        return {}
    emb = np.load(emb_path)
    return {h: emb[k] for k, h in enumerate(hashes) if k < len(emb)}


def _upgrade_ids() -> None:
    """Indexes written before text hashes were stored: add them from the texts the index was built from."""
    ids_path, texts = config.INDEX_DIR / "ids.json", config.INDEX_DIR / "embed_input.jsonl"
    if not ids_path.exists() or not texts.exists():
        return
    meta = json.load(open(ids_path))
    if "hashes" in meta:
        return
    by_id = {r["id"]: r["text"] for r in _read_jsonl(texts)}
    if set(by_id) != set(meta["ids"]):
        log.warning("embed_input.jsonl does not match ids.json; existing embeddings will not be reused")
        return
    meta["hashes"] = [_text_hash(by_id[i]) for i in meta["ids"]]
    json.dump(meta, open(ids_path, "w"))


def _prepare() -> None:
    """Write the chunk texts that have no embedding yet to embed_input.jsonl (all texts to embed_input_all)."""
    config.INDEX_DIR.mkdir(parents=True, exist_ok=True)
    _upgrade_ids()
    known = _known_vectors()
    rows = [{"id": c["chunk_id"], "text": embedding_text(c)} for c in _read_jsonl(config.CORPUS_DIR / "chunks.jsonl")]
    with open(config.INDEX_DIR / "embed_input_all.jsonl", "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    todo = [r for r in rows if _text_hash(r["text"]) not in known]
    with open(config.INDEX_DIR / "embed_input.jsonl", "w", encoding="utf-8") as f:
        for r in todo:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"{len(rows)} chunks: {len(rows) - len(todo)} reuse existing embeddings, {len(todo)} to embed "
          f"-> {config.INDEX_DIR / 'embed_input.jsonl'}")


def _merge(new_dir: Path | None = None) -> None:
    """Combine reused and newly computed embeddings (new_dir/embeddings.npy + ids.json) in chunk order."""
    known = _known_vectors()
    new_dir = new_dir or config.INDEX_DIR / "new"
    if (new_dir / "ids.json").exists():
        meta = json.load(open(new_dir / "ids.json"))
        new = np.load(new_dir / "embeddings.npy")
        texts = {r["id"]: r["text"] for r in _read_jsonl(config.INDEX_DIR / "embed_input.jsonl")}
        for i, vec in zip(meta["ids"], new):
            known[_text_hash(texts[i])] = vec
        model = meta.get("model")
    else:
        model = json.load(open(config.INDEX_DIR / "ids.json")).get("model") if (config.INDEX_DIR / "ids.json").exists() else None
    rows = _read_jsonl(config.INDEX_DIR / "embed_input_all.jsonl")
    hashes = [_text_hash(r["text"]) for r in rows]
    missing = [r["id"] for r, h in zip(rows, hashes) if h not in known]
    if missing:
        raise SystemExit(f"{len(missing)} chunks still have no embedding (e.g. {missing[:3]}); run the embedding job")
    emb = np.stack([known[h] for h in hashes]).astype(np.float16)
    np.save(config.INDEX_DIR / "embeddings.npy", emb)
    json.dump({"model": model, "dim": int(emb.shape[1]), "ids": [r["id"] for r in rows], "hashes": hashes},
              open(config.INDEX_DIR / "ids.json", "w"))
    print(f"merged {len(rows)} embeddings ({emb.shape[1]}-d) -> {config.INDEX_DIR}")


async def _embed_remote(batch: int = 16) -> None:
    """Embed the chunks listed in embed_input.jsonl through the running GPU gateway (small updates: no batch job)."""
    rows = _read_jsonl(config.INDEX_DIR / "embed_input.jsonl")
    if rows:
        gpu = GPUClient(timeout=300)
        vecs = []
        for k in range(0, len(rows), batch):
            vecs.append(await gpu.embed_documents([r["text"] for r in rows[k: k + batch]]))
            print(f"  embedded {min(k + batch, len(rows))}/{len(rows)}", flush=True)
        await gpu.aclose()
        new = config.INDEX_DIR / "new"
        new.mkdir(parents=True, exist_ok=True)
        np.save(new / "embeddings.npy", np.concatenate(vecs).astype(np.float16))
        meta = json.load(open(config.INDEX_DIR / "ids.json"))
        json.dump({"model": meta.get("model"), "ids": [r["id"] for r in rows]}, open(new / "ids.json", "w"))
    _merge(config.INDEX_DIR / "new")


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
    sub.add_parser("embed-missing", help="prepare, embed the missing chunks via the GPU gateway, merge")
    mg = sub.add_parser("merge")
    mg.add_argument("--new", type=Path, help="directory with the new embeddings.npy and ids.json")
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
    elif args.cmd == "merge":
        _merge(args.new)
    elif args.cmd == "embed-missing":
        _prepare()
        asyncio.run(_embed_remote())
    else:
        asyncio.run(_search_cli(args))


if __name__ == "__main__":
    main()
