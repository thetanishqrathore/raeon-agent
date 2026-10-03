"""BM25 query-focused relevance filtering.

Given the page's content split into blocks and a query (explicit, or auto-derived
from title/headings à la crawl4ai's ``BM25ContentFilter``), rank blocks by BM25
relevance and keep the most relevant subset — so the LLM only sees the slice of a
big page that matters, slashing tokens. Degrades gracefully (positional keep) if
``rank_bm25`` isn't available.
"""

from __future__ import annotations

import re
from typing import List, Optional, Sequence

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOP = frozenset(
    "a an the of to in on at for and or but is are was were be been being this that "
    "these those it its as by with from into over under then than so such not no can "
    "will would should could may might do does did has have had you your we our they".split()
)


def tokenize(text: str) -> List[str]:
    return [t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOP and len(t) > 1]


def auto_query(title: str = "", headings: Sequence[str] = (), description: str = "") -> str:
    """Derive a relevance query from the page's own framing when none is given."""
    parts = [title or "", description or ""]
    parts.extend(list(headings)[:6])
    q = " ".join(p for p in parts if p).strip()
    return q


def rank_blocks(
    blocks: List[str],
    query: str,
    *,
    top_k: Optional[int] = None,
    max_tokens: Optional[int] = None,
    token_counter=None,
) -> List[str]:
    """Return the BM25-most-relevant blocks for ``query``, in original order.

    Either ``top_k`` (count) or ``max_tokens`` (cumulative budget) bounds output.
    """
    if not blocks:
        return []
    q_tokens = tokenize(query)
    if not q_tokens:
        ordered_idx = list(range(len(blocks)))
    else:
        try:
            from rank_bm25 import BM25Okapi  # noqa: WPS433 — lazy/optional

            corpus = [tokenize(b) or ["∅"] for b in blocks]
            bm = BM25Okapi(corpus)
            scores = bm.get_scores(q_tokens)
            ordered_idx = sorted(range(len(blocks)), key=lambda i: scores[i], reverse=True)
        except Exception:  # noqa: BLE001 — fall back to positional
            ordered_idx = list(range(len(blocks)))

    keep: set[int] = set()
    if max_tokens is not None and token_counter is not None:
        budget = 0
        for i in ordered_idx:
            t = token_counter(blocks[i])
            if budget + t > max_tokens and keep:
                break
            keep.add(i)
            budget += t
    else:
        k = top_k if top_k is not None else len(blocks)
        keep = set(ordered_idx[:k])

    return [blocks[i] for i in range(len(blocks)) if i in keep]
