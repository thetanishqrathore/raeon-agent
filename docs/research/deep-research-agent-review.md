# Deep-Research Agent Review

This note summarizes the design background used for Raeon Agent's research layer. It is not a benchmark claim and does not imply affiliation with any project named here.

## Patterns Reviewed

Open-source deep-research agents commonly use a few repeatable ideas:

- A planner or brief-writing step before search.
- Parallel or delegated sub-researchers for independent topics.
- Search-result ranking before spending extraction budget.
- Per-source compression before long synthesis.
- Citation IDs, retrieved-source ledgers, and validation passes.
- A final writer that sees cited evidence rather than all raw browsing context.
- Evaluation harnesses with synthetic cases or LLM-as-judge scoring.

Raeon Agent adopts the lightest useful version of those ideas inside Hermes' existing loop. The fork uses tools and helper modules rather than a separate framework.

## What Was Borrowed As Design Inspiration

- Forced reflection between research rounds, implemented as `tools/think_tool.py`.
- Context hygiene through a research ledger, implemented in `agent/research_state.py` and `tools/research_ledger_tool.py`.
- Source ranking and domain diversity before extraction, implemented in `agent/research_ranking.py`.
- Citation membership checks, implemented in `agent/research_citations.py`.
- Cited report helper functions, implemented in `agent/research_report.py`.

## What Was Not Adopted

- No LangGraph/LangChain migration.
- No special-token tool protocol.
- No copied code from unlicensed repositories.
- No live leaderboard or benchmark claim in this public snapshot.
- No human-approval UI for research claims; research outputs remain drafts for human review.

## License Notes

External repos were used as research references. Any implementation in this repository should be reviewed against source licenses before copying code. This public snapshot is distributed under the upstream Hermes MIT license with additional notice in [../../NOTICE.md](../../NOTICE.md).
