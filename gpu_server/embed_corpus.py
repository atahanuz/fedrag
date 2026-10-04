"""Embed the chunk corpus on the GPU.

Input:  JSONL with {"id": ..., "text": ...} per line (made by ``fedrag.retrieval.index prepare``).
Output: ``embeddings.npy`` (float16, L2-normalised, row i = line i) and ``ids.json``.

    python embed_corpus.py /content/embed_input.jsonl /content/index_out
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from models import EMBED_MODEL, Embedder  # noqa: E402


def main(inp: str, out_dir: str) -> None:
    rows = [json.loads(line) for line in open(inp, encoding="utf-8")]
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    emb = Embedder()
    print(f"loaded {EMBED_MODEL} (dim={emb.dim}) in {time.time() - t0:.0f}s; embedding {len(rows)} texts", flush=True)
    t1 = time.time()
    vecs = emb.embed([r["text"] for r in rows], is_query=False, batch_size=32, show_progress=True)
    print(f"embedded in {time.time() - t1:.0f}s", flush=True)
    np.save(out / "embeddings.npy", vecs.astype(np.float16))
    json.dump({"model": EMBED_MODEL, "dim": int(vecs.shape[1]), "ids": [r["id"] for r in rows]},
              open(out / "ids.json", "w"))
    print("EMBED_DONE", vecs.shape, flush=True)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
