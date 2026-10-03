"""Hermes native web-extract provider.

A zero-API-key, self-hosted alternative to Firecrawl/Exa/Tavily for the
``web_extract`` capability. Fetches pages itself (HTTP, escalating to the
Camoufox browser when needed) and converts them to clean "fit markdown" with a
deterministic pruning pipeline — optionally answering a ``query`` or extracting
a ``schema`` via the auxiliary LLM.

Selected via::

    web:
      extract_backend: hermes

It is also the keyless fallback when no other extract backend is configured.
``lxml`` / ``rank-bm25`` are lazy-installed on first use (``search.hermes`` in
:data:`tools.lazy_deps.LAZY_DEPS`).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List

from agent.web_search_provider import WebSearchProvider

logger = logging.getLogger(__name__)

_MAX_CONCURRENCY = 5


class HermesNativeWebSearchProvider(WebSearchProvider):
    """Native fetch + deterministic extraction provider (extract-only)."""

    @property
    def name(self) -> str:
        return "hermes"

    @property
    def display_name(self) -> str:
        return "Hermes (native scraper)"

    def is_available(self) -> bool:
        # Always usable: no API key required. Parsing deps lazy-install on first
        # extract; if that ultimately fails we surface a per-URL error.
        return True

    def supports_search(self) -> bool:
        return False

    def supports_extract(self) -> bool:
        return True

    async def extract(self, urls: List[str], **kwargs: Any) -> List[Dict[str, Any]]:
        """Scrape ``urls`` concurrently → fit-markdown (+ optional query/schema)."""
        from tools.interrupt import is_interrupted

        if is_interrupted():
            return [{"url": u, "title": "", "content": "", "error": "Interrupted"} for u in urls]

        try:
            from plugins.web.hermes.scraper.deps import ensure_parse_deps

            ensure_parse_deps()
        except ImportError as exc:
            return [
                {"url": u, "title": "", "content": "",
                 "error": f"Hermes scraper deps unavailable: {exc}"}
                for u in urls
            ]

        from plugins.web.hermes.scraper.pipeline import ScrapeOptions, scrape

        opts = ScrapeOptions(
            format=kwargs.get("format") or "markdown",
            citations=bool(kwargs.get("citations", False)),
            query=kwargs.get("query"),
            schema=kwargs.get("schema"),
            instruction=kwargs.get("instruction", "") or "",
            use_llm=bool(kwargs.get("use_llm", True)),
            allow_browser=bool(kwargs.get("allow_browser", True)),
        )

        sem = asyncio.Semaphore(_MAX_CONCURRENCY)

        async def _one(u: str) -> Dict[str, Any]:
            async with sem:
                if is_interrupted():
                    return {"url": u, "title": "", "content": "", "error": "Interrupted"}
                try:
                    res = await scrape(u, opts)
                    return res.to_provider_dict()
                except Exception as exc:  # noqa: BLE001 — per-URL isolation
                    logger.warning("Hermes scrape failed for %s: %s", u, exc)
                    return {"url": u, "title": "", "content": "", "error": f"scrape failed: {exc}"}

        return await asyncio.gather(*[_one(u) for u in urls])

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "Hermes (native scraper)",
            "badge": "free",
            "tag": "Self-hosted fetch + clean fit-markdown extraction. No API key.",
            "env_vars": [],
        }
