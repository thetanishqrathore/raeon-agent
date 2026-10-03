---
name: lead-enrichment
description: Turn a qualified company + target role into a contact record (person, title, LinkedIn, email) with honest confidence — free-first, no fabricated emails.
when_to_use: After you've identified a company and a likely buyer role and need the decision-maker's name, LinkedIn, and email for outreach. Pairs with the deep-research skill for the discovery phase.
---

# Lead enrichment — company → qualified contact

Discovery (deep-research / search) finds the **company** and the **role**; this
finds the **person** and their **contact details**, with confidence you can
defend. Free-first: public pages + email-pattern inference + MX check. **Never
fabricate an email or overstate confidence** — the whole point is a record a
human can trust.

## Stage A — find the person (name · title · LinkedIn)
1. **LinkedIn x-ray** (no login wall): `web_search` for
   `site:linkedin.com/in ("Director of Technical Records" OR "CAMO Manager" OR "Head of Quality") "<Company>"`.
   Read the **result snippet** (name + title + the `/in/` URL) — do NOT open
   LinkedIn itself.
2. **Company pages**: `web_extract` the `/team`, `/leadership`, `/about`,
   `/contact` pages, plus press releases and conference-speaker pages. These
   give names, titles, and often a sample email.
3. Pick the **best-fit buyer** (VP/Director of Technical Records, Continuing
   Airworthiness / CAMO Manager, Director of Maintenance, Head of Quality, …).
   A job post that names the hiring company is a strong lead **and** a trigger.

## Stage B — find / derive the email (use `enrich_contact`)
1. `web_extract` the company's `contact` / `about` / `team` pages.
2. Call **`enrich_contact`** with: `domain` (the company domain), `person_name`,
   `person_title`, and `page_text` = the text you just extracted. It will:
   - harvest sample emails from the page, **infer the org's pattern**
     (`first.last@`, `flast@`, …), and construct the target's email;
   - run an **MX check** on the domain;
   - return `email`, `email_confidence`, `email_source`, and `backup_contact`.
3. Record its `email_confidence` and `email_source` **verbatim** in the sheet.

## Stage C — backup
If no personal email is derivable, use the returned `backup_contact` (generic
inbox / contact form / LinkedIn) and set Email Confidence to `Contact form only`.

## Confidence ladder (be honest)
| Value | Meaning |
|---|---|
| **Verified** | a verification provider (e.g. Hunter, if configured) confirmed it |
| **Likely** | pattern confirmed by ≥2 sample emails + MX valid |
| **Inferred** | pattern from 1 sample, or a common-pattern guess + MX valid |
| **Contact form only** | no personal email; generic inbox / form exists |
| **Not found** | nothing — record honestly, never guess |

On the free path most emails are **Likely / Inferred** — that's fine; mark them
as such. Setting up `HUNTER_API_KEY` (free tier ~25/mo) upgrades top leads to
**Verified**.

## Fills these lead-gen sheet columns
Target Person Name · Title · LinkedIn · Email · Email Confidence · Email
Source/Pattern · Backup Contact. These drive the lead's Buyer-quality and
Outreach-confidence scores.

## Anti-patterns (don't)
- Don't invent an email or present an inferred one as Verified.
- Don't scrape behind LinkedIn's login — use public search snippets only.
- Don't leave Email Confidence blank — every contact carries a level + source.
