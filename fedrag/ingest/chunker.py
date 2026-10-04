"""Section assignment and chunking.

Every paragraph gets a *section path* (e.g. ``Federal Reserve Bank of Chicago >
Labor Markets``) built from the PDF outline when one exists, refined by headings
detected from the font. FOMC minutes have no outline, so their standard headings
are recognised directly. Paragraphs are then packed into overlapping chunks that
never cross a top-level section boundary, so a Beige Book chunk always belongs
to exactly one District.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .pdf_parser import ParsedPDF

MAX_WORDS = 320  # ~430 tokens
MIN_WORDS = 80
OVERLAP_WORDS = 45

SKIP_SECTIONS = {"contents", "table of contents"}
MAX_OUTLINE_DEPTH = 2  # deeper outline levels are handled like font-detected sub-headings
_CONTINUED = re.compile(r"(—|--|-|–)\s*continued\b|\(continued\)", re.I)


@dataclass
class Para:
    page: int
    text: str
    kind: str
    path: list[str] = field(default_factory=list)


@dataclass
class Chunk:
    position: int
    section: str
    page_start: int
    page_end: int
    text: str

    @property
    def n_words(self) -> int:
        return len(self.text.split())


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", s.lower())


# ---------------------------------------------------------------------------
# Section assignment
# ---------------------------------------------------------------------------


def assign_sections(parsed: ParsedPDF) -> list[Para]:
    paras = [Para(page=pg.number, text=p.text, kind=p.kind) for pg in parsed.pages for p in pg.paragraphs]
    if not paras:
        return paras

    toc = [(lvl, title, page) for lvl, title, page in parsed.toc if title.strip()]
    # Position (paragraph index) where each TOC entry starts.
    starts: dict[int, list[tuple[int, str]]] = {}
    cursor = 0
    for lvl, title, page in toc:
        key = _norm(title)[:60]
        found = None
        for i in range(cursor, len(paras)):
            p = paras[i]
            if p.page > page + 1:
                break
            if p.page < page - 1 or len(p.text) > 250:
                continue
            pk = _norm(p.text)
            if key and (pk.startswith(key) or (len(pk) >= 12 and key.startswith(pk[:60]))):
                found = i
                break
        if found is None:  # heading not found in the text layer: start at the top of its page
            found = next((i for i in range(cursor, len(paras)) if paras[i].page >= page), len(paras) - 1)
        starts.setdefault(found, []).append((lvl, title))
        cursor = found

    has_toc = bool(toc)
    path: list[str] = []
    sub: str | None = None  # font-detected (or deep outline) heading below the outline path
    for i, p in enumerate(paras):
        if i in starts:
            for lvl, title in starts[i]:
                if lvl <= MAX_OUTLINE_DEPTH:
                    path = path[: lvl - 1] + [title]
                    sub = None
                else:
                    # deep outline entries (e.g. MPR "Box 3. ...") end without a marker, so
                    # treat them like font headings: replaced by the next heading
                    sub = title
            if p.kind == "heading" or _norm(p.text).startswith(_norm(starts[i][-1][1])[:40]):
                p.kind = "heading"
        elif p.kind == "heading" and not _CONTINUED.search(p.text):
            if has_toc:
                sub = p.text
            else:
                path = [p.text]
        p.path = path + ([sub] if sub and sub not in path else [])
    return paras


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\"“])")


def _split_long(text: str, kind: str, max_words: int) -> list[str]:
    """Split one oversized paragraph into pieces of at most ``max_words``."""
    if len(text.split()) <= max_words:
        return [text]
    units = text.split("\n") if kind == "table" else _SENT_SPLIT.split(text)
    pieces, cur, n = [], [], 0
    for u in units:
        w = len(u.split())
        if cur and n + w > max_words:
            pieces.append(("\n" if kind == "table" else " ").join(cur))
            cur, n = [], 0
        if w > max_words:  # pathological run-on text: hard split by words
            words = u.split()
            for j in range(0, len(words), max_words):
                pieces.append(" ".join(words[j: j + max_words]))
            continue
        cur.append(u)
        n += w
    if cur:
        pieces.append(("\n" if kind == "table" else " ").join(cur))
    return pieces


def _tail_overlap(text: str, n_words: int) -> str:
    """Last few whole sentences of ``text`` totalling at most ``n_words``."""
    if "\n" in text.strip().split("\n\n")[-1]:  # ends in a table: no prose overlap
        return ""
    sents = _SENT_SPLIT.split(text.split("\n\n")[-1])
    out: list[str] = []
    for s in reversed(sents):
        if len(" ".join(out + [s]).split()) > n_words:
            break
        out.insert(0, s)
    return " ".join(out)


def chunk_document(paras: list[Para], max_words: int = MAX_WORDS, min_words: int = MIN_WORDS,
                   overlap_words: int = OVERLAP_WORDS) -> list[Chunk]:
    # Drop table-of-contents pages and expand oversized paragraphs.
    items: list[Para] = []
    last_caption = ""
    for p in paras:
        if p.path and p.path[0].strip().lower() in SKIP_SECTIONS:
            continue
        if re.match(r"^(table|figure)\s+[A-Z]?\d", p.text, re.I):
            last_caption = p.text.split("\n")[0][:200]
        pieces = _split_long(p.text, p.kind, max_words)
        for j, piece in enumerate(pieces):
            if j > 0 and p.kind == "table" and last_caption:
                piece = f"{last_caption} (continued)\n{piece}"
            items.append(Para(page=p.page, text=piece, kind=p.kind, path=p.path))

    chunks: list[Chunk] = []
    cur: list[Para] = []

    def section_of(ps: list[Para]) -> str:
        # most specific path shared by the chunk's paragraphs (use the first non-heading's path)
        body = [p for p in ps if p.kind != "heading"] or ps
        return " > ".join(body[0].path)

    def emit(ps: list[Para], overlap: str = ""):
        text = "\n\n".join(p.text for p in ps)
        if overlap:
            text = overlap + "\n\n" + text
        chunks.append(Chunk(position=len(chunks), section=section_of(ps), page_start=ps[0].page,
                            page_end=ps[-1].page, text=text.strip()))

    overlap = ""
    for p in items:
        top_changed = cur and (p.path[:1] != cur[-1].path[:1])
        words_cur = sum(len(x.text.split()) for x in cur) + len(overlap.split())
        if cur and (top_changed or words_cur + len(p.text.split()) > max_words):
            # never leave a heading dangling at the end of a chunk
            carry = []
            while cur and cur[-1].kind == "heading":
                carry.insert(0, cur.pop())
            if cur:
                emit(cur, overlap)
                overlap = "" if top_changed else _tail_overlap(chunks[-1].text, overlap_words)
            else:
                overlap = ""
            if top_changed:
                overlap = ""
            cur = carry
        cur.append(p)
    if cur:
        emit(cur, overlap)

    # Merge tiny chunks into their predecessor when they share the top-level section.
    merged: list[Chunk] = []
    for c in chunks:
        if (merged and c.n_words < min_words and c.section.split(" > ")[0] == merged[-1].section.split(" > ")[0]
                and merged[-1].n_words + c.n_words <= max_words * 1.3):
            prev = merged[-1]
            prev.text = prev.text + "\n\n" + c.text
            prev.page_end = c.page_end
        else:
            merged.append(c)
    # Drop chunks with no substance (cover pages, heading-only remnants).
    merged = [c for c in merged if c.n_words >= 12 and not _only_headings(c.text, paras)]
    for i, c in enumerate(merged):
        c.position = i
    return merged


def _only_headings(text: str, paras: list[Para]) -> bool:
    headings = {p.text for p in paras if p.kind == "heading"}
    return all(block.strip() in headings for block in text.split("\n\n") if block.strip())
