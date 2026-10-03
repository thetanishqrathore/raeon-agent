"""Google Programmable Search (Custom Search JSON API) — bundled, auto-loaded.

Free-tier (100/day) search backend. Activates when GOOGLE_CSE_API_KEY and
GOOGLE_CSE_CX are set. See :mod:`plugins.web.google_cse.provider`.
"""

from __future__ import annotations


def register(ctx) -> None:
    from plugins.web.google_cse.provider import GoogleCSEWebSearchProvider

    ctx.register_web_search_provider(GoogleCSEWebSearchProvider())
