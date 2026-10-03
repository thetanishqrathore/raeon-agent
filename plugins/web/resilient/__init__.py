"""Resilient search router — bundled, auto-loaded meta-provider.

Routes ``web_search`` across the configured free backends (SearXNG, ddgs,
Brave, Tavily, Google CSE) with quota-aware, health-aware fallback. Activate
via ``web.search_backend: resilient``. See :mod:`plugins.web.resilient.provider`.
"""

from __future__ import annotations


def register(ctx) -> None:
    """Register the resilient router provider with the plugin context."""
    from plugins.web.resilient.provider import ResilientSearchProvider

    ctx.register_web_search_provider(ResilientSearchProvider())
