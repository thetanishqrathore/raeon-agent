# Research Architecture

Raeon Agent keeps Hermes' existing single agent loop and adds a small set of research-specific tools and libraries around it. The diagram below is conceptual and skill-driven: the model still chooses tools through normal Hermes tool calling.

```mermaid
flowchart TD
    U["User question or lead-gen brief"] --> H["Hermes CLI / web chat"]
    H --> L["Hermes conversation loop"]

    L --> S["deep-research skill<br/>prompt-level procedure"]
    S --> WS["web_search"]
    WS --> R["resilient provider router<br/>fallback, merge, empty-result handling"]
    R --> RS["rank_sources<br/>RRF + domain diversity"]
    RS --> WE["web_extract"]
    WE --> P["plugins/web/hermes scraper<br/>fetch, clean, markdown, chunk, BM25"]

    L --> T["think tool<br/>structured reflection"]
    L --> Q["research_ledger<br/>brief, visited URLs, claims"]
    L --> C["citation validator<br/>cited URL in retrieved set"]
    C --> W["research_report helpers<br/>optional cited-doc assembly"]
    F["Founder Feed<br/>candidate digest"] --> L
    W --> O["reviewed research / lead notes"]
```

## Implemented Code

- `plugins/web/hermes`: native scraper and extraction pipeline.
- `plugins/web/resilient`: search-provider fallback and result merge logic.
- `agent/research_ranking.py`: source ranking and domain diversity helpers.
- `agent/research_citations.py`: citation extraction, URL canonicalization, retrieved-set checks, and citation nudge helpers.
- `agent/research_state.py`: SQLite-backed research ledger.
- `agent/research_report.py`: cited-source section and report-context helpers.
- `tools/think_tool.py`, `tools/rank_sources_tool.py`, `tools/research_ledger_tool.py`: model-callable research tools.
- `scripts/demo_research.py`: offline synthetic demo of extraction, ranking, citation provenance, and ledger reopen.

## Prompt Conventions

The deep-research skill instructs the model to scope the request, search, rank, extract, reflect, track sources, and cite retrieved evidence. Those conventions improve behavior but are not the same as a hard runtime proof. The hard checks in this snapshot cover selected mechanics, such as source ranking, ledger persistence, and whether cited URLs were retrieved. `research_report.py` is a helper library; it is not a guarantee that every final answer used the helper.
