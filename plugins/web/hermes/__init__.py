"""Hermes native web-extract plugin — bundled, auto-loaded.

A dependency-light, deterministic HTML→fit-markdown scraper (plus optional
BM25-filtered LLM query/structured extraction) that gives Hermes a self-hosted,
keyless ``web_extract`` backend rivalling hosted scrapers. See
:mod:`plugins.web.hermes.provider` and :mod:`plugins.web.hermes.scraper`.
"""

from __future__ import annotations


def register(ctx) -> None:
    """Register the Hermes native scraper provider with the plugin context."""
    from plugins.web.hermes.provider import HermesNativeWebSearchProvider

    ctx.register_web_search_provider(HermesNativeWebSearchProvider())
