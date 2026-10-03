# Public Release Notes

This repo is a cleaned public snapshot of Raeon Agent, a custom build on top of Nous Research Hermes Agent.

## What This Snapshot Includes

- Upstream Hermes runtime, plugins, skills, CLI, web app, and tests retained under the upstream MIT license.
- Raeon-specific research and search additions:
  - `plugins/web/hermes` native scraper and extraction pipeline.
  - `plugins/web/resilient` search-router fallback and merge logic.
  - `agent/research_citations.py`, `agent/research_ranking.py`, `agent/research_report.py`, and `agent/research_state.py`.
  - `tools/think_tool.py`, `tools/rank_sources_tool.py`, and `tools/research_ledger_tool.py`.
  - `scripts/lead_gen_batch.py` helpers for source-backed lead generation.
- Public-facing setup docs for local install, CI, web chat auth, and runtime environment variables.

## What This Snapshot Does Not Claim

- It is not an official Nous Research release.
- It is an independent customization, not an upstream Hermes distribution.
- It is not a security audit.
- It does not guarantee factual correctness of generated research or lead lists.
- It does not include private infrastructure, backup material, production domains, live credentials, or deployment secrets.

## Secret Handling

Runtime credentials should live outside the repository, usually in `${HERMES_HOME:-~/.hermes}/.env`.

Before publishing a fresh export, run secret scanning against the clean source tree:

```bash
gitleaks dir . --redact
```

Development checkouts that include dependency folders can produce many detections from public fixtures or vendored files. The release check should scan the clean exported source that will be pushed.

The prepared release copy was scanned with Gitleaks and had no unreviewed findings after 614 narrow fingerprint exceptions for reviewed fixtures, public IDs, and generated/doc assets.

## Validation Scope

The focused public validation path is:

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

For the web dashboard:

```bash
npm ci --ignore-scripts
npm --workspace web run build
npm --workspace web test
```

Do not report test counts in public materials unless they come from the exact release commit being published.

## Known Public Snapshot Limits

- The offline demo uses synthetic local HTML. It proves wiring, not live search quality.
- Citation validation checks provenance membership. It is not a full entailment proof.
- The research skill includes prompt-level operating conventions. Some are guidance rather than hard runtime gates.
- The targeted release validation passed on the prepared copy. The full upstream-style wrapper suite was attempted but not completed for release feasibility; three inherited macOS keychain-mocking failures in `tests/agent/test_anthropic_adapter.py` remained outside the custom runtime changes.
- Live providers, paid evals, and outreach/enrichment services were not run as part of the public release validation.
- Production npm audit currently has no findings after compatible fixes. Development advisories remain in inherited Electron/build tooling and should be tracked separately from runtime risk.
