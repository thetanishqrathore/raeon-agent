---
name: deep-research
description: "Use when the user wants thorough, multi-source, citation-backed research on a question or topic — not a quick lookup. Runs a human-like bounded loop: scope & write a brief → iterative web_search → READ pages with web_extract → distill → reflect on gaps → search deeper → verify each claim across sources → synthesize a structured, cited report with confidence labels. Triggers: 'research', 'deep dive', 'investigate', 'find everything about', 'compare X vs Y', 'what's the latest on', 'write a report on', 'is it true that'. Many search/extract/LLM calls per run are expected and encouraged; quality and grounding beat speed."
version: 2.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [research, deep-research, web-research, search, citations, synthesis, fact-checking, verification, report]
    category: research
    related_skills: [osint-investigation, domain-intel, arxiv]
    requires_tools: [web_search, web_extract]
---

# Deep Research — research the web like a human

Answer a non-trivial question the way a careful human researcher would: form a
plan, search, **actually read the pages**, distill what matters, notice what's
still missing, dig deeper, **verify each claim against the sources**, then write
it up with citations and honest confidence. This is a *bounded loop*, not a
single search — expect several rounds and many tool calls.

The procedure below ports proven techniques from the open deep-research
literature (named inline) onto Hermes' own tools — `web_search`, `web_extract`
(with its `query` and `schema` modes), and the headless browser as fallback.
Nothing here needs a new dependency; it's how you *use* the tools.

## When to use

- "Research / investigate / deep dive on X", "write a report on X"
- "What's the latest / current state of X" (anything time-sensitive)
- "Compare X vs Y", "what are the best / top X", "is it true that …"
- Any question where one search + one page isn't enough, the answer is
  external/evolving, or the user needs to be able to trust the result.

## When NOT to use

- A single fact gettable in one `web_search` + one `web_extract` → just do that.
- Reading one known URL → `web_extract` directly.
- Public-records / corporate / sanctions investigation → `osint-investigation`.
- Domain / DNS / infra recon → `domain-intel`. Academic papers → `arxiv`.

## The prime directive: retrieve, don't assert (ReAct)

For every non-trivial external fact, run **Thought → Action → Observation** and
assert **only** from the Observation:

- **Thought:** what do I need to know next, and why?
- **Action:** `web_search(...)` or `web_extract(...)`.
- **Observation:** what the tool actually returned.

Never state a non-trivial fact you could have grounded. Search snippets are
*leads, not evidence* — they justify a `web_extract`, not a claim. Never
fabricate a URL, quote, or date. If you cannot verify something, say so.
*(ReAct, arXiv:2210.03629.)*

## Effort tiers (pick from the request; default = detailed)

| Tier | Rounds (depth) | Queries/round (breadth) | Pages read | Verify phase | Use for |
|---|---|---|---|---|---|
| **quick** | 1 | 3 | ≤6 | light | a focused question that still needs ≥2 sources |
| **detailed** | 2–3 | 4 | 8–15 | yes | most "research / compare / latest on X" asks |
| **report** | 3+ | 4–6 | 20+ | full | "thorough report", landscape/literature reviews |

Honor an explicit budget ("be exhaustive", "spend a lot", a token target) by
scaling up. Keep three **hard stops** in mind and jump to synthesis when any is
hit: max rounds (depth), a sensible page-fetch cap, and obvious wall-clock/cost.
*(Budget triple from Firecrawl /deep-research.)*

## Phase 0 — Scope & write the brief

1. **Clarify or proceed — one decision.** If the question is underspecified in a
   way that would change the answer (missing timeframe, region, budget,
   use-case, or the definition of "best"), ask **2–3 clarifying questions** first
   (interactive); otherwise state explicit **assumptions** and proceed
   (autonomous). Don't stall a researchable question waiting on input, and don't
   guess when a wrong assumption would waste the whole run.
   *(open_deep_research clarify-with-user; dzhng generateFeedback.)*
2. Rewrite the request into a short, **first-person, frozen research brief** —
   phrase the objective as "I will determine …" — and keep it visible in your
   reasoning. It is the single source of truth for every search and the final
   write-up; do not drift from it. Include explicit **source preferences** so
   retrieval favors the right evidence from the start. *(open_deep_research
   write_brief.)*

```
BRIEF  (first person — "I will …")
  objective:          <one sentence: what I will determine>
  sub_questions:      [ ] q1  [ ] q2  [ ] q3 …   (3–7, each independently answerable)
  scope/exclusions:   <what's in / out>
  source_preferences: <prefer: primary / official / standards / peer-reviewed /
                       recent;  avoid: SEO filler, content farms, undated pages>
  success_criteria:   <what a complete answer must contain>
STATE  (update every round)
  learnings:        (claim — source URL — date — confidence)
  visited_urls:     <set, for dedup>
  open_leads:       (terms/names/urls/contradictions to chase)
```

## Phase 1 — Iterative research loop

Work one sub-question at a time. Treat each as its own mini-investigation
("isolated context") so one topic's raw pages don't pollute another's reasoning
— return only a compressed digest to the brief. *(LangChain ODR.)*

**Fan out the breadth phase.** When the brief splits into several *independent*
sub-questions (e.g. "pricing of X", "pricing of Y", "regulatory status of Z"),
delegate them in parallel with **`delegate_task`** (batch `tasks`, one per
sub-question, `toolsets: ["web"]`). Give each subagent a self-contained goal plus
the **research contract** so it returns a *compressed cited report*, not raw
pages:

> Research `<sub-question>`. Search, then `web_extract` the most authoritative
> and most recent sources and read them; cross-check load-bearing facts across
> ≥2 independent sources. RETURN a compressed digest: each key claim followed by
> the source URL you actually retrieved, then a `Sources:` list and a one-line
> confidence note. No raw page dumps, and never cite a URL you didn't retrieve.
> If sources are weak, say so.

Each subagent runs in its own isolated context (the supervisor → parallel
sub-agent → compress pattern), so you synthesize the returned reports without
ever loading their raw pages — keeping your own context clean. `delegate_task`
runs them concurrently (≤ the delegation cap, default 3) and reports per-task
status, so a failed branch is a "proceed without this" signal, not a sunk run.
If `delegation.model` is configured, subagents can run on a cheaper/faster model
than you. Reserve fan-out for genuinely independent threads — do a single lookup
yourself. *(open_deep_research supervisor; onyx isolation; deepagents task.)*

Per round:

1. **Reformulate → breadth queries.** Turn the active sub-question into
   `breadth` (default 4) *complementary* `web_search` queries — different angles,
   synonyms, and a recency cue (current year / "latest") when freshness matters;
   use operators (`site:`, `filetype:pdf`, quotes) when useful. On the **first**
   round derive queries from the brief; on **later** rounds derive each next
   query from your **most recent reasoning gap** / a branch follow-up, not by
   re-paraphrasing the original question, and re-anchor to the brief
   periodically. *(RAG-Fusion for the fan-out; IRCoT + Self-Ask for
   query-from-reasoning and sequential one-hop decomposition.)*
2. **Fan out** all `breadth` `web_search` calls **in one turn** (parallel).
3. **Merge & dedup.** Combine the ranked result lists by **Reciprocal Rank
   Fusion** — score each URL `Σ 1/(60 + rank)` across queries, sort desc — so
   URLs that surface from multiple angles rise and single-query flukes sink.
   Canonicalize URLs (strip tracking params / fragments), drop anything already
   in `visited_urls`, and skip obvious near-duplicate/syndicated reposts.
   *(RAG-Fusion / RRF.)* The **`rank_sources`** tool does exactly this
   deterministically — pass it your per-query result lists and it returns the
   RRF-fused, one-per-domain shortlist — so prefer it over eyeballing the merge.
   Select the top **N** (≈ breadth) URLs, prefer primary/official + recent.
4. **Read.** `web_extract` the selected URLs (parallel, ≤5/call). Power moves:
   - pass the sub-question as **`query`** to pull just the relevant answer from a
     long page;
   - pass a **`schema`** like `{"key_facts":[{"claim","source_url","quote","date"}],
     "followups":[...]}` to get structured, comparable facts.
   READ the returned markdown — never answer from the search snippet.
5. **Distill before inject (Search-o1).** Reduce each source to a *short, cited
   digest of only the facts bearing on this sub-question* before it enters your
   working notes. (`web_extract`'s `query` mode already does this distillation;
   for very long/dense pages do a second focused `query` pass.) Raw page dumps
   must not accumulate in the main reasoning context — that causes drift.
6. **Ledger.** Mark every extracted snippet `seen`; mark the ones you actually
   use `cited`. Append findings to `STATE.learnings` with source URL + date. For
   a **long or multi-round** investigation (where your context may be compacted),
   persist this to the durable **`research_ledger`** tool — `set_brief` once,
   then `add_learning` / `add_visited` as you go, and `get` it back if your
   context was trimmed — so the brief and visited set survive compaction instead
   of being silently dropped. *(local-deep-researcher running-summary; deepagents
   offload-to-disk.)*
7. **Reflect / gap-find — in a standalone `think` turn.** After the reads, call
   the **`think`** tool ALONE (never paired with a search or extract in the same
   turn — it must see this round's results first). In it, look over the
   *retrieved-but-uncited* pool and the brief and decide
   `{ sufficient: bool, gaps: [...], next_queries: [...] }`, and put the single
   concrete next action in `next_step`. Keep a tiny **Reflexion** note (`tried /
   dead-end / next`, last ~5) so you don't re-run dead queries or re-read dead
   domains. This forced reflection turn is what decides loop-vs-stop — don't skip
   it, and don't reflexively search again before taking it. *(think_tool from
   open_deep_research / onyx / deepagents; STORM/Co-STORM moderator gap-finder;
   mshumer/OpenDeepResearcher adaptive stop; Reflexion memory.)*
8. **Branch & decay.** If a sub-question spawns children, thread down only
   `{learnings, followups, visited_urls}` and halve breadth at each level
   (`breadth_next = ceil(breadth/2)`) so the tree fans wide then converges.
   *(dzhng/deep-research.)*

**Stop** when any holds: every sub-question is answered with ≥2 credible sources
**and** the uncited pool yields no salient new question (convergence); or a hard
budget (rounds / fetches / time) is exhausted; or `depth` is reached. The
`report` tier has a minimum-rounds floor. On stop → go to synthesis with
whatever evidence exists.

## Phase 2 — Factored verification (detailed & report tiers)

"Has a citation" ≠ "is supported by the citation." Verify in a context that does
**not** see your draft, so the check can't rubber-stamp a hallucination.
*(Chain-of-Verification, factored variant; Self-RAG ISSUP grounding gate.)*

1. **Decompose** the draft's load-bearing statements into **atomic,
   decontextualized claims** (resolve "it/the company/last year" into explicit
   entities and dates). Drop opinions / non-verifiable text.
2. For each claim, run an **isolated** `web_search` → `web_extract` that sees
   *only the claim*, using `schema`:
   `{"claim","supported":"full|partial|none","evidence_url","evidence_quote","date"}`.
3. **Triangulate.** Require ≥2 **independent domains** for each load-bearing
   fact. On conflict, surface both positions with dated citations and prefer the
   more recent / more authoritative / better-corroborated one — don't silently
   pick. Assign each claim a label: **high** (≥2 independent + consistent) /
   **medium** (1 strong source) / **uncertain** (weak/conflicting).
4. Unsupported or `partial` claims → re-retrieve, hedge the wording, or delete.
   Never ship an unsupported load-bearing claim.

## Phase 3 — Synthesize the report (single pass, never parallelized)

1. **Outline first** from the curated digests; write each section conditioned
   only on its assigned sources, keeping a sentence → source map. *(STORM.)*
2. **Cite during generation**, not after: assemble the prompt from the numbered,
   pre-vetted excerpts (each with URL + date) and emit inline `[n]` markers bound
   to those exact excerpts as you write.
3. Structure:
   - **Bottom line up front** (2–4 sentences answering the original question).
   - **Body** by sub-question/theme; every substantive claim carries `[n]` and,
     for contested points, "according to <source> (date) …".
   - **Sources** — numbered `[n] Title — URL (date)`, primary sources first.
   - **Confidence & gaps** — what's well-established (high), what's thin/
     single-sourced (medium/uncertain), what's still unknown, plus the **as-of
     date** for time-sensitive facts.
   - **Follow-up questions** (optional).
4. Do a final pass: does every cited sentence actually follow from its source?
   Strip or fix any citation that doesn't. *(Entailment/NLI gate.)*

## Tool-usage summary

- `web_search` — discovery + RRF fan-out + verification corroboration rounds.
  The search backend is selected and failed-over **automatically** (a resilient,
  quota-aware router behind the tool) — you never choose or name a search engine.
- `rank_sources` — fuse your per-query result lists by RRF and take a
  one-per-domain shortlist **before** spending `web_extract` budget.
- `web_extract` **query mode** — per-source distillation against a sub-question
  or a claim (this is your grounded Observation).
- `web_extract` **schema mode** — emit the `{key_facts,followups}` state objects
  and the `{claim,supported,evidence_url}` verification records.
- `think` — standalone reflection **between** search sets (call it ALONE):
  consolidate findings, name gaps, decide loop-vs-stop.
- `delegate_task` (`toolsets: ["web"]`) — fan out INDEPENDENT sub-questions to
  isolated sub-researchers that return compressed cited reports (the research
  contract); you synthesize the reports without seeing their raw pages.
- `research_ledger` — for long / multi-round runs, persist the brief + learnings
  + visited URLs (survives context compaction); `get` it back if context was
  trimmed so you never re-research or lose the brief.
- **browser tools** — fallback only when `web_extract` reports `needs_browser`
  (JS-only / bot-wall / paywall): `browser_navigate` → `browser_snapshot`.

## Anti-patterns (don't)

- ❌ Assert an external fact from a snippet without `web_extract`-ing the page.
- ❌ Stop after one round while sub-questions remain open.
- ❌ Cite a page you didn't read, or invent a URL/quote/date.
- ❌ Let raw scraped pages pile up in context instead of distilling per source.
- ❌ Present one source's claim as settled, or skip the verification phase on a
  `detailed`/`report` task.
- ❌ Ignore publication dates on a time-sensitive topic.

## Provenance

Techniques adapted (as procedure, not vendored) from: LangChain
open_deep_research, dzhng/deep-research, Stanford STORM, mshumer/OpenDeepResearcher,
Firecrawl /deep-research, and the methods ReAct, IRCoT, Self-Ask, Search-o1,
RAG-Fusion, Chain-of-Verification, Self-RAG, and Reflexion. See
`RESEARCH_LANDSCAPE.md` in this folder for the full verified survey + roadmap.
