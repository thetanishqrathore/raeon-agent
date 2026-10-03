# Research Upgrade Plan

The major research/search pieces in this public snapshot are already implemented as additive modules around Hermes. Future work should stay narrow and measurable.

## Current Priority

1. Keep the offline demo reliable and credential-free.
2. Keep focused CI green for the custom research/search/extraction surface.
3. Review generated research manually before using it for client work or outreach.
4. Refresh action pins and dependency advisories deliberately.

## Future Candidates

| Candidate | Why | Caution |
|---|---|---|
| Citation-support evaluator | Better check whether a claim is supported by a cited source. | Avoid blocking valid answers on brittle heuristics. |
| More lead enrichment adapters | Useful for verified contacts. | Requires provider terms, credentials, rate limits, and data-quality review. |
| Research eval cases | Tracks quality regressions over time. | Do not publish benchmark claims without reproducible release data. |
| Deployment recipe | Helps reproduce the web dashboard on a VPS. | Keep secrets and hostnames out of the repo. |

## Guardrails

- Preserve the upstream MIT license and notice.
- Prefer small tools, plugins, and helper modules over changes to the core model loop.
- Do not add public claims about affiliations, private infrastructure, or unverified benchmark performance.
- Keep credentials in `${HERMES_HOME:-~/.hermes}/.env` or the deployment secret store, never in Git.
