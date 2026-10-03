#!/usr/bin/env python3
"""rank_sources tool (P1.2) — deterministic RRF + domain-diversity selection.

A thin wrapper over :mod:`agent.research_ranking`. The model fans out several
complementary ``web_search`` calls, hands their result lists to this tool, and
gets back one deduped, diversity-balanced shortlist to read with ``web_extract``
— so it doesn't burn extract budget on near-duplicates or a single domain's SEO
restatements. The fusion math is identical to what the deep-research skill
describes in prose; this just makes it exact and effortless.
"""

import json
from typing import Any, List


def rank_sources(result_sets: Any, max_results: int = 8, per_domain: int = 1) -> str:
    """Merge per-query search result lists into a ranked, diverse shortlist."""
    if not isinstance(result_sets, list) or not result_sets:
        return tool_error(
            "Provide `result_sets`: a list of per-query result lists (each a "
            "list of search hits with at least a `url`)."
        )
    # Tolerate a single flat list of hits (treat as one query's results).
    if not isinstance(result_sets[0], list):
        result_sets = [result_sets]

    try:
        mr = int(max_results) if max_results else 8
    except (TypeError, ValueError):
        mr = 8
    try:
        pd = int(per_domain) if per_domain else 1
    except (TypeError, ValueError):
        pd = 1
    mr = max(1, mr)
    pd = max(1, pd)

    try:
        from agent.research_ranking import rank_and_select, domain_of

        selected = rank_and_select(result_sets, max_results=mr, per_domain=pd)
    except Exception as exc:  # noqa: BLE001
        return tool_error(f"ranking failed: {exc}")

    out: List[dict] = [
        {
            "url": it.get("url", ""),
            "title": it.get("title", ""),
            "domain": domain_of(it.get("url", "")),
            "rrf_score": it.get("rrf_score"),
        }
        for it in selected
    ]
    input_count = sum(len(r) for r in result_sets if isinstance(r, list))
    return json.dumps(
        {"selected": out, "selected_count": len(out), "input_count": input_count},
        ensure_ascii=False,
        indent=2,
    )


def check_rank_sources_requirements() -> bool:
    """No external requirements -- always available."""
    return True


RANK_SOURCES_SCHEMA = {
    "name": "rank_sources",
    "description": (
        "Deterministically merge several web_search result lists into ONE "
        "deduped, diversity-balanced shortlist before spending web_extract "
        "budget. Pass `result_sets` = the result lists from your complementary "
        "searches (one list per query). Returns the top URLs by Reciprocal Rank "
        "Fusion (a URL ranking well across multiple queries rises; single-query "
        "flukes sink) with at most `per_domain` per site, so duplicates and SEO "
        "restatements collapse and you read a diverse set. Use it between fanning "
        "out searches and reading pages; skip it for a single search."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "result_sets": {
                "type": "array",
                "items": {"type": "array", "items": {"type": "object"}},
                "description": (
                    "List of per-query result lists. Each hit should have at "
                    "least a `url` (title/description are preserved if present). "
                    "A single flat list is also accepted."
                ),
            },
            "max_results": {
                "type": "integer",
                "description": "How many URLs to select (default 8).",
            },
            "per_domain": {
                "type": "integer",
                "description": "Max sources per domain in the shortlist (default 1).",
            },
        },
        "required": ["result_sets"],
    },
}


# --- Registry ---
from tools.registry import registry, tool_error

registry.register(
    name="rank_sources",
    toolset="web",
    schema=RANK_SOURCES_SCHEMA,
    handler=lambda args, **kw: rank_sources(
        result_sets=args.get("result_sets"),
        max_results=args.get("max_results", 8),
        per_domain=args.get("per_domain", 1),
    ),
    check_fn=check_rank_sources_requirements,
    emoji="🗂️",
)
