# CI

The public CI is intentionally focused on the custom Raeon surface: research state, citation/provenance checks, ranking, lead-generation helpers, native extraction, resilient search routing, and the web dashboard build/test path.

It does not try to run every upstream Hermes test. Upstream Hermes contains broad gateway, desktop, messaging, Docker, provider, and integration suites, some of which need live services or platform-specific setup. Widen CI when this fork changes one of those areas.

## Python Job

The workflow creates `.venv`, installs the editable package plus the dependencies needed by the custom scraper, then calls the repo's canonical runner:

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

The offline demo can also be run locally after setup:

```bash
.venv/bin/python scripts/demo_research.py
```

That demo uses synthetic local HTML and a temporary ledger. It requires no network, provider keys, model calls, or user profile state.

## Web Job

The web job installs from the root npm workspace and runs:

```bash
npm ci --ignore-scripts
npm --workspace web run typecheck
npm --workspace web test
npm --workspace web run build
npm audit --omit=dev
```

The production npm audit is expected to be clean after compatible fixes. Remaining inherited development/Electron-toolchain advisories should be handled as maintenance work and should not be hidden by a forced major upgrade without testing the affected apps. Desktop packaging has separate lifecycle/setup needs and is not covered by this web-only path.

## Action Pinning

The workflow pins GitHub Actions by commit SHA instead of mutable major tags:

- `actions/checkout` v4 tag commit: `11d5960a326750d5838078e36cf38b85af677262`
- `actions/setup-python` v5 tag commit: `a26af69be951a213d495a4c3e4e4022e16d87065`
- `actions/setup-node` v4 tag commit: `49933ea5288caeca8642d1e84afbd3f7d6820020`

Refresh these pins deliberately when updating the workflow.

## Secret Scanning

For release hygiene, scan the clean exported source tree:

```bash
gitleaks dir . --redact
```

Dependency folders and generated assets can produce noisy findings. Scan the source that will be published, and review any ignored fingerprints narrowly.
