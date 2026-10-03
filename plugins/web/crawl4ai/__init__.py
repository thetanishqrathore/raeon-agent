"""Crawl4AI web-extract plugin — bundled, auto-loaded.

A self-hosted, browser-rendered ``web_extract`` backend powered by a local
Crawl4AI service (https://github.com/unclecode/crawl4ai). Pages are fetched in
headless Chromium server-side and converted to clean fit-markdown, which makes
this backend far more reliable than plain HTTP fetching on JS-heavy pages —
while staying free and fully self-hosted.

Deployment: see ``docker/crawl4ai/README.md`` (container ``hermes-crawl4ai``
next to ``hermes-searxng``). Selected via ``web.extract_backend: crawl4ai``.
"""

from __future__ import annotations


def register(ctx) -> None:
    """Register the Crawl4AI provider with the plugin context."""
    from plugins.web.crawl4ai.provider import Crawl4AIWebSearchProvider

    ctx.register_web_search_provider(Crawl4AIWebSearchProvider())
