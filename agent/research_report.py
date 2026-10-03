#!/usr/bin/env python3
"""Cited-docs-only report assembly (P1.6).

The final writer should see ONLY the documents it actually cites, distilled and
fit to a token budget, and the report's citations should be densely and
deterministically numbered — never renumbered by the model. These are the
patterns from open_deep_research / onyx / gpt-researcher / STORM (writer sees
cited docs only; ``collapse_citations`` global renumber; progressive-truncation
retry on overflow).

This module is pure assembly + deterministic transforms — it does not call an
LLM. The agent/model still writes the prose; this guarantees the writer's INPUT
contains only cited sources within budget, and post-processes the draft into a
clean, consistently-numbered cited report. Citation parsing/identity reuses
:mod:`agent.research_citations`, so it stays consistent with the validator.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from agent.research_citations import (
    canonicalize_url,
    extract_urls,
    renumber_citations,
)


def estimate_tokens(text: str) -> int:
    """Cheap token estimate (~4 chars/token). Override via a real counter."""
    if not text:
        return 0
    return max(1, len(text) // 4)


def select_cited_docs(
    docs: Dict[str, str],
    draft_or_claims: str,
) -> Dict[str, str]:
    """Keep only the docs whose URL is cited in the draft/claims text.

    ``docs`` maps URL -> content. Matching is by canonical URL, so a doc keyed
    ``https://a.com/p`` is kept when the draft cites ``https://a.com/p/?utm=x``.
    The writer never sees uncited sources — that's the whole point.
    """
    cited = {canonicalize_url(u) for u in extract_urls(draft_or_claims)}
    cited.discard("")
    out: Dict[str, str] = {}
    for url, content in (docs or {}).items():
        if canonicalize_url(url) in cited:
            out[url] = content
    return out


def fit_docs_to_budget(
    docs: Dict[str, str],
    token_budget: int,
    counter: Callable[[str], int] = estimate_tokens,
) -> Tuple[Dict[str, str], bool]:
    """Progressively truncate doc bodies (largest first) to fit ``token_budget``.

    Returns ``(fitted_docs, truncated)``. Every doc is kept (so no cited source
    silently disappears) but long bodies are trimmed and marked with a
    ``… [truncated]`` suffix. Mirrors the progressive-truncation retry the report
    writers use on token overflow.
    """
    if token_budget <= 0 or not docs:
        return dict(docs), False
    fitted = dict(docs)
    if sum(counter(c) for c in fitted.values()) <= token_budget:
        return fitted, False

    # Counter-agnostic: repeatedly trim the CURRENT largest doc by a fixed
    # fraction and re-measure with the caller's counter, so any token estimator
    # works (we don't assume a chars-per-token ratio). A char floor keeps every
    # cited source's head intact; stripping the marker before re-trimming avoids
    # compounding "… [truncated]" suffixes across passes.
    mark = "\n… [truncated]"
    char_floor = 200
    truncated = False
    guard = 0
    while sum(counter(c) for c in fitted.values()) > token_budget and guard < 10000:
        guard += 1
        url = max(fitted, key=lambda u: counter(fitted[u]))
        body = fitted[url]
        if body.endswith(mark):
            body = body[: -len(mark)]
        if len(body) <= char_floor:
            break  # largest doc already at the floor → can't shrink further
        new_len = max(char_floor, int(len(body) * 0.75))
        if new_len >= len(body):
            new_len = len(body) - char_floor
        fitted[url] = body[:new_len].rstrip() + mark
        truncated = True
    return fitted, truncated


def assemble_writer_context(
    brief: str,
    docs: Dict[str, str],
    draft_or_claims: str,
    token_budget: int = 12000,
    counter: Callable[[str], int] = estimate_tokens,
) -> Dict[str, Any]:
    """Build the final-writer input: brief + ONLY cited docs, fit to budget.

    Returns a dict with the assembled ``context`` string, the ``cited_urls``
    kept, and whether any doc was ``truncated``.
    """
    cited_docs = select_cited_docs(docs, draft_or_claims)
    fitted, truncated = fit_docs_to_budget(cited_docs, token_budget, counter)
    parts = [f"RESEARCH BRIEF:\n{brief.strip()}", "", "CITED SOURCES (use only these):"]
    for i, (url, content) in enumerate(fitted.items(), start=1):
        parts.append(f"\n[{i}] {url}\n{content}")
    return {
        "context": "\n".join(parts),
        "cited_urls": list(fitted.keys()),
        "truncated": truncated,
    }


def build_sources_section(ordered_urls: Sequence[str]) -> str:
    """Render a numbered ``## Sources`` block from URLs in citation order."""
    if not ordered_urls:
        return ""
    lines = ["## Sources"]
    for i, url in enumerate(ordered_urls, start=1):
        lines.append(f"[{i}] {url}")
    return "\n".join(lines)


def finalize_report(
    draft_text: str,
    *,
    append_sources: bool = True,
) -> Tuple[str, List[Tuple[int, str]]]:
    """Deterministically finalize a draft's citations.

    Collects inline-cited URLs in order of appearance, assigns dense 1..k ids,
    optionally appends a ``## Sources`` section, and — when the draft uses
    numeric markers like ``[3][7]`` — collapses them to ``[1][2]`` consistent
    with that ordering (the renumber is code, never the model). Returns
    ``(report_text, [(id, url)])``.

    The draft body itself is not rewritten (we never mangle the model's prose);
    only numeric markers are densified and a Sources list is appended.
    """
    if not draft_text:
        return draft_text, []

    # Cited URLs, canonicalized, in appearance order.
    ordered: List[str] = []
    seen = set()
    for raw in extract_urls(draft_text):
        c = canonicalize_url(raw)
        if c and c not in seen:
            seen.add(c)
            ordered.append(raw)  # keep the original (display) URL

    # Densify any numeric markers against the same ordering.
    text, _src = renumber_citations(draft_text, ordered)

    sources = [(i + 1, u) for i, u in enumerate(ordered)]
    if append_sources and ordered:
        text = text.rstrip() + "\n\n" + build_sources_section(ordered)
    return text, sources
