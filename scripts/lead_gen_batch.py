#!/usr/bin/env python3
"""Aviation lead-gen batch runner (deterministic; for cron / reproducible runs).

End-to-end over Hermes' own tools: resilient search router -> scrape company
pages -> LinkedIn x-ray + cheap LLM buyer-resolution -> enrich_contact (honest
email confidence) -> qualify/score/draft (synthesis LLM) -> CSV (30-col lead
schema) + summary. The interactive path is the `lead-generation` skill (the
agent loop drives the same tools); this script is the unattended equivalent.

Per-lead reasoning uses OpenRouter models (NOT the main gpt-5.5), tiered:
  LEADGEN_LLM          synthesis/qualify/draft   (default deepseek/deepseek-v4-pro)
  LEADGEN_RESOLVE_LLM  cheap buyer-snippet clean (default deepseek/deepseek-v4-flash)
OpenRouter key from $OPENROUTER_API_KEY or ~/.hermes/.openrouter_key.

Usage:
  venv/bin/python scripts/lead_gen_batch.py --out leads.csv --target 50 [--concurrency 4]
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from hermes_cli.config import reload_env  # noqa: E402
import agent.auxiliary_client as ac  # noqa: E402
from agent.auxiliary_client import extract_content_or_reasoning  # noqa: E402
from hermes_cli.plugins import _ensure_plugins_discovered  # noqa: E402
from agent.web_search_registry import get_provider  # noqa: E402
from plugins.web.hermes.scraper.pipeline import scrape, ScrapeOptions  # noqa: E402
from tools.enrich_contact_tool import enrich_contact  # noqa: E402
from agent.research_ranking import reciprocal_rank_fusion, domain_of  # noqa: E402
from agent.research_citations import canonicalize_url, extract_urls  # noqa: E402

LLM = os.getenv("LEADGEN_LLM", "deepseek/deepseek-v4-pro")
RESOLVE_LLM = os.getenv("LEADGEN_RESOLVE_LLM", "deepseek/deepseek-v4-flash")

JUNK = {"linkedin.", "indeed.", "glassdoor.", "wikipedia.", "youtube.", "facebook.", "twitter.",
        "x.com", "reddit.", "ziprecruiter.", "builtin.", "tealhq.", "scribd.", "sofemaonline.",
        "sassofia.", "studysmarter.", "vaia.com", "aircraftit.", "appsruntheworld.", "google.",
        "bing.", "medium.", "quora.", "europa.eu", ".gov", ".edu", "faa.", "easa.", "icao.",
        "caa.", "ainonline.", "aviationweek.", "flightglobal.", "aerotime.", "simpleflying.",
        "pprune.", "crunchbase.", "bloomberg.", "yahoo.", "amazon."}
DISCOVERY = [
    "aircraft technical records management company", "aircraft records management services provider",
    "aircraft redelivery records services company", "aircraft lease return records management company",
    "CAMO continuing airworthiness management organisation", "aircraft records digitization company",
    "MRO aircraft maintenance technical records provider", "aircraft asset management records lessor",
    '"technical records" aircraft maintenance company', "aircraft component back to birth traceability company",
    "aviation records scanning indexing service company", "aircraft transition records management company",
    "engine MRO records management company", "aircraft teardown parts traceability company",
    "part 145 maintenance organisation technical records", "aircraft leasing technical records team",
    "aviation document control records management company", "business jet maintenance records management",
    "helicopter MRO technical records company", "aircraft component repair records management",
]
EXISTING_TOOLS = ["AirVault", "AMOS", "TRAX", "Ramco", "Ultramain", "CAMP", "Rusada", "OASES",
                  "Swiss-AS", "IFS", "SharePoint"]
SCRAPE_PATHS = ["/team", "/our-team", "/leadership", "/people", "/management", "/about",
                "/about-us", "/contact", "/contact-us", "", "/services"]
COLUMNS = ["Lead ID", "Priority", "Lead Score", "Company Name", "Website", "Country", "City / Region",
           "Company Type", "Estimated Size", "Aviation Segment", "Why This Company Fits Raeon Labs",
           "Documentation Pain Hypothesis", "Evidence Found", "Source URLs", "Trigger Event / Signal",
           "Existing Tools Mentioned", "Target Person Name", "Target Person Title", "Target Person LinkedIn",
           "Email", "Email Confidence", "Email Source / Pattern", "Backup Contact", "Personalization Angle",
           "Suggested Offer", "Outreach Email Subject", "Outreach Email Draft", "Follow-up Email Draft",
           "Notes", "Status",
           # Appended (not inserted) so existing positional consumers keep
           # their column offsets. C5: rows whose evidence/source URLs were
           # never fetched this batch are FLAGGED here, never dropped.
           "Evidence Verified"]

LEAD_PROMPT = """Qualify a customer for Raeon Labs (AI-assisted aviation documentation/technical-records systems: extract fields from aviation PDFs/scans, link task cards/work orders/logs, cut manual indexing, improve records search + audit-readiness, integrate around AirVault/AMOS/TRAX/Ramco/Ultramain — not replace them). ICP: MRO, airline maintenance, CAMO, aircraft lessors, records providers, component/engine repair, teardown/parts.

COMPANY: {domain} ({title})
TOOLS ON SITE: {tools}
LIKELY BUYER: {person_name} — {person_title}
PAGE TEXT:
{company_text}

Score /100: industry fit /25, doc-pain /25, buyer quality /20, outreach confidence /15, trigger /15. Priority A=80-100,B=65-79,C=50-64,Reject<50. If NOT an ICP (regulator, magazine/news, association, unrelated, or a pure software vendor with no records-ops), Reject.
Email tone: calm, specific, operational, no hype/buzzwords; "I noticed…". Subject 3-7 words. Structure: specific observation -> doc pain -> Raeon relevance (extraction, review queues, search, integrate around existing tools) -> soft CTA. Sign "Tanishq, Raeon Labs". GREETING: greet by the buyer's FIRST name only if a real name is given and it MUST match; if buyer is "(unknown)" use "Hi there," and address the team — NEVER invent a name.
Return ONLY valid JSON (escape newlines as \\n, no raw newlines in strings):
{{"company_name":"","country":"","city":"","company_type":"","estimated_size":"","aviation_segment":"","why_fits":"","pain_hypothesis":"","evidence":"","trigger":"","lead_score":0,"priority":"","personalization":"","offer":"","subject":"","email_draft":"","followup":"","notes":""}}"""

_RESOLVE_PROMPT = """From these LinkedIn snippets for company "{company}", pick the SINGLE best decision-maker for aviation technical-records / documentation outreach (VP/Director/Head/Manager of Technical Records, Continuing Airworthiness/CAMO, Maintenance, Quality, Engineering Records). Use ONLY the snippets; never invent.
Snippets:
{snippets}
Return ONLY JSON: {{"name":"","title":"","linkedin":"","matched":true}} (matched=false if none fits; name=real full name only; title=role only)."""

_stats = {"json_fail": 0}

# Second-level labels that act as public suffixes (co.uk, com.au, …) so the
# dedupe base for "acme.co.uk" is "acme.co.uk", not "co.uk" (which would
# silently collapse every UK/AU/... company into one candidate).
_PUBLIC_SLDS = {"co", "com", "net", "org", "ac", "gov", "edu"}


def _is_junk_domain(d: str) -> bool:
    """Label-boundary junk match: '.x.com.' must not swallow 'flex.com'."""
    dd = f".{d}."
    for a in JUNK:
        if f".{a.strip('.')}." in dd:
            return True
    return False


def _dedupe_base(d: str) -> str:
    """Registrable-ish base domain used to dedupe candidate companies."""
    parts = d.split(".")
    if len(parts) >= 3 and parts[-2] in _PUBLIC_SLDS and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _key() -> str:
    env = os.getenv("OPENROUTER_API_KEY", "").strip()
    if env:
        return env
    kf = Path.home() / ".hermes/.openrouter_key"
    return kf.read_text().strip() if kf.exists() else ""


KEY = ""


def _repair_json(s):
    for fn in (lambda x: x,
               lambda x: re.sub(r'\\(?!["\\/bfnrtu])', r'\\\\', x),
               lambda x: re.sub(r'\\(?!["\\/bfnrtu])', r'\\\\', x).replace("\n", " ").replace("\r", " ")):
        try:
            return json.loads(fn(s))
        except Exception:
            continue
    return None


async def llm_json(prompt, model=None, max_tokens=4000):
    model = model or LLM
    for i in range(2):
        try:
            p = prompt if i == 0 else prompt + "\n\nPrevious reply was invalid JSON. Return STRICTLY valid JSON, newlines as \\n."
            resp = await ac.async_call_llm(task="lead_gen", provider="openrouter", model=model,
                                           api_key=KEY, temperature=0.2, max_tokens=max_tokens,
                                           messages=[{"role": "user", "content": p}])
            txt = extract_content_or_reasoning(resp) or ""
            m = re.search(r"\{.*\}", txt, re.DOTALL)
            obj = _repair_json(m.group(0)) if m else None
            if obj is not None:
                return obj
        except Exception:
            pass
    _stats["json_fail"] += 1
    return {}


async def resolve_buyer(company, snippets):
    if not snippets:
        return {}
    txt = "\n".join(f"- {s['title']} | {s['url']}" for s in snippets[:5])
    j = await llm_json(_RESOLVE_PROMPT.format(company=company, snippets=txt), model=RESOLVE_LLM, max_tokens=400)
    nm = str((j or {}).get("name", "")).strip()
    if j and j.get("matched") and nm and 1 <= len(nm.split()) <= 4 and "@" not in nm:
        return {"name": nm, "title": str(j.get("title", "") or "").strip()[:70], "linkedin": str(j.get("linkedin", "") or "")}
    return {}


def srch(prov, q, n=8):
    try:
        return (prov.search(q, n).get("data") or {}).get("web") or []
    except Exception:
        return []


def _output_email(enr: dict) -> str:
    """Sheet-facing email: blank a dead guess (audit low-severity fix).

    When MX is invalid AND confidence is 'Not found', the internal record may
    still carry a common-pattern guess for traceability — but the OUTPUT row
    must not present an unusable address as if it were actionable. Internals
    (Email Confidence / Source columns) are kept verbatim.
    """
    if str(enr.get("email_confidence") or "") == "Not found" and enr.get("mx_valid") is False:
        return ""
    return str(enr.get("email", "") or "")


def verify_row_evidence(row: dict, known_urls: set) -> tuple[bool, list]:
    """C5 grounding: check the row's evidence/source URLs against the batch.

    ``known_urls`` is the canonicalized set of URLs this batch actually touched
    (scraped pages + discovery search results). Any URL cited in the row's
    'Evidence Found' / 'Source URLs' fields outside that set was hallucinated
    by the synthesis LLM. Returns ``(verified, offending_urls)`` — flagging
    only; rows are never dropped.
    """
    cited = []
    for field in ("Evidence Found", "Source URLs"):
        cited.extend(extract_urls(str(row.get(field) or "")))
    offending, seen = [], set()
    for u in cited:
        c = canonicalize_url(u)
        if c and c not in known_urls and c not in seen:
            seen.add(c)
            offending.append(u)
    return (not offending, offending)


def _evidence_verified_value(verified: bool, offending: list) -> str:
    if verified:
        return "Yes"
    shown = ", ".join(offending[:3])
    more = f" (+{len(offending) - 3} more)" if len(offending) > 3 else ""
    return f"No — unretrieved: {shown}{more}"


async def process(prov, sem, idx, domain, title, src, fetched_urls):
    async with sem:
        try:
            texts, urls = [], [src]
            for path in SCRAPE_PATHS:
                try:
                    r = await scrape(f"https://{domain}{path}", ScrapeOptions(use_llm=False))
                    if r.markdown and len(r.markdown) > 200:
                        texts.append(r.markdown[:6000]); urls.append(f"https://{domain}{path}")
                        fetched_urls.add(canonicalize_url(f"https://{domain}{path}"))
                except Exception:
                    pass
                if len(texts) >= 4:
                    break
            ctext = "\n\n".join(texts)[:14000]
            if not ctext:
                return None
            tools_found = [t for t in EXISTING_TOOLS if t.lower() in ctext.lower()] or ["Not found"]
            cname = domain.split(".")[0]
            xr = await asyncio.to_thread(srch, prov,
                  f'site:linkedin.com/in (technical records OR airworthiness OR maintenance OR quality OR records) "{cname}"', 6)
            snippets = [{"title": h.get("title", ""), "url": h.get("url", "")} for h in xr if "linkedin.com/in" in h.get("url", "")]
            b = await resolve_buyer(cname, snippets)
            pn, pt, pli = b.get("name", ""), b.get("title", ""), b.get("linkedin", "")
            enr = json.loads(enrich_contact(domain, pn, pt, page_text=ctext, contact_url=f"https://{domain}/contact"))
            j = await llm_json(LEAD_PROMPT.format(domain=domain, title=title, tools=tools_found,
                              company_text=ctext[:6500], person_name=pn or "(unknown)", person_title=pt or "(unknown)"))
            try:
                score = int(j.get("lead_score") or 0)
            except (TypeError, ValueError):
                score = 0
            return {
                "Lead ID": "", "Priority": j.get("priority", ""), "Lead Score": score,
                "Company Name": j.get("company_name") or cname, "Website": f"https://{domain}",
                "Country": j.get("country", ""), "City / Region": j.get("city", ""),
                "Company Type": j.get("company_type", ""), "Estimated Size": j.get("estimated_size", ""),
                "Aviation Segment": j.get("aviation_segment", ""), "Why This Company Fits Raeon Labs": j.get("why_fits", ""),
                "Documentation Pain Hypothesis": j.get("pain_hypothesis", ""), "Evidence Found": j.get("evidence", ""),
                "Source URLs": " | ".join(urls[:4]), "Trigger Event / Signal": j.get("trigger", ""),
                "Existing Tools Mentioned": ", ".join(tools_found), "Target Person Name": pn or "(not found)",
                "Target Person Title": pt or "(not found)", "Target Person LinkedIn": pli,
                "Email": _output_email(enr), "Email Confidence": enr.get("email_confidence", ""),
                "Email Source / Pattern": enr.get("email_source", ""), "Backup Contact": enr.get("backup_contact", ""),
                "Personalization Angle": j.get("personalization", ""), "Suggested Offer": j.get("offer", ""),
                "Outreach Email Subject": j.get("subject", ""), "Outreach Email Draft": j.get("email_draft", ""),
                "Follow-up Email Draft": j.get("followup", ""), "Notes": j.get("notes", ""), "Status": "Drafted",
            }
        except Exception as e:
            print(f"  [{idx}] {domain} FAILED: {e}")
            return None


async def run(out_path: Path, target: int, cap: int, concurrency: int):
    reload_env()
    _ensure_plugins_discovered()
    prov = get_provider("resilient") or get_provider("ddgs")
    if prov is None:
        print("No search provider available."); return
    print(f"Discovery ({len(DISCOVERY)} queries) via {prov.name}...")
    fused = reciprocal_rank_fusion([srch(prov, q) for q in DISCOVERY])
    seen, candidates = set(), []
    for it in fused:
        d = domain_of(it.get("url", ""))
        if not d or _is_junk_domain(d):
            continue
        base = _dedupe_base(d)
        if base in seen:
            continue
        seen.add(base)
        candidates.append((d, it.get("title", ""), it.get("url", "")))
        if len(candidates) >= cap:
            break
    print(f"  {len(candidates)} candidates after filter")
    sem = asyncio.Semaphore(concurrency)
    fetched_urls: set = set()
    results = await asyncio.gather(*[process(prov, sem, i + 1, d, t, s, fetched_urls)
                                     for i, (d, t, s) in enumerate(candidates)])
    rows = [r for r in results if r]
    # C5 grounding: evidence/source URLs must come from what this batch actually
    # touched — scraped pages + discovery results. LLM-invented URLs get the row
    # FLAGGED in 'Evidence Verified' (never silently dropped).
    known_urls = set(fetched_urls)
    known_urls |= {canonicalize_url(it.get("url", "")) for it in fused}
    known_urls.discard("")
    for r in rows:
        verified, offending = verify_row_evidence(r, known_urls)
        r["Evidence Verified"] = _evidence_verified_value(verified, offending)
    qualified = sorted([r for r in rows if (r["Lead Score"] or 0) >= 50], key=lambda r: -r["Lead Score"])[:target]
    for i, r in enumerate(qualified, 1):
        r["Lead ID"] = f"L{i:03d}"
    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS); w.writeheader()
        for r in qualified:
            w.writerow(r)
    pri = Counter(r["Priority"] for r in qualified)
    emails = Counter(r["Email Confidence"] for r in qualified)
    unverified = sum(1 for r in qualified if r["Evidence Verified"] != "Yes")
    print(f"\nWrote {len(qualified)} qualified leads (from {len(rows)} processed) -> {out_path}")
    print(f"  A={pri['A']} B={pri['B']} C={pri['C']} | emails={dict(emails)} | json_fail={_stats['json_fail']}"
          f" | evidence_unverified={unverified}")


def main(argv=None):
    global KEY
    ap = argparse.ArgumentParser(description="Aviation lead-gen batch")
    ap.add_argument("--out", default="leads.csv")
    ap.add_argument("--target", type=int, default=50)
    ap.add_argument("--cap", type=int, default=64, help="max companies to process")
    ap.add_argument("--concurrency", type=int, default=4)
    args = ap.parse_args(argv)
    KEY = _key()
    if not KEY:
        print("No OpenRouter key ($OPENROUTER_API_KEY or ~/.hermes/.openrouter_key)."); return 1
    asyncio.run(run(Path(args.out), args.target, args.cap, args.concurrency))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
