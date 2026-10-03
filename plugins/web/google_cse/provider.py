"""Google Programmable Search (Custom Search JSON API) — free-tier plugin.

Search-only provider over Google's official Custom Search JSON API. The free
tier is **100 queries/day** (then it errors until the daily reset), which makes
it a clean daily "100 real-Google queries" floor for the resilient router.

Config keys::

    web:
      search_backend: "google-cse"

Auth env vars::

    GOOGLE_CSE_API_KEY=...    # https://developers.google.com/custom-search/v1/introduction
    GOOGLE_CSE_CX=...         # the Programmable Search Engine id (cx)
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict

from agent.web_search_provider import WebSearchProvider

logger = logging.getLogger(__name__)

_CSE_ENDPOINT = "https://www.googleapis.com/customsearch/v1"


class GoogleCSEWebSearchProvider(WebSearchProvider):
    """Search-only Google Custom Search provider (free tier: 100/day)."""

    @property
    def name(self) -> str:
        return "google-cse"

    @property
    def display_name(self) -> str:
        return "Google Programmable Search (Free 100/day)"

    def is_available(self) -> bool:
        """True when both the API key and the search-engine id (cx) are set."""
        return bool(os.getenv("GOOGLE_CSE_API_KEY", "").strip()
                    and os.getenv("GOOGLE_CSE_CX", "").strip())

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return False

    def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        import httpx

        api_key = os.getenv("GOOGLE_CSE_API_KEY", "").strip()
        cx = os.getenv("GOOGLE_CSE_CX", "").strip()
        if not api_key or not cx:
            return {"success": False, "error": "GOOGLE_CSE_API_KEY / GOOGLE_CSE_CX is not set"}

        # CSE returns at most 10 results per request.
        num = max(1, min(int(limit), 10))

        try:
            resp = httpx.get(
                _CSE_ENDPOINT,
                params={"key": api_key, "cx": cx, "q": query, "num": num},
                timeout=15,
            )
        except httpx.RequestError as exc:
            logger.warning("Google CSE request error: %s", exc)
            return {"success": False, "error": f"Could not reach Google CSE: {exc}"}

        # 429/403 carry the daily-quota / rate-limit signal — surface the
        # reason text so the router classifies it as QUOTA_EXCEEDED.
        if resp.status_code in (429, 403):
            reason = ""
            try:
                errs = (resp.json().get("error") or {}).get("errors") or []
                reason = (errs[0].get("reason") if errs else "") or ""
            except Exception:  # noqa: BLE001
                pass
            return {"success": False,
                    "error": f"Google CSE quota/limit (HTTP {resp.status_code} {reason})"}
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            return {"success": False, "error": f"Google CSE returned HTTP {exc.response.status_code}"}

        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            return {"success": False, "error": "Could not parse Google CSE response as JSON"}

        items = data.get("items") or []
        web_results = [
            {
                "title": str(it.get("title", "")),
                "url": str(it.get("link", "")),
                "description": str(it.get("snippet", "")),
                "position": i + 1,
            }
            for i, it in enumerate(items[:limit])
        ]
        logger.info("Google CSE '%s': %d results", query, len(web_results))
        return {"success": True, "data": {"web": web_results}}

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "Google Programmable Search (Free)",
            "badge": "free",
            "tag": "Official Google API — 100 queries/day free, search only.",
            "env_vars": [
                {"key": "GOOGLE_CSE_API_KEY",
                 "prompt": "Google Custom Search API key",
                 "url": "https://developers.google.com/custom-search/v1/introduction"},
                {"key": "GOOGLE_CSE_CX",
                 "prompt": "Programmable Search Engine id (cx)",
                 "url": "https://programmablesearchengine.google.com/"},
            ],
        }
