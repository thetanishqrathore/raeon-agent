# Gap Analysis

This file describes the current public snapshot after Raeon-specific research/search additions. It is a maintenance guide, not a claim that the agent is complete.

## Covered

- Source ranking before extraction: `agent/research_ranking.py`.
- Durable research ledger: `agent/research_state.py`.
- Citation provenance membership checks: `agent/research_citations.py`.
- Cited report helper functions: `agent/research_report.py`.
- Native web extraction pipeline: `plugins/web/hermes`.
- Resilient search fallback and merge logic: `plugins/web/resilient`.
- Offline synthetic demo: `scripts/demo_research.py`.

## Still Limited

- Entailment checking is partial. The code can detect fabricated cited URLs, but it does not fully prove that cited text supports every claim.
- Prompt conventions still carry some methodology. For example, the skill tells the model when to reflect or gather more evidence; not every instruction is a runtime gate.
- Search providers are external systems with quotas, outages, changing ranking behavior, and different result quality.
- The public repo does not include private deployment state, production observability, or live credential configuration.
- Lead-generation output is a review artifact, not an automatic outreach authority.

## Sensible Next Work

- Add a narrow citation-support evaluator for claim/source entailment if live use shows citation membership is not enough.
- Keep the offline demo small and stable so clients can run it without keys.
- Expand CI only when the public fork touches broader Hermes surfaces.
- Track development dependency advisories without forcing major upgrades that could break Electron or build tooling.
