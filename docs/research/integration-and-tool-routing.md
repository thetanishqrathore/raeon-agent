# Integration And Tool Routing

Raeon Agent extends Hermes by making search and research tools available to the existing model loop. The model still chooses tools through normal Hermes tool-calling, guided by tool descriptions and the deep-research skill.

## Tool Selection

| Situation | Expected route |
|---|---|
| Stable local/code question | Answer from existing context or local files. |
| One current external fact | `web_search` then `web_extract`, with citations. |
| Multi-source research or comparison | Deep-research skill: search, rank, extract, reflect, ledger, cite, synthesize. |
| Too many similar results | `rank_sources` before extraction. |
| Long-running research | `research_ledger` to preserve brief, URLs, claims, and learnings. |
| Research round checkpoint | `think` can be used to state findings, gaps, and next action. |

## Provider Routing

The resilient search provider chooses among configured search backends. The model asks for `web_search`; backend fallback and merge behavior happen below the model-facing tool surface.

Common environment variables are documented in [../../deploy/agent.env.example](../../deploy/agent.env.example). Search quality depends on the providers and keys configured by the operator.

## Model Routing

Hermes supports provider/model configuration for different subtasks. Raeon Agent can use a cheaper model for extraction or delegation and a stronger model for synthesis, but that is configuration, not a hard requirement. The offline demo does not call any model.

## Verification

Use the focused tests and offline demo in [../ci.md](../ci.md). Do not publish live quality percentages unless they come from a reproducible run on the exact release commit.
