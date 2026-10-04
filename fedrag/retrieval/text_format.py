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


def locator(unit: str, start: int, end: int | None = None) -> str:
    """'p. 4' / 'pp. 4-5' for PDF pages, 'part 2' / 'parts 2-3' for web pages and Word files."""
    end = end or start
    if unit == "page":
        return f"p. {start}" if start == end else f"pp. {start}-{end}"
    if unit == "table":
        return f"table {start}"
    return f"part {start}" if start == end else f"parts {start}-{end}"


def citation_label(c: dict) -> str:
    if c.get("kind") == "table":
        return f"{c['title']}, data table `{c['table']}`"
    return f"{c['title']}, {locator(c.get('unit', 'page'), c['page_start'], c['page_end'])}"
