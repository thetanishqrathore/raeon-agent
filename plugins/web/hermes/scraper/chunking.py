"""Token-aware chunking for LLM extraction.

Splits markdown on block boundaries (blank lines / headings) and packs blocks
into chunks under a token budget, with a small overlap so meaning isn't severed
at a boundary. Mirrors ScrapeGraphAI's word-accumulation chunker but boundary-
aware and with overlap (crawl4ai's ``OverlappingWindowChunking`` idea).

Token counts use ``tiktoken`` when it happens to be importable, else a
chars/4 heuristic — accurate enough for budgeting (we never need exactness).
"""

from __future__ import annotations

import re
from typing import List, Optional

_HEADING_RE = re.compile(r"^#{1,6}\s")
_enc = None
_enc_tried = False


def estimate_tokens(text: str) -> int:
    global _enc, _enc_tried
    if not _enc_tried:
        _enc_tried = True
        try:
            import tiktoken  # noqa: WPS433 — optional, not a declared dep

            _enc = tiktoken.get_encoding("cl100k_base")
        except Exception:  # noqa: BLE001
            _enc = None
    if _enc is not None:
        try:
            return len(_enc.encode(text))
        except Exception:  # noqa: BLE001
            pass
    return max(1, len(text) // 4)


def _split_giant_block(block: str, max_tokens: int) -> List[str]:
    """Split a single oversized block by sentences, then hard words."""
    sentences = re.split(r"(?<=[.!?。！？])\s+", block)
    out: List[str] = []
    cur: List[str] = []
    cur_tok = 0
    for s in sentences:
        st = estimate_tokens(s)
        if st > max_tokens:
            # very long sentence — hard split on words
            words = s.split(" ")
            step = max(1, int(max_tokens * 4 / 1))  # ~chars budget
            buf = ""
            for w in words:
                if estimate_tokens(buf + " " + w) > max_tokens and buf:
                    out.append(buf.strip())
                    buf = w
                else:
                    buf = f"{buf} {w}" if buf else w
            if buf:
                out.append(buf.strip())
            continue
        if cur_tok + st > max_tokens and cur:
            out.append(" ".join(cur))
            cur, cur_tok = [], 0
        cur.append(s)
        cur_tok += st
    if cur:
        out.append(" ".join(cur))
    return out


def chunk_text(text: str, max_tokens: int = 1800, overlap_tokens: int = 150) -> List[str]:
    """Pack ``text`` into chunks of ≤ ``max_tokens`` with a trailing overlap."""
    text = text.strip()
    if not text:
        return []
    if estimate_tokens(text) <= max_tokens:
        return [text]

    blocks = re.split(r"\n\n+", text)
    chunks: List[str] = []
    cur: List[str] = []
    cur_tok = 0

    def flush() -> List[str]:
        nonlocal cur, cur_tok
        if not cur:
            return []
        chunks.append("\n\n".join(cur))
        # build overlap tail
        tail: List[str] = []
        tail_tok = 0
        for blk in reversed(cur):
            t = estimate_tokens(blk)
            if tail_tok + t > overlap_tokens:
                break
            tail.insert(0, blk)
            tail_tok += t
        return tail

    for block in blocks:
        bt = estimate_tokens(block)
        if bt > max_tokens:
            # giant block: flush current, emit sub-chunks
            tail = flush()
            cur, cur_tok = [], 0
            for piece in _split_giant_block(block, max_tokens):
                chunks.append(piece)
            cur = list(tail)
            cur_tok = sum(estimate_tokens(b) for b in cur)
            continue
        if cur_tok + bt > max_tokens and cur:
            tail = flush()
            cur = list(tail)
            cur_tok = sum(estimate_tokens(b) for b in cur)
        cur.append(block)
        cur_tok += bt

    if cur:
        chunks.append("\n\n".join(cur))
    return [c for c in chunks if c.strip()]
