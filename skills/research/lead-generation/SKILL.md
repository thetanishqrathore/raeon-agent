---
name: lead-generation
description: Find, qualify, enrich, score, and draft outreach for B2B leads end-to-end into a sheet — research-grounded, honest confidence, never sends email. Built for aviation documentation/technical-records ICP but general.
when_to_use: When asked to build a list of qualified leads with decision-makers, contact details, and personalized outreach drafts (e.g. "find N companies that…, find the buyer, draft emails, put it in a sheet"). Composes the deep-research and lead-enrichment skills.
---

# Lead generation — discover → qualify → enrich → score → draft

A research-and-outreach loop. **Research only — never send email.** Every row
must make obvious: why the company fits, why this person, what problem you'd
solve, the evidence, and how confident the contact is. Don't hallucinate; mark
inferred data as inferred.

## Pipeline (per the brief's ICP + scoring rubric)
1. **Discover** — fan out `web_search` with ICP queries (segment + role + tool +
   region terms). The search backend is auto-selected/failed-over by the router
   — you never pick an engine. Use `rank_sources` to RRF-merge + dedupe + take a
   one-per-domain shortlist. Drop aggregators (LinkedIn/Wikipedia/job boards as
   "companies"), regulators, and news/magazines.
2. **Research the company** — `web_extract` the site (home/about/services) to
   confirm ICP fit and gather evidence + existing tools (AMOS/TRAX/AirVault/…).
   Distill; don't dump raw pages. Use `think` between batches.
3. **Find the buyer** — LinkedIn x-ray (`site:linkedin.com/in (<roles>) "<company>"`)
   read from snippets only; resolve to ONE clean {name, title, /in/ url}.
4. **Enrich the contact** — apply the `lead-enrichment` skill: `enrich_contact`
   with the company domain + buyer + extracted page text → email + honest
   confidence (Verified/Likely/Inferred/Contact-form/Not-found) + source + backup.
5. **Score** — apply the brief's rubric (industry fit /25, doc-pain /25, buyer
   quality /20, outreach confidence /15, trigger /15 → A/B/C/Reject). Keep only
   score ≥ 50 unless told otherwise.
6. **Draft** — a calm, specific, operational initial email + follow-up. Greet the
   buyer by first name (must match the enriched name); if unknown, "Hi there,".
   No hype, no buzzwords, no fake familiarity. Structure: specific observation →
   pain hypothesis → relevance → soft CTA. Subject 3–7 words.
7. **Write the sheet** — one row per qualified lead with the exact columns the
   brief specifies (Lead ID, Priority, Score, Company, Website, Country, City,
   Type, Size, Segment, Why-fits, Pain, Evidence, Source URLs, Trigger, Existing
   Tools, Person, Title, LinkedIn, Email, Email Confidence, Email Source, Backup,
   Personalization, Offer, Subject, Email Draft, Follow-up, Notes, Status).
   Use Google Sheets (if a Sheets tool is available) or emit CSV for import.
8. **Summary report** — counts (A/B/C), top leads, common pain patterns, common
   buyer titles, best personalization angles, recommended next niches, and any
   data-quality issues.

## Tiered models (cost discipline)
Route the cheap work cheap: page distillation / buyer-snippet resolution →
fast/cheap model; qualification + drafting → a capable synthesis model; keep the
frontier model for hard reasoning only. (Config: `auxiliary.web_extract`,
`delegation`, and the batch script's `LEADGEN_LLM` / `LEADGEN_RESOLVE_LLM`.)

## Unattended / batch (cron)
For a scheduled or large reproducible run, use the committed batch runner instead
of doing every step by hand:
`venv/bin/python scripts/lead_gen_batch.py --out leads.csv --target 50`
It performs this exact pipeline deterministically and writes the 30-column CSV.

## Anti-patterns (don't)
- Don't invent emails or present inferred ones as Verified.
- Don't scrape behind LinkedIn's login — public search snippets only.
- Don't add generic aviation leads with no documentation angle, or a generic CEO
  email when a better ops/maintenance/records contact exists.
- Don't send any email — draft only.
