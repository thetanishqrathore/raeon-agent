# Deep-research landscape — verified findings & roadmap

_Auto-generated from a multi-agent web research sweep (29 agents). The roadmap drives this skill's design._

## Roadmap

# Hermes Deep-Research Roadmap

## 1. Ranked capabilities to add (top 12)

Ranking favors high-impact/low-effort items that reuse Hermes's existing tools (web_search, query/schema web_extract, native fit-markdown scraper, skills, config-gated research prompt). Most are prompt/procedure work, not new infra.

**#1 — General `deep-research` orchestration loop (the master skill)**
- *Why human-like:* A real researcher plans, searches, reads, notices gaps, digs again, cross-checks, then writes with citations. Hermes has all primitives but NO general orchestration skill that sequences them with budgets and stopping criteria.
- *Artifact:* Rewrite/extend the existing `deep-research` SKILL.md into a full bounded loop (designed in §2).
- *Borrow:* loop spine from **LangChain open_deep_research** (brief→supervisor→single-writer) + **dzhng/deep-research** (breadth×depth) + **STORM** (gap-finder) + **Firecrawl /deep-research** (triple budget).
- *Impact: high · Effort: medium*

**#2 — Retrieve-don't-assert invariant (ReAct discipline in the system prompt)**
- *Why:* The cheapest, highest-leverage anti-hallucination rule: never state a non-trivial external fact you could ground; emit Thought→(web_search/web_extract) Action→Observation, then assert only from the Observation.
- *Artifact:* Tighten the config-gated research-quality system-prompt block + a Thought/Action/Observation trace template in SKILL.md.
- *Borrow:* **ReAct** (arXiv:2210.03629).
- *Impact: high · Effort: low*

**#3 — Reason-conditioned per-source distillation (compress-before-inject)**
- *Why:* Raw scraped pages bloat context and cause drift in multi-source runs — Hermes's exact failure mode. Distill each source to only the facts bearing on the current sub-question *before* it touches the main reasoning context.
- *Artifact:* Mandatory per-source refine step between web_extract and synthesis; expose an optional `reason_context` param on web_extract so the distillation is conditioned on the live reasoning state, not just a static query string. Use a cheap/fast model.
- *Borrow:* **Search-o1** "Reason-in-Documents" module.
- *Impact: high · Effort: low*

**#4 — Factored verification + per-claim grounding gate with confidence labels**
- *Why:* "Has a citation" ≠ "is supported by the citation." Re-check each atomic claim in a *fresh context that does not see the draft*, so the verifier can't rubber-stamp the original hallucination.
- *Artifact:* A distinct pre-synthesis phase in SKILL.md: decompose draft → atomic, decontextualized claims → for each, isolated web_search→web_extract with schema `{claim, supported, evidence_url, evidence_quote}` → label `{high|medium|uncertain}` from corroboration count → strip/hedge/re-retrieve unsupported; render the tag beside every citation.
- *Borrow:* **Chain-of-Verification** (factored/draft-independent), **Self-RAG** ISSUP grounding gate, **Perplexity DR** (confidence labels — design inspiration only).
- *Impact: high · Effort: low*

**#5 — Research brief + context-isolated sub-agents + finding compression**
- *Why:* Underspecified queries cause drift; naive multi-agent causes ~15× token blow-up. A frozen written brief anchors everything; each sub-question is researched in an isolated context that returns only a compressed cited digest; writing stays single-agent.
- *Artifact:* Mandatory brief step (objective, sub-questions, scope/exclusions, success criteria) + per-sub-question isolated turns returning digests, not raw markdown; final synthesis in ONE agent.
- *Borrow:* **LangChain open_deep_research** (brief-as-anchor, research-vs-writing split, two-layer compression).
- *Impact: high · Effort: low*

**#6 — Gap-finder reflection + adaptive stopping**
- *Why:* Gives a principled signal for WHEN to keep going and what to ask next — which Hermes lacks. Convergence = "no salient new question from retrieved-but-uncited material."
- *Artifact:* Per-round step: maintain a "seen vs cited" ledger over extracted snippets; run a moderator prompt over the uncited pool to emit next sub-queries; each round returns `{sufficient, gaps, next_queries}`; stop on insufficient-gaps or budget. Min-rounds floor for high-stakes queries.
- *Borrow:* **STORM/Co-STORM** moderator gap-finder; **mshumer/OpenDeepResearcher** adaptive `{sufficient, gaps, next_queries}` stop.
- *Impact: high · Effort: low*

**#7 — Query-from-reasoning + sequential one-hop decomposition**
- *Why:* On multi-hop questions, derive each next web_search from the latest reasoning sentence / open sub-goal (the next information gap) rather than re-paraphrasing the original question; chain dependent hops where each answer feeds the next query. Periodically re-anchor to the brief to avoid drift.
- *Artifact:* Procedure rules in SKILL.md: first hop = user question; subsequent queries = most-recent reasoning gap; accumulate evidence across hops; cap hop budget.
- *Borrow:* **IRCoT** (query = last reasoning sentence), **Self-Ask** (sequential dependent decomposition + per-hop citation trail).
- *Impact: high · Effort: low*

**#8 — Triple hard-stop budget + typed activity log**
- *Why:* SKILL.md text is advisory; a real agent needs deterministic cost/latency caps and an auditable live trace.
- *Artifact:* Three numeric guards — `max_iterations`, `time_limit_seconds`, `max_fetches` — wired as counters into the tool/orchestration layer so the run jumps to synthesis when any is exhausted; emit one typed line per step `{type: search|extract|analyze|reflect|verify|synthesis, status, message, timestamp, iteration}`. URL/fetch count doubles as the cost meter.
- *Borrow:* **Firecrawl /deep-research** (depth×time×URL budget + typed activity stream).
- *Impact: medium · Effort: low*

**#9 — Breadth×depth dials + minimal threaded state object**
- *Why:* Makes thoroughness/cost a legible user control and bounds the tree (fan wide, then converge).
- *Artifact:* User-facing `breadth`/`depth` args; geometric decay `breadth_next=ceil(breadth/2)`; the ONLY state threaded down a branch is `{learnings[], followUpQuestions[], visitedUrls[]}`. Map onto effort tiers (quick / detailed / report).
- *Borrow:* **dzhng/deep-research** (recursion + decay); effort tiers from **LearningCircuit/local-deep-research**.
- *Impact: medium · Effort: low*

**#10 — Multi-query fan-out + Reciprocal Rank Fusion de-dup, plus evidence dedup**
- *Why:* Reformulate into 3–5 angles, merge ranked SERPs by RRF (rank-only, calibration-free; web_search has no comparable scores), then web_extract only top-N fused URLs — boosts recall, demotes single-query flukes, cuts redundant extraction. Then dedup at canonical-URL + near-duplicate level so syndicated reposts don't fake triangulation.
- *Artifact:* RRF merge step (k=60, keyed on canonical URL) + MMR/embedding-cluster evidence dedup in SKILL.md.
- *Borrow:* **RAG-Fusion** (Raudaschl/rag-fusion); MMR/canonical dedup technique.
- *Impact: medium · Effort: low*

**#11 — Adaptive source routing to specialized backends**
- *Why:* Academic claims → Semantic Scholar/arXiv/PubMed; code/API → GitHub search; general → web_search. Higher-quality, domain-appropriate evidence.
- *Artifact:* Thin source-specific wrappers as MCP servers or skills + a routing rule in the research prompt. (Package each as both MCP + lazy skill.)
- *Borrow:* **LearningCircuit/local-deep-research** (engine routing).
- *Impact: medium · Effort: medium*

**#12 — Benchmark-driven eval + regression harness**
- *Why:* Measure quality instead of vibes; verify the 30%-fewer-steps / token claims on Hermes's own traces before bigger bets.
- *Artifact:* Wire DeepResearch Bench (RACE LLM-as-judge), BrowseComp, plus a small in-house golden-report set; LLM-as-judge auto-grading of trajectories.
- *Borrow:* DeepResearch Bench / BrowseComp; **WebVoyager** LLM-as-judge.
- *Impact: medium · Effort: medium*

*(Bigger-bet candidate, ranked lower: code-as-action sandboxed inner loop from **smolagents** — exposes web_search/web_extract as Python-callable functions so one block fans out/dedups/extracts and returns only distilled results. Impact medium, effort high due to sandbox isolation. Prototype the cheap interim — a batch multi-query web_search variant — first.)*

---

## 2. Design of #1 — the `deep-research` orchestration skill

A bounded, observable, ReAct-disciplined loop. Implemented as SKILL.md procedure + thin counter plumbing in the tool layer for hard stops. Gated by the existing research-quality config flag.

### Inputs / dials
- `question` (required)
- `breadth` (queries per round, default 4), `depth` (max rounds, default 2) → `breadth_next = ceil(breadth/2)` each level
- `effort` tier: `quick` (1 round, breadth 3, ≤6 fetches), `detailed` (2–3 rounds), `report` (3+ rounds, factored verification on) — tier presets the budgets below
- Hard budgets (enforced as counters, not prose): `max_iterations`, `time_limit_seconds`, `max_fetches`

### Phase 0 — Scope & brief
1. If question is underspecified (missing budget/region/timeframe/use-case), ask 2–3 clarifying questions (interactive) or write explicit assumptions (autonomous).
2. Emit a frozen **research brief**: `{objective, sub_questions[], scope/exclusions, success_criteria}`. This is the single source of truth for all fan-out and the writer. (LangChain ODR)

### Phase 1 — Iterative research loop (per round, until stop)
For each open sub-question (run in isolated context / sub-turn — LangChain ODR):
1. **Reformulate** into `breadth` query angles (RAG-Fusion). First round derives queries from the brief; later rounds derive each query from the latest reasoning gap / branch follow-ups (IRCoT/Self-Ask), re-anchoring to the brief periodically.
2. **Fan out** parallel `web_search` calls (issue them in one batch).
3. **Merge** ranked SERPs by RRF (k=60, canonical-URL key); dedup against `visitedUrls`; MMR-dedup near-duplicate domains. Select top-N URLs.
4. **Read**: `web_extract` each top URL with the sub-question as `query` (and a JSON `schema` like `{learnings[], followUpQuestions[], key_facts:[{claim, source_url, quote, date}]}` when structured facts are wanted). 
5. **Distill before inject** (Search-o1): each source is compressed — conditioned on sub-question + current reasoning state (`reason_context`) — to a short cited digest. Raw markdown never enters the orchestrator context.
6. **Ledger**: record each extracted snippet as `seen`; tag which feed the working notes as `cited`.
7. **Reflect / gap-find** (STORM moderator + mshumer): over the retrieved-but-uncited pool, emit `{sufficient: bool, gaps[], next_queries[]}`. Append a bounded Reflexion note (`tried / dead-end / next`) — last ~5 only — so dead queries/domains aren't retried.
8. Branch state threaded down = ONLY `{learnings[], followUpQuestions[], visitedUrls[]}` (dzhng). Child query = parent goal + branch follow-ups; `breadth` decays.
9. Emit a typed activity line for every step (Firecrawl).

### Stopping criteria (whichever first)
- `sufficient == true` AND uncited pool yields no salient new question (convergence signal), with a min-rounds floor for `report` tier;
- any hard budget exhausted (`max_iterations` / `time_limit_seconds` / `max_fetches`);
- `depth` reached.
On stop → jump to synthesis with whatever evidence exists.

### Phase 2 — Factored verification (on for `detailed`/`report`)
1. Draft → decompose into atomic, decontextualized claims (resolve pronouns/"the company"); drop opinion/non-verifiable.
2. For each load-bearing claim, run an **isolated** web_search→web_extract that sees ONLY the claim, not the draft (CoVe factored). Schema `{claim, supported: full|partial|none, evidence_url, evidence_quote}`.
3. Cross-source triangulation: require ≥2 independent domains per load-bearing fact; on conflict, surface both positions with dated citations and prefer the more recent/authoritative/corroborated; assign `{high|medium|uncertain}` (Self-RAG ISSUP, Perplexity-style labels). Unsupported/partial → re-retrieve, hedge, or delete.

### Phase 3 — Synthesis (single agent — never parallelized)
1. Outline-first from curated digests; write each section conditioned only on its assigned sources (STORM), maintaining a sentence→source map.
2. **Citation-during-generation**: prompt is assembled from numbered pre-vetted excerpts (URL + date); model emits inline `[n]` bound to those exact excerpts as it writes — no post-hoc attachment.
3. NLI/entailment gate on each cited sentence (citation recall + precision); strip/regenerate citations that fail.
4. Render confidence tag beside each citation; deduplicated reference list; append a "Follow-up questions" + open-gaps section.

### Tool usage summary
- `web_search`: discovery + RRF fan-out + verification corroboration rounds.
- `web_extract` **query mode**: per-source distillation against a sub-question/claim (the grounded Observation).
- `web_extract` **schema mode**: emit the `{learnings, followUpQuestions}` state object and the `{claim, supported, evidence_url}` verification records.
- Headless browser: fallback only for JS-heavy/paywalled/anti-bot pages (tiered extraction; map-then-crawl on blocks).

---

## 3. Quick wins vs bigger bets

**Quick wins (low effort, ship first — pure SKILL.md / system-prompt edits over existing tools):**
- #2 Retrieve-don't-assert ReAct invariant (system prompt)
- #3 Compress-before-inject distillation (Search-o1)
- #4 Factored per-claim verification + confidence labels (CoVe / Self-RAG / Perplexity)
- #5 Research brief + isolated sub-agents + finding compression (LangChain ODR)
- #6 Gap-finder + adaptive stop (STORM / mshumer)
- #7 Query-from-reasoning + sequential decomposition (IRCoT / Self-Ask)
- #9 breadth×depth dials + threaded state object (dzhng)
- #10 RRF dedup + evidence dedup (RAG-Fusion)
- These together *are* the #1 orchestration skill — bundle them into one SKILL.md rewrite.

**Medium (small plumbing beyond prompt text):**
- #8 Triple hard-stop budget counters + typed activity log (Firecrawl) — needs counters in the tool/orchestration layer for deterministic stops.
- #3 `reason_context` param on web_extract.
- #11 Source-routing MCP/skill wrappers (arXiv/PubMed/Semantic Scholar/GitHub) — LDR.

**Bigger bets (defer until eval justifies):**
- #12 Benchmark/eval harness (DeepResearch Bench, BrowseComp, LLM-as-judge) — build early enough to measure the above.
- Code-as-action sandboxed inner loop (**smolagents**) — high effort (E2B/Docker isolation, quotas, timeouts); prototype a batch multi-query web_search variant first to capture ~60% of the win without arbitrary code execution.
- Persistent "knowledge library" (encrypted/embedded cross-run source store, **LDR**) — only if Hermes wants durable personal-research memory; land later as an MCP-backed vector store.

**Do NOT vendor any repo** — every system above (gpt-researcher, STORM/DSPy, LangGraph, smolagents, dzhng, Firecrawl endpoint, the Next.js clones) is either redundant with Hermes's native scraper/tool loop or coupled to infra Hermes doesn't use. Port the algorithms as markdown procedures; keep the per-turn tool path as default/fallback.

## Verified systems

### gpt-researcher (assafelovic)
- URL: https://github.com/assafelovic/gpt-researcher
- adopt_as: prompt_pattern | impact: medium | effort: low
- best idea: The plan-then-parallel-fan-out structure: decompose the question into N independent sub-queries, fan out web_search+web_extract concurrently, then synthesize — with an explicit source-redundancy rule (each load-bearing fact must appear in >=2 independent sources; on disagreement, prefer the consensus/most-frequent value and surface the disagreement rather than silently picking one). Hermes can encode this entirely as a SKILL.md procedure plus its config-gated research-quality system prompt; it already has all the primitive tools (parallel web_search/web_extract, schema extraction) needed.

### Stanford STORM / Co-STORM (stanford-oval/storm)
- URL: https://github.com/stanford-oval/storm
- adopt_as: skill | impact: high | effort: low
- best idea: Adopt the Moderator's "ask about retrieved-but-unincorporated information" gap-finder — cheapest, highest-leverage idea and a near-perfect fit for Hermes's deep-research loop, giving a principled signal for WHEN to keep researching and what to ask next (which Hermes lacks). HOW: implement as procedure changes to the existing deep-research SKILL.md, no new infra. (a) Maintain a lightweight "seen vs cited" ledger over snippets returned by web_extract (the fit-markdown scraper already yields query/schema-scoped chunks); tag which were actually used in the draft. (b) After each synthesis pass, run a "moderator" prompt over the retrieved-but-uncited pool to surface unincorporated facts and emit the next round of targeted sub-queries via web_search/web_extract; stop when that pool yields no salient new questions — this becomes the loop's convergence signal. Secondary idea to layer on: perspective-guided fan-out — before fan-out, derive 3-6 distinct stakeholder/perspective lenses (optionally seeded by one web_search of "similar overview articles") and generate sub-queries per perspective instead of one flat decomposition.

### HuggingFace smolagents / open-deep-research
- URL: https://github.com/huggingface/smolagents
- adopt_as: prompt_pattern | impact: medium | effort: high
- best idea: Adopt the code-as-action pattern for the research inner loop only — not the whole framework. Hermes' most token-expensive phase is search→dedup→extract, where each query, each candidate URL, and each extraction currently costs a separate tool-call round-trip plus full intermediate results back into context. The single best idea: give Hermes a sandboxed code-execution tool that exposes its existing web_search and web_extract (plus the HTML→fit-markdown scraper) as callable functions, so the model can write ONE block that fans out N queries, dedups/normalizes URLs, ranks, and extracts top-k — returning only the distilled result to context instead of every intermediate page. HOW for Hermes: (1) Wrap the existing web_search/web_extract/scraper as Python-callable shims inside a restricted interpreter (reuse MCP-style boundary or a Docker/E2B sandbox); (2) expose it as a new optional 'research_code' tool gated by the same config flag as the research-quality system prompt; (3) encode the orchestration recipe (loop, dedup by canonical URL, filter by query relevance, batch-extract, summarize) as a SKILL.md so the behavior is documented/portable without forking smolagents; (4) keep the per-turn tool path as the default/fallback. This captures the token-efficiency and expressiveness win against Hermes' actual bottleneck while reusing all existing tooling.

### LangChain open_deep_research
- URL: https://github.com/langchain-ai/open_deep_research
- adopt_as: skill | impact: high | effort: low
- best idea: The research-brief-as-anchor pattern, paired with sub-agent context isolation + mandatory finding compression. Single best adopt: make the deep-research skill (1) emit an explicit written research brief from the user dialogue before any search, then (2) for parallel sub-research, give each sub-agent an isolated context and require it to return only a compressed, cited findings digest — never raw page dumps — to the orchestrator, while keeping final synthesis/writing in a single agent.

### dzhng/deep-research
- URL: https://github.com/dzhng/deep-research
- adopt_as: skill | impact: medium | effort: low
- best idea: The structured {learnings, followUpQuestions} object threaded down a breadth×depth recursion, with breadth geometrically decaying per level (newBreadth=ceil(breadth/2)) — a tiny, legible state object as the only thing passed between recursion levels, and two integer dials exposed to the user.

### nickscamara/open-deep-research
- URL: https://github.com/nickscamara/open-deep-research
- adopt_as: prompt_pattern | impact: low | effort: low
- best idea: The single transferable idea is the schema-driven multi-page extract step: issue a search, then run ONE extraction pass with an explicit output schema across the whole batch of result URLs, so the reasoning model receives pre-structured fields instead of raw markdown from each page. Hermes already has web_search + web_extract (with query/schema extraction) and a deep-research skill, so it would adopt this as a refinement to the deep-research SKILL.md procedure: after fan-out search, define a JSON schema for the target facts and call web_extract with that schema across all collected URLs in a structured pass before synthesis — tightening the existing pipeline rather than adding a dependency. The persistence/auth layer is Vercel/Next.js-specific and not relevant to Hermes (it's a CLI-style agent, not a hosted webapp).

### mshumer/OpenDeepResearcher
- URL: https://github.com/mshumer/OpenDeepResearcher
- adopt_as: skill | impact: medium | effort: low
- best idea: LLM-driven adaptive stopping with explicit gap-naming: each round the model returns {sufficient, gaps, next_queries}, which both terminates the loop and seeds the next searches.

### LearningCircuit/local-deep-research (LDR)
- URL: https://github.com/LearningCircuit/local-deep-research
- adopt_as: prompt_pattern | impact: medium | effort: medium
- best idea: Route research queries to specialized sources (academic/code/web/local) rather than one generic web_search, plus explicit quick/detailed/report effort tiers.

### btahir/open-deep-research
- URL: https://github.com/btahir/open-deep-research
- adopt_as: skill | impact: medium | effort: low
- best idea: see real_approach/best idea above

### Firecrawl /deep-research (Alpha)
- URL: https://docs.firecrawl.dev/features/alpha/deep-research
- adopt_as: prompt_pattern | impact: medium | effort: low
- best idea: Adopt the triple hard-stop budget (depth x wall-clock-time x URL/fetch count) coupled to a typed, streamed activity log, and make the URL-count budget double as the cost cap. Hermes already has a deep-research skill (SKILL.md) and a config-gated research-quality prompt, but those are soft guidance the model can ignore/overrun. Concretely: (1) Add three explicit numeric guards to the deep-research skill's procedure — max_iterations, time_limit_seconds, max_fetches — enforced as hard stops in the loop, not advisory text. Since the skill is markdown the model self-enforces; for a true hard stop, wire the counters into the web_search/web_extract tool layer (or a thin orchestrator) so the run terminates deterministically when any budget is exhausted, then jumps straight to synthesis. (2) Standardize the trace as a typed activity log — emit one structured line per step {type: search|extract|analyze|reasoning|synthesis, status, message, timestamp, depth/iteration} so the user gets a live, auditable reasoning trace and Hermes gets observability + a per-fetch cost meter. This turns Hermes' existing fan-out research from "best effort" into bounded, observable, cost-controlled.

### OpenAI Deep Research (closed product, design reference)
- URL: https://openai.com/index/introducing-deep-research/
- adopt_as: skill | impact: medium | effort: low
- best idea: Make explicit backtracking/strategy-pivoting and an upfront clarify-scope gate first-class steps in Hermes' research loop rather than hoping the model does them implicitly. (1) Clarify gate: before fanning out searches, if the question is underspecified, ask 2-3 scoping questions (or, in autonomous mode, write explicit assumptions). (2) Pivot/backtrack: after each fetch batch, force a short structured reflection — "is this path productive? what new evidence changes the plan?" — and allow the loop to drop/replace sub-queries. Hermes' existing deep-research skill already gestures at fan-out + verify; this sharpens it with the two disposition behaviors the page credits for the quality jump.

### Perplexity Deep Research (closed, design reference)
- URL: https://www.perplexity.ai/hub/blog/introducing-perplexity-deep-research
- adopt_as: skill | impact: high | effort: low
- best idea: Pre-synthesis adversarial verification pass with per-claim confidence labels (high/medium/uncertain) rendered next to each citation.

### ReAct
- URL: https://arxiv.org/abs/2210.03629
- adopt_as: prompt_pattern | impact: high | effort: low
- best idea: Enforce the reason -> retrieve/observe -> reason loop as a hard discipline so the model never asserts an external fact it could instead ground via web_search/web_extract, keeping every Thought/Action/Observation step in an inspectable trace. Hermes already has the mechanics (deep-research skill fans out searches, fetches, verifies); the adoptable gap is making the loop an explicit citation-or-retrieve invariant. HOW: (1) Bake the invariant into the config-gated research-quality system prompt — "before stating any non-trivial external fact, emit a Thought naming what to verify, then a web_search/web_extract Action, then ground the claim in the Observation; uncited external claims are not permitted." (2) Encode the explicit Thought/Action/Observation trace template in deep-research SKILL.md so traces stay auditable. web_extract's schema/query extraction is exactly the grounded-Observation step. No new infra — a prompt/skill discipline layer over existing tools.

### IRCoT (Interleaving Retrieval with Chain-of-Thought Reasoning)
- URL: https://arxiv.org/abs/2212.10509
- adopt_as: prompt_pattern | impact: high | effort: low
- best idea: Adopt query-rewriting-from-reasoning: in Hermes's multi-step research loop, generate the next web_search query from the agent's latest reasoning sentence / current sub-goal rather than reissuing the original question. This makes each successive search target the specific next information gap (next hop) instead of redundantly re-searching the broad original question, which improves recall on multi-hop questions and reduces wasted/duplicate retrievals.

### Self-Ask
- URL: https://arxiv.org/abs/2210.03350
- adopt_as: prompt_pattern | impact: medium | effort: low
- best idea: Sequential one-hop decomposition loop in the deep-research skill: emit a single follow-up sub-question, search/extract for it, record a cited intermediate answer, repeat until no follow-ups remain, then compose — giving both higher multi-hop accuracy and a per-hop citation trail.

### FLARE (Forward-Looking Active REtrieval augmented generation)
- URL: https://arxiv.org/abs/2305.06983
- adopt_as: prompt_pattern | impact: medium | effort: low
- best idea: Confidence/assertion-gated retrieval with the about-to-be-asserted statement as the query: retrieve precisely when the agent is about to state a specific fact it cannot back up, and search for that exact claim — rather than retrieving once up front or on every step.

### Search-o1
- URL: https://github.com/RUC-NLPIR/Search-o1
- adopt_as: prompt_pattern | impact: high | effort: low
- best idea: The Reason-in-Documents distillation pass: never inject a raw scraped page into the reasoning context — first run a cheap, query-and-reasoning-conditioned compression that reduces each source to only the answer-relevant facts, then feed that distilled snippet back into the chain.

### Self-RAG
- URL: https://arxiv.org/abs/2310.11511
- adopt_as: skill | impact: high | effort: low
- best idea: Adopt the per-claim grounding/supportedness gate (ISSUP), not the training machinery. Hermes uses frozen Claude models, so it can't add special vocab tokens — but it can adopt the scoring DISCIPLINE: before a statement enters the final answer, verify whether its cited source span actually entails it, labeled fully-supported / partial / unsupported, then delete, hedge, or re-retrieve anything not fully supported. Implement as a grounding-verification pass in the deep-research SKILL.md: after web_search/web_extract gather sources, run a structured LLM-judge step that attaches a candidate evidence span to each load-bearing sentence and emits a tri-state support label; unsupported/partial claims trigger a targeted re-query via web_search. No new tools needed — web_extract's query/schema extraction already pulls candidate spans, and the config-gated research-quality system prompt is the natural home. Also borrow adaptive retrieval: gate whether a sub-claim needs retrieval at all to cut wasted fetches.

### CRAG (Corrective RAG)
- URL: https://arxiv.org/abs/2401.15884
- adopt_as: skill | impact: medium | effort: low
- best idea: Relevance-grade scraped sources before synthesis and auto-escalate to web_search when the corpus scores poorly.

### Reflexion
- URL: https://arxiv.org/abs/2303.11366
- adopt_as: skill | impact: medium | effort: low
- best idea: Single best idea: maintain a small running "reflection log" inside a research run — after each search/extract round, the agent writes a 1-3 sentence note ("query X returned only SEO spam; domain Y paywalled; pivot to primary-source/site: filters") and that note is fed back into the next iteration's context, so dead-end queries and dead domains aren't retried. HOW Hermes adopts it cheaply: add a Reflexion-style step to the deep-research SKILL.md (and/or the config-gated research-quality system prompt) — no new tool needed. The skill already does fan-out search + verify; insert an explicit "REFLECT" instruction between iterations: after a batch of web_search/web_extract calls, the agent appends a short structured note to a scratchpad memory section (tried-queries, dead-ends, what-to-try-next) and is instructed to read that section before issuing the next batch. Hermes's natural "feedback signal" = the verifier/adversarial-check stage already in deep-research (did the extracted evidence actually answer the sub-question?), which plays the Evaluator role. Keep the buffer bounded (last N reflections) to avoid context bloat. This is a prompt/skill-level change, not a code change.

### Chain-of-Verification (CoVe)
- URL: https://arxiv.org/abs/2309.11495
- adopt_as: skill | impact: high | effort: low
- best idea: Adopt the "factored verification" principle: after drafting the research report, decompose it into atomic claims and re-verify each in an isolated context that is NOT conditioned on the draft, using a fresh web_search/web_extract per claim rather than asking the model to self-check the draft inline. Hermes' deep-research skill already does adversarial verification, but likely conditions on the assembled draft; the load-bearing upgrade is the isolation: extract claims, then for each, spawn an independent verification query (search + extract) whose prompt contains ONLY the claim, not the surrounding narrative or other claims. Implement as a 'factored-verification' phase in the deep-research SKILL.md: (a) parse final draft into a checklist of atomic, individually-checkable claims; (b) for each claim run an independent web_search→web_extract with a schema like {claim, supported: bool, evidence_url, evidence_quote}; (c) revise or flag/remove unsupported claims (Factor+Revise step). Because the verifier never sees the draft's framing, it can't rubber-stamp the original hallucination. This reuses existing tools (web_search, schema-based web_extract) with zero new infra — only a prompt/skill-procedure change, optionally gated behind the existing research-quality system-prompt config flag.

### RAG-Fusion
- URL: https://github.com/Raudaschl/rag-fusion
- adopt_as: skill | impact: medium | effort: low
- best idea: Adopt the multi-query + Reciprocal Rank Fusion pattern as a research procedure layered over Hermes's existing web_search tool. The single best idea is RRF as a rank-only, calibration-free merge across multiple query reformulations — it boosts recall and demotes single-query flukes without needing any score normalization, which fits perfectly because web_search returns ranked result lists with no comparable scores. HOW: encode it as a deep-research SKILL.md step rather than new infra. The skill instructs the agent to (a) fan the user's question into 3-5 reformulations (different angles/synonyms/specificity), (b) issue parallel web_search calls, (c) merge by RRF with k=60 keyed on canonical URL, (d) web_extract only the top-N fused URLs. No vector DB or embeddings needed — Hermes operates over live web search, so just reuse its ranked SERPs as the input lists. This is a pure prompt/procedure change, gated behind the existing research-quality system prompt.

