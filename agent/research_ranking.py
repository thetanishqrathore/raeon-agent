#!/usr/bin/env python3
"""Source ranking + domain-diversity selection for research (P1.2).

When the breadth phase fans out several complementary searches, the same strong
sources surface from multiple angles and weak SEO restatements cluster on a few
domains. Spending scarce ``web_extract`` budget on near-duplicates is wasteful.
This module does, deterministically and in code, what the deep-research skill
otherwise asks the model to do by hand:

* **Reciprocal Rank Fusion (RRF)** across the per-query result lists — a URL that
  ranks well from several angles rises; single-query flukes sink. Score for a
  URL is ``Σ 1/(k + rank)`` over the lists it appears in (k=60, the standard
  constant). *(RAG-Fusion / RRF; open_deep_research dedup.)*
* **One-source-per-domain diversity floor** before extraction, with a safe
  backfill so we never under-select when good sources legitimately cluster on a
  few domains. *(btahir source scoring; gpt-researcher SourceCurator.)*

Pure + deterministic (no model calls, no network). URL identity reuses the
canonicalizer from :mod:`agent.research_citations` so dedup is consistent with
the citation validator.
"""

from __future__ import annotations

from typing import Any, Dict, List, Sequence
from urllib.parse import urlsplit

from agent.research_citations import canonicalize_url


_RRF_K = 60  # standard RRF damping constant


def domain_of(url: str) -> str:
    """Host-level domain (netloc minus ``www.`` and port). Best effort.

    Host-level (not registrable eTLD+1) keeps this dependency-free and
    predictable; ``docs.example.com`` and ``blog.example.com`` count as distinct
    domains, which is acceptable for a diversity floor.
    """
    if not url:
        return ""
    try:
        net = urlsplit(url).netloc.lower()
    except Exception:
        return url.lower()
    if net.startswith("www."):
        net = net[4:]
    return net.split(":")[0]


def _item_url(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("url") or item.get("sourceURL") or item.get("link") or "")
    return str(item or "")


def reciprocal_rank_fusion(
    result_lists: Sequence[Sequence[Any]],
    k: int = _RRF_K,
) -> List[Dict[str, Any]]:
    """Fuse ranked result lists into one deduped list, best-first.

    Each input list is one query's ranked hits (dicts with a ``url`` — title /
    description preserved — or bare URL strings). Returns dict items annotated
    with ``canonical_url`` and ``rrf_score``, sorted by score descending.
    """
    scores: Dict[str, float] = {}
    rep: Dict[str, Dict[str, Any]] = {}
    first_order: Dict[str, int] = {}
    seq = 0
    for results in result_lists or []:
        for rank, item in enumerate(results or [], start=1):
            url = _item_url(item)
            canon = canonicalize_url(url)
            if not canon:
                continue
            scores[canon] = scores.get(canon, 0.0) + 1.0 / (k + rank)
            if canon not in rep:
                base = dict(item) if isinstance(item, dict) else {}
                base["url"] = url
                base["canonical_url"] = canon
                rep[canon] = base
                first_order[canon] = seq
                seq += 1
    ranked = sorted(
        rep.values(),
        # Higher score first; ties broken by first-seen order for determinism.
        key=lambda it: (-scores[it["canonical_url"]], first_order[it["canonical_url"]]),
    )
    for it in ranked:
        it["rrf_score"] = round(scores[it["canonical_url"]], 6)
    return ranked


def select_diverse(
    ranked: Sequence[Dict[str, Any]],
    max_results: int,
    per_domain: int = 1,
) -> List[Dict[str, Any]]:
    """Take the top ``max_results`` enforcing ``per_domain`` per host.

    If the per-domain cap leaves us short of ``max_results`` (sources cluster on
    a few domains), backfill with the next-best held-back items so we never
    under-select. Order is preserved best-first.
    """
    if max_results <= 0:
        return []
    selected: List[Dict[str, Any]] = []
    overflow: List[Dict[str, Any]] = []
    counts: Dict[str, int] = {}
    for it in ranked:
        d = domain_of(it.get("url", ""))
        if counts.get(d, 0) < per_domain:
            selected.append(it)
            counts[d] = counts.get(d, 0) + 1
        else:
            overflow.append(it)
        if len(selected) >= max_results:
            return selected[:max_results]
    for it in overflow:  # backfill, best-first, only if still short
        if len(selected) >= max_results:
            break
        selected.append(it)
    return selected[:max_results]


def rank_and_select(
    result_lists: Sequence[Sequence[Any]],
    max_results: int = 8,
    per_domain: int = 1,
    k: int = _RRF_K,
) -> List[Dict[str, Any]]:
    """RRF-fuse the lists, then apply the domain-diversity floor. One call."""
    return select_diverse(
        reciprocal_rank_fusion(result_lists, k=k), max_results, per_domain
    )
