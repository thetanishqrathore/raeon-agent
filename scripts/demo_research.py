#!/usr/bin/env python3
"""Exercise Raeon's research components offline with synthetic source material.

No network requests, model calls, API keys, or persistent user state. Run from
the checkout after installing the package and the native scraper dependencies.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.research_citations import validate_answer_citations
from agent.research_ranking import rank_and_select
from agent.research_state import ResearchLedger
from plugins.web.hermes.scraper.pipeline import ScrapeOptions, scrape_html


def main() -> None:
    source_url = "https://example.com/research"
    html = """<!doctype html><html><head><title>Synthetic research note</title></head>
    <body><nav>Home | Subscribe</nav><main><article>
    <h1>Document review workflow</h1>
    <p>This fictional team reviews extracted document fields before exporting
    them to its records system. Each field retains a link to the source page,
    so a reviewer can inspect the evidence and correct uncertain values.</p>
    <p>The demonstration uses synthetic content to show extraction and source
    tracking. It makes no claim about a real company or measured time savings.</p>
    </article></main><footer>Privacy | Contact</footer></body></html>"""
    scraped = scrape_html(html, source_url, ScrapeOptions())
    ranked = rank_and_select([
        [{"url": source_url, "title": "Synthetic research note"},
         {"url": "https://example.org/comparison", "title": "Synthetic comparison"}],
        [{"url": source_url + "?utm_source=demo", "title": "Same note, tracked URL"}],
    ], max_results=2, per_domain=1)
    messages = [{"role": "tool", "name": "web_extract", "content": json.dumps({
        "results": [{"url": source_url, "title": scraped.title,
                     "content": scraped.markdown}],
    })}]
    supported = validate_answer_citations(f"Source: {source_url}", messages)
    fabricated = validate_answer_citations("Source: https://example.net/not-retrieved", messages)
    with tempfile.TemporaryDirectory(prefix="raeon-demo-") as tmp:
        db_path = str(Path(tmp) / "research.db")
        ledger = ResearchLedger("offline-demo", db_path=db_path)
        ledger.set_brief("Inspect a synthetic document-review workflow.")
        ledger.add_learning("Reviewers inspect extracted fields against source pages.",
                            source_url=source_url, confidence="demo-only")
        reopened = ResearchLedger("offline-demo", db_path=db_path).get_state()
    print(json.dumps({
        "mode": "offline, synthetic fixtures; no LLM or live research",
        "extracted_markdown": scraped.markdown,
        "ranked_sources": [item["canonical_url"] for item in ranked],
        "retrieved_url_passes_provenance_check": supported.ok,
        "unretrieved_url_is_flagged": bool(fabricated.fabricated),
        "ledger_survives_reopen": len(reopened["learnings"]) == 1,
        "limitation": "URL provenance does not establish factual or semantic support.",
    }, indent=2))


if __name__ == "__main__":
    main()
