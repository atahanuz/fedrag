"""Embedding and reranking models served next to the LLM on the GPU box.

Both are loaded with plain transformers / sentence-transformers so that the
corpus embeddings (``embed_corpus.py``) and the query embeddings served by
``gateway.py`` come from exactly the same code path.
"""

from __future__ import annotations

import os
import threading

import torch

EMBED_MODEL = os.environ.get("FEDRAG_EMBED_MODEL", "Qwen/Qwen3-Embedding-8B")
RERANK_MODEL = os.environ.get("FEDRAG_RERANK_MODEL", "Qwen/Qwen3-Reranker-4B")

# Qwen3-Embedding expects an instruction on the query side only.
DEFAULT_QUERY_INSTRUCTION = (
    "Given a question about the U.S. economy, monetary policy, banking supervision or financial "
    "stability, retrieve passages from Federal Reserve publications that answer it"
)
DEFAULT_RERANK_INSTRUCTION = (
    "Given a question, judge whether this passage from a Federal Reserve publication contains "
    "information that helps answer it"
)


def _dtype_kwargs() -> dict:
    # transformers>=5 renamed torch_dtype -> dtype
    import transformers

    major = int(transformers.__version__.split(".")[0])
    return {"dtype": torch.bfloat16} if major >= 5 else {"torch_dtype": torch.bfloat16}


class Embedder:
    def __init__(self, model_name: str = EMBED_MODEL, max_seq_length: int = 1024):
        from sentence_transformers import SentenceTransformer

        self.model = SentenceTransformer(
            model_name,
            device="cuda",
            model_kwargs={**_dtype_kwargs(), "attn_implementation": "sdpa"},
            tokenizer_kwargs={"padding_side": "left"},
        )
        self.model.max_seq_length = max_seq_length
        self.dim = self.model.get_sentence_embedding_dimension()
        self.lock = threading.Lock()

    @torch.inference_mode()
    def embed(self, texts: list[str], is_query: bool = False, instruction: str | None = None,
              batch_size: int = 32, show_progress: bool = False):
        prompt = f"Instruct: {instruction or DEFAULT_QUERY_INSTRUCTION}\nQuery:" if is_query else None
        with self.lock:
            return self.model.encode(
                texts,
                prompt=prompt,
                batch_size=batch_size,
                normalize_embeddings=True,
                convert_to_numpy=True,
                show_progress_bar=show_progress,
            )


class Reranker:
    """Qwen3-Reranker: relevance = P("yes") at the last position."""

    PREFIX = (
        "<|im_start|>system\nJudge whether the Document meets the requirements based on the Query and "
        "the Instruct provided. Note that the answer can only be \"yes\" or \"no\".<|im_end|>\n<|im_start|>user\n"
    )
    SUFFIX = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"

    def __init__(self, model_name: str = RERANK_MODEL, max_length: int = 2048):
        from transformers import AutoModelForCausalLM, AutoTokenizer

        self.tok = AutoTokenizer.from_pretrained(model_name, padding_side="left")
        self.model = (
            AutoModelForCausalLM.from_pretrained(model_name, **_dtype_kwargs(), attn_implementation="sdpa")
            .cuda()
            .eval()
        )
        self.max_length = max_length
        self.prefix_ids = self.tok.encode(self.PREFIX, add_special_tokens=False)
        self.suffix_ids = self.tok.encode(self.SUFFIX, add_special_tokens=False)
        self.yes_id = self.tok.convert_tokens_to_ids("yes")
        self.no_id = self.tok.convert_tokens_to_ids("no")
        self.lock = threading.Lock()

    @torch.inference_mode()
    def score(self, query: str, documents: list[str], instruction: str | None = None,
              batch_size: int = 16) -> list[float]:
        instruction = instruction or DEFAULT_RERANK_INSTRUCTION
        pairs = [f"<Instruct>: {instruction}\n<Query>: {query}\n<Document>: {d}" for d in documents]
        budget = self.max_length - len(self.prefix_ids) - len(self.suffix_ids)
        scores: list[float] = []
        with self.lock:
            # sort by length to minimise padding, restore order afterwards
            order = sorted(range(len(pairs)), key=lambda i: len(pairs[i]))
            out = [0.0] * len(pairs)
            for start in range(0, len(order), batch_size):
                idx = order[start: start + batch_size]
                enc = self.tok([pairs[i] for i in idx], padding=False, truncation="longest_first",
                               return_attention_mask=False, max_length=budget)
                ids = [self.prefix_ids + e + self.suffix_ids for e in enc["input_ids"]]
                batch = self.tok.pad({"input_ids": ids}, padding=True, return_tensors="pt").to("cuda")
                logits = self.model(**batch).logits[:, -1, :]
                two = torch.stack([logits[:, self.no_id], logits[:, self.yes_id]], dim=1).float()
                probs = torch.nn.functional.softmax(two, dim=1)[:, 1].tolist()
                for i, p in zip(idx, probs):
                    out[i] = p
            scores = out
        return scores
