"""Small, fast BM25 over a sparse matrix (scores for all documents in one product)."""

from __future__ import annotations

import re

import numpy as np
import snowballstemmer
from scipy import sparse

_STOP = set("""
a about above after again against all also am an and any are as at be because been before being below
between both but by can could did do does doing down during each few for from further had has have
having he her here hers herself him himself his how i if in into is it its itself just me more most my
myself no nor not now of off on once only or other our ours ourselves out over own same she should so
some such than that the their theirs them themselves then there these they this those through to too
under until up very was we were what when where which while who whom why will with would you your
yours yourself yourselves s t d ll m o re ve y
""".split())

_TOKEN = re.compile(r"[a-z0-9]+(?:\.[0-9]+)?")
_stemmer = snowballstemmer.stemmer("english")
_stem_cache: dict[str, str] = {}


def _stem(tok: str) -> str:
    s = _stem_cache.get(tok)
    if s is None:
        s = _stem_cache[tok] = _stemmer.stemWord(tok)
    return s


def tokenize(text: str) -> list[str]:
    return [_stem(t) for t in _TOKEN.findall(text.lower()) if t not in _STOP]


class BM25:
    def __init__(self, texts: list[str], k1: float = 1.2, b: float = 0.75):
        docs = [tokenize(t) for t in texts]
        vocab: dict[str, int] = {}
        rows, cols, vals = [], [], []
        for i, toks in enumerate(docs):
            counts: dict[int, int] = {}
            for t in toks:
                j = vocab.setdefault(t, len(vocab))
                counts[j] = counts.get(j, 0) + 1
            rows.extend([i] * len(counts))
            cols.extend(counts.keys())
            vals.extend(counts.values())
        n = len(docs)
        tf = sparse.csr_matrix((np.array(vals, dtype=np.float32), (rows, cols)), shape=(n, len(vocab)))
        dl = np.asarray(tf.sum(axis=1)).ravel()
        avgdl = dl.mean() if n else 1.0
        df = np.bincount(tf.indices, minlength=len(vocab))
        idf = np.log(1 + (n - df + 0.5) / (df + 0.5)).astype(np.float32)
        # per-entry BM25 weight
        tf = tf.tocoo()
        denom = tf.data + k1 * (1 - b + b * dl[tf.row] / avgdl)
        w = idf[tf.col] * tf.data * (k1 + 1) / denom
        self.matrix = sparse.csc_matrix((w.astype(np.float32), (tf.row, tf.col)), shape=(n, len(vocab)))
        self.vocab = vocab

    def scores(self, query: str) -> np.ndarray:
        ids = [self.vocab[t] for t in set(tokenize(query)) if t in self.vocab]
        if not ids:
            return np.zeros(self.matrix.shape[0], dtype=np.float32)
        return np.asarray(self.matrix[:, ids].sum(axis=1)).ravel()
