"""Hermes native web scraper.

A dependency-light, deterministic HTML → "fit markdown" extraction pipeline
modelled on the cores of crawl4ai (heuristic content pruning, fit-markdown,
BM25 query filtering) and ScrapeGraphAI (aggressive HTML cleanup, token-aware
chunking, map-reduce LLM extraction).

Design goals:
  * The DEFAULT path (fetch → clean → prune → markdown) uses NO LLM and NO
    third-party scraping API — this is the Firecrawl-equivalent core.
  * The LLM only engages when the caller asks a question (``query``) or wants
    structured output (``schema``); even then we pre-filter chunks with BM25 so
    we send the model the smallest relevant slice.

Public entrypoint: :func:`plugins.web.hermes.scraper.pipeline.scrape`.
"""

from __future__ import annotations

__all__ = ["scrape", "ScrapeResult", "ScrapeOptions"]


def __getattr__(name: str):  # pragma: no cover - thin lazy re-export
    # Lazy re-export so importing the package (e.g. during plugin discovery)
    # does not pull in lxml before deps are ensured.
    if name in {"scrape", "ScrapeResult", "ScrapeOptions"}:
        from plugins.web.hermes.scraper import pipeline

        return getattr(pipeline, name)
    raise AttributeError(name)
