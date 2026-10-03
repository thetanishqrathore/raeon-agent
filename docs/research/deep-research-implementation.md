# Deep-Research Implementation

This document records what exists in the public Raeon Agent snapshot. It avoids historical branch names, old approval states, and test counts that can go stale.

## Implemented Surface

| Capability | Main files | What it does |
|---|---|---|
| Reflection tool | `tools/think_tool.py` | Provides a no-op tool call for structured findings/gaps/next-step reflection. |
| Source ranking | `agent/research_ranking.py`, `tools/rank_sources_tool.py` | Ranks and deduplicates search results with RRF-style scoring and domain diversity. |
| Citation checks | `agent/research_citations.py`, `agent/verification_stop.py` | Checks whether cited URLs are present in retrieved search/extract/delegation results and can nudge revision. |
| Research ledger | `agent/research_state.py`, `tools/research_ledger_tool.py` | Stores brief, visited URLs, claims, and learnings outside the model context in SQLite. |
| Report helpers | `agent/research_report.py` | Builds cited-source sections and writer context from retrieved material when called. |
| Native extraction | `plugins/web/hermes` | Cleans HTML, generates markdown, chunks content, ranks chunks, and supports extraction helpers. |
| Search router | `plugins/web/resilient` | Handles provider fallback, result merge, and empty-result classification. |
| Lead helpers | `scripts/lead_gen_batch.py` | Provides source-backed candidate filtering utilities. |
| Offline demo | `scripts/demo_research.py` | Demonstrates local extraction, ranking, citation provenance, and ledger reopen without network or model calls. |

## Validation

Run the focused validation suite from the repository root:

```bash
scripts/run_tests.sh \
  tests/agent/test_research_ranking.py \
  tests/agent/test_research_citations.py \
  tests/agent/test_research_report.py \
  tests/agent/test_research_state.py \
  tests/tools/test_think_tool.py \
  tests/tools/test_research_ledger_tool.py \
  tests/scripts/test_lead_gen_batch.py \
  tests/plugins/web/test_hermes_scraper.py \
  tests/plugins/web/test_resilient_router.py \
  tests/hermes_cli/test_research_config.py \
  tests/hermes_cli/test_search_router_config.py \
  -q
```

Run the no-credential demo:

```bash
.venv/bin/python scripts/demo_research.py
```

## Important Limits

- Citation validation checks retrieved-source membership. It does not prove that every sentence is entailed by the cited source.
- Delegation and reflection sequencing are partly prompt/skill conventions.
- `research_report.py` is an optional helper library, not automatic enforcement for every response.
- Live search quality depends on configured providers, quotas, and the freshness of the web.
- Lead lists require human review before outreach.
