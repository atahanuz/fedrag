"""How chunks are rendered for the embedder, the reranker and the LLM.

Each chunk is prefixed with its document and section ("contextual chunk
headers"), so a passage such as "Prices rose modestly" is embedded together
with "Beige Book, September 2026 / Federal Reserve Bank of Chicago > Prices".
"""

from __future__ import annotations


def chunk_header(c: dict) -> str:
    head = f"{c['title']} ({c['date']})"
    if c.get("section"):
        head += f"\nSection: {c['section']}"
    return head


def embedding_text(c: dict) -> str:
    return f"{chunk_header(c)}\n\n{c['text']}"


def rerank_text(c: dict, max_chars: int = 3000) -> str:
    return f"{chunk_header(c)}\n{c['text'][:max_chars]}"


def citation_label(c: dict) -> str:
    pages = f"p. {c['page_start']}" if c["page_start"] == c["page_end"] else f"pp. {c['page_start']}-{c['page_end']}"
    return f"{c['title']}, {pages}"
