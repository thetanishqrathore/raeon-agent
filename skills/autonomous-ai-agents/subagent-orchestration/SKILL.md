---
name: subagent-orchestration
description: Use when a task fans out across independent items (many URLs/files/leads to process, parallel research angles, a long job to run in the background, or a draft that needs fresh-context verification). Teaches when to delegate vs work directly, how to write child contracts, parallel/background modes, model routing, and synthesis.
version: 1.0.0
author: Hermes Agent + Tanishq
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Delegation, Subagents, Orchestration, Parallel, Fan-Out, Verification]
    related_skills: [deep-research, claude-code, plan]
---

# Subagent Orchestration

Coordinate `delegate_task` children the way strong agent harnesses do: fan out
across independent work, keep each child's context isolated, get back
*findings* (never transcripts), and synthesize in the parent.

## When to delegate — and when NOT to

Delegate when:
- The work **fans out across independent items**: read N source pages, process
  N files, enrich N leads, investigate N hypotheses. Independence is the test.
- A subtask would **pollute your context** with bulk it produces (long page
  dumps, logs, transcripts) when you only need the conclusions.
- You want a **fresh-context verifier**: a child that never saw your reasoning
  critiques a draft far better than self-review.
- A long task should run in the **background** while you keep working
  (`background=true`).

Do NOT delegate:
- Single-file reads, one search, or anything you can finish in 1–3 tool calls
  directly — the spawn overhead exceeds the work.
- Work that needs the conversation's shared context. **Children see nothing of
  this conversation** — no messages, no memory, no prior tool results. If the
  brief can't be made self-contained, don't delegate it.
- Sequential steps where each depends on the previous result (do those
  yourself, or chain: delegate → synthesize → delegate).

## Mechanics (what the tool actually does)

- **Parallel batch:** pass multiple tasks in one `delegate_task` call — they
  run concurrently (cap ~3 at a time; larger batches queue). One batch of 3
  beats three sequential calls.
- **Background:** `background=true` returns immediately; completion surfaces
  as a new turn when you're idle. Use for long research/builds; keep working
  meanwhile.
- **Depth 1:** children cannot spawn grandchildren. Plan the fan-out at your
  level.
- **Budgets are independent:** each child gets its own iteration budget
  (~50) — delegation is also how you stretch a long job past your own budget.
- **Toolsets:** narrow each child to what it needs (e.g. `toolsets=["web"]`
  for readers). Fewer tools = faster, safer children.
- **Model routing:** children inherit the parent model by default — usually
  correct (on this deployment the parent runs on a flat-rate subscription
  backend, so a "cheaper" metered model is a false economy). Route a child to
  another provider/model only when the parent model is metered or the task is
  huge-volume grunt work.

## The contract — the part most people get wrong

Every child brief must be a **complete, self-contained work order**:

1. **Context** the child needs (it knows NOTHING of this conversation):
   who/what this is for, one sentence.
2. **Exact inputs**: the URLs/paths/names — never "the files we discussed".
3. **Deliverable shape**: per-item structure, e.g. "For each URL return:
   3–5 concrete facts (numbers, names, what shipped), one notable quote,
   one line on why a solo AI-automation founder would care."
4. **Anti-padding clause**: "Return findings only — no preamble, no
   methodology narration."
5. **Failure protocol**: "If a page won't load, say FAILED: <url> <reason>
   and move on" — never let one bad item sink the batch.

## Proven patterns

- **Parallel source-reading:** shortlist N items → 2–3 children, each assigned
  3–4 URLs with the contract above → synthesize. (This is how the Founder Feed
  briefing reads primary sources.)
- **Research fan-out:** decompose a question into 2–4 *complementary* angles
  (not overlapping rephrasings), one child per angle, then reconcile —
  disagreements between children are signal, chase them. See `deep-research`
  for the full loop.
- **Fresh-context critic:** hand a child your draft + the acceptance criteria
  ONLY (not your reasoning): "Verify every claim against the cited source;
  list what fails and why." Adversarial framing beats "review this".
- **Background + foreground:** kick off the slow child with `background=true`,
  do the fast parts yourself, weave in the child's results when they arrive.
- **Coding hand-off:** for real coding tasks prefer delegating to Claude Code
  or Codex CLI via the terminal (`claude -p …`) — see the `claude-code` skill —
  and reserve `delegate_task` children for research/reading/verification
  around the code work.

## Synthesis (parent's job, never skipped)

- Merge child findings into ONE coherent answer; resolve conflicts explicitly
  ("child A's number is from the primary source; using it").
- Attribute: keep which child/source produced each load-bearing fact.
- Report failures honestly: "2 of 8 pages failed to load" — never paper over
  gaps with invention.
- Never paste raw child output into the final answer — the user asked you,
  not the committee.
