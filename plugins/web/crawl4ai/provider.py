"""Crawl4AI web-extract provider — self-hosted, browser-rendered extraction.

Talks to a local `Crawl4AI <https://github.com/unclecode/crawl4ai>`_ service
(the ``hermes-crawl4ai`` container; see ``docker/crawl4ai/README.md``) over its
REST API. Each URL is rendered in headless Chromium **server-side** and comes
back as clean fit-markdown, so JS-heavy/SPA pages that defeat plain HTTP
fetching extract reliably — with zero API cost and no third-party data flow.

Selected via::

    web:
      extract_backend: crawl4ai
      extract_fallback: true      # default — fall back to the native scraper

Env (config-aware, i.e. ``hermes config set`` / ``~/.hermes/.env`` work)::

    CRAWL4AI_URL=http://127.0.0.1:11235
    CRAWL4AI_API_TOKEN=<token>    # required by crawl4ai >= 0.9.0 deployments
    CRAWL4AI_TIMEOUT=60           # optional, seconds per URL (default 60)

Contract notes
--------------
* **SSRF / policy guard runs BEFORE the request.** The crawl executes inside
  the Crawl4AI container, so the tool-level guard in ``web_extract_tool`` is
  not enough on its own for direct callers: this provider re-checks every URL
  against :func:`tools.url_safety.async_is_safe_url` and the website-access
  policy gate (:func:`tools.website_policy.check_website_access`) before any
  bytes leave the process, and re-checks the *final* (post-redirect) URL after
  the crawl — exactly mirroring the firecrawl provider's per-URL loop.
* **One request per URL.** A hard-blocked page makes the whole ``POST /crawl``
  request fail server-side (HTTP 500), so batching would let one bad URL
  poison the batch. Requests run concurrently under a small semaphore.
* **Typed errors** (:class:`Crawl4AIAuthError` etc.) are raised internally and
  flattened into per-URL ``error`` strings — ``extract()`` never raises for
  network/server trouble.
* **Automatic fallback**: when the service is down / times out / can't crawl a
  page, the native hermes scraper (:mod:`plugins.web.hermes`) takes over for
  the affected URLs unless ``web.extract_fallback: false``. Every result
  carries ``metadata.engine`` and fallback results carry an
  ``extraction_note`` naming the engine that actually produced the content.
  Policy/SSRF-blocked URLs are **never** retried through the fallback.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, List, Optional

import httpx

from agent.web_search_provider import WebSearchProvider
from tools.url_safety import async_is_safe_url
from tools.website_policy import check_website_access

logger = logging.getLogger(__name__)

_MAX_CONCURRENCY = 5
_DEFAULT_TIMEOUT_S = 60.0
# Server-side page budget: keep it comfortably below the HTTP read timeout so
# a slow page produces a *typed crawl error* (with the server's reason) rather
# than a client-side read timeout.
_PAGE_TIMEOUT_MARGIN_S = 10.0

_SSRF_BLOCK_MESSAGE = "Blocked: URL targets a private or internal network address"


# ---------------------------------------------------------------------------
# Typed errors
# ---------------------------------------------------------------------------

class Crawl4AIError(Exception):
    """Base class for Crawl4AI provider errors."""


class Crawl4AIAuthError(Crawl4AIError):
    """Server rejected our credentials (401/403) — check CRAWL4AI_API_TOKEN."""


class Crawl4AIUnavailableError(Crawl4AIError):
    """Service unreachable (connection refused/reset, DNS, bad gateway)."""


class Crawl4AITimeoutError(Crawl4AIError):
    """Request exceeded the client-side timeout budget."""


class Crawl4AICrawlError(Crawl4AIError):
    """Service reachable but the crawl itself failed (bot wall, page error)."""


# ---------------------------------------------------------------------------
# Config helpers (config-aware env, mirrors the SearXNG provider)
# ---------------------------------------------------------------------------

def _env_value(name: str) -> str:
    """Resolve ``name`` via Hermes config-aware env, then process env."""
    try:
        from hermes_cli.config import get_env_value

        val = get_env_value(name)
    except Exception:  # noqa: BLE001 — config layer optional in tests
        val = None
    if val is None:
        val = os.getenv(name, "")
    return (val or "").strip()


def _crawl4ai_url() -> str:
    return _env_value("CRAWL4AI_URL").rstrip("/")


def _crawl4ai_token() -> str:
    return _env_value("CRAWL4AI_API_TOKEN")


def _request_timeout_s() -> float:
    raw = _env_value("CRAWL4AI_TIMEOUT")
    try:
        val = float(raw)
        if val > 0:
            return val
    except (TypeError, ValueError):
        pass
    return _DEFAULT_TIMEOUT_S


def _extract_fallback_enabled() -> bool:
    """``web.extract_fallback`` from config.yaml — default True."""
    try:
        from hermes_cli.config import load_config

        val = load_config().get("web", {}).get("extract_fallback", True)
    except Exception:  # noqa: BLE001 — config unavailable → safe default
        return True
    if isinstance(val, str):
        return val.strip().lower() not in {"false", "0", "no", "off"}
    return bool(val)


def _client_factory(timeout_s: float) -> httpx.AsyncClient:
    """Build the HTTP client — module-level so tests can monkeypatch it."""
    headers = {"Content-Type": "application/json"}
    token = _crawl4ai_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.AsyncClient(
        base_url=_crawl4ai_url(),
        headers=headers,
        timeout=httpx.Timeout(timeout_s, connect=10.0),
    )


def check_crawl4ai_service(timeout_s: float = 5.0) -> Dict[str, Any]:
    """Synchronous liveness probe for status displays.

    Returns ``{"configured": bool, "reachable": bool, "version": str|None,
    "url": str, "error": str|None}``. ``/health`` is unauthenticated on the
    official image, so this needs no token. Never raises.
    """
    url = _crawl4ai_url()
    out: Dict[str, Any] = {
        "configured": bool(url), "reachable": False,
        "version": None, "url": url, "error": None,
    }
    if not url:
        out["error"] = "CRAWL4AI_URL is not set"
        return out
    try:
        resp = httpx.get(f"{url}/health", timeout=timeout_s)
        resp.raise_for_status()
        data = resp.json() if resp.content else {}
        out["reachable"] = True
        if isinstance(data, dict):
            out["version"] = data.get("version")
    except Exception as exc:  # noqa: BLE001 — status probe never raises
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


# ---------------------------------------------------------------------------
# Payload / response mapping
# ---------------------------------------------------------------------------

def _build_crawl_payload(
    url: str, *, format: Optional[str], query: Optional[str], page_timeout_ms: int,
) -> Dict[str, Any]:
    """Build the ``POST /crawl`` body (crawl4ai 0.9.x type/params dump format).

    ``query`` switches the fit-markdown content filter from structure-based
    pruning to BM25 relevance filtering so the returned markdown is biased
    toward passages that answer the query — degrading gracefully to plain
    markdown when the server rejects the filter (verified against 0.9.0).
    """
    if query:
        content_filter = {
            "type": "BM25ContentFilter",
            "params": {"user_query": query, "bm25_threshold": 1.0},
        }
    else:
        content_filter = {
            "type": "PruningContentFilter",
            "params": {"threshold": 0.48, "threshold_type": "fixed"},
        }
    crawler_params: Dict[str, Any] = {
        "cache_mode": "bypass",
        "page_timeout": page_timeout_ms,
        "excluded_tags": ["nav", "footer", "header", "aside", "form"],
        "remove_overlay_elements": True,
        "markdown_generator": {
            "type": "DefaultMarkdownGenerator",
            "params": {"content_filter": content_filter},
        },
    }
    if format == "html":
        # cleaned_html is always produced; nothing extra needed.
        pass
    return {
        "urls": [url],
        "browser_config": {"type": "BrowserConfig", "params": {"headless": True}},
        "crawler_config": {"type": "CrawlerRunConfig", "params": crawler_params},
    }


def _pick_content(result: Dict[str, Any], format: Optional[str]) -> tuple[str, str]:
    """Return ``(content, raw_content)`` honoring the requested format.

    Markdown (default): prefer ``fit_markdown`` when it carries real content,
    else ``raw_markdown``. HTML: ``cleaned_html`` (falls back to markdown).
    """
    md = result.get("markdown") or {}
    if not isinstance(md, dict):  # older servers returned a plain string
        md = {"raw_markdown": str(md)}
    raw_md = (md.get("raw_markdown") or "").strip()
    fit_md = (md.get("fit_markdown") or "").strip()
    # A fit filter can over-prune; treat a fit that lost >90% of a non-trivial
    # page as a filter miss and fall back to the raw markdown.
    if fit_md and (len(fit_md) >= 200 or len(fit_md) >= 0.1 * max(len(raw_md), 1)):
        chosen_md = fit_md
    else:
        chosen_md = raw_md or fit_md
    if format == "html":
        html = (result.get("cleaned_html") or result.get("html") or "").strip()
        return (html or chosen_md), raw_md
    return chosen_md, raw_md


class Crawl4AIWebSearchProvider(WebSearchProvider):
    """Extract-only provider backed by a self-hosted Crawl4AI service."""

    @property
    def name(self) -> str:
        return "crawl4ai"

    @property
    def display_name(self) -> str:
        return "Crawl4AI (self-hosted)"

    def is_available(self) -> bool:
        """Available when ``CRAWL4AI_URL`` is set (liveness checked per call)."""
        return bool(_crawl4ai_url())

    def supports_search(self) -> bool:
        return False

    def supports_extract(self) -> bool:
        return True

    # -- core ---------------------------------------------------------------

    async def _guard_url(self, url: str) -> Optional[Dict[str, Any]]:
        """Policy + SSRF pre-flight. Returns an error entry when blocked.

        MUST run before any request: the crawl executes server-side inside the
        Crawl4AI container, which can reach localhost/private ranges that the
        tool-level guard would have blocked for in-process fetches.
        """
        blocked = check_website_access(url)
        if blocked:
            logger.info(
                "Blocked crawl4ai extract for %s by rule %s",
                blocked["host"], blocked["rule"],
            )
            return {
                "url": url, "title": "", "content": "",
                "error": blocked["message"],
                "blocked_by_policy": {
                    "host": blocked["host"],
                    "rule": blocked["rule"],
                    "source": blocked["source"],
                },
            }
        if not await async_is_safe_url(url):
            logger.info("Blocked crawl4ai extract for %s (private/internal address)", url)
            return {"url": url, "title": "", "content": "", "error": _SSRF_BLOCK_MESSAGE,
                    "ssrf_blocked": True}
        return None

    async def _crawl_one(
        self,
        client: httpx.AsyncClient,
        url: str,
        *,
        format: Optional[str],
        query: Optional[str],
        timeout_s: float,
    ) -> Dict[str, Any]:
        """Crawl a single URL. Raises typed errors; caller flattens them."""
        page_timeout_ms = int(max(timeout_s - _PAGE_TIMEOUT_MARGIN_S, 15.0) * 1000)
        payload = _build_crawl_payload(
            url, format=format, query=query, page_timeout_ms=page_timeout_ms,
        )
        try:
            resp = await client.post("/crawl", json=payload)
        except httpx.TimeoutException as exc:
            raise Crawl4AITimeoutError(
                f"Crawl4AI request timed out after {timeout_s:.0f}s"
            ) from exc
        except httpx.TransportError as exc:
            raise Crawl4AIUnavailableError(
                f"Crawl4AI service unreachable at {_crawl4ai_url() or '<unset>'}: {exc}"
            ) from exc

        if resp.status_code in (401, 403):
            raise Crawl4AIAuthError(
                "Crawl4AI rejected the request "
                f"(HTTP {resp.status_code}) — check CRAWL4AI_API_TOKEN."
            )
        if resp.status_code >= 500:
            # The server 500s when the crawl itself fails (bot wall, nav error)
            # and puts the reason in {"error": ...} / {"detail": ...}.
            reason = ""
            try:
                body = resp.json()
                reason = str(body.get("error") or body.get("detail") or "")[:300]
            except Exception:  # noqa: BLE001
                reason = (resp.text or "")[:300]
            raise Crawl4AICrawlError(reason or f"HTTP {resp.status_code}")
        if resp.status_code >= 400:
            raise Crawl4AICrawlError(f"HTTP {resp.status_code}: {(resp.text or '')[:300]}")

        try:
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise Crawl4AICrawlError(f"unparseable response: {exc}") from exc

        results = data.get("results") or []
        if not data.get("success") or not results:
            raise Crawl4AICrawlError(str(data.get("error") or "empty crawl result")[:300])

        r = results[0]
        if not r.get("success"):
            raise Crawl4AICrawlError(str(r.get("error_message") or "crawl failed")[:300])

        final_url = r.get("redirected_url") or r.get("url") or url
        # Post-redirect re-check: policy + SSRF on the URL we actually landed on.
        if final_url != url:
            re_blocked = await self._guard_url(final_url)
            if re_blocked:
                re_blocked["url"] = final_url
                return re_blocked

        metadata = r.get("metadata") or {}
        if not isinstance(metadata, dict):
            metadata = {}
        title = str(metadata.get("title") or "")
        content, raw_content = _pick_content(r, format)
        entry: Dict[str, Any] = {
            "url": final_url,
            "title": title,
            "content": content,
            "raw_content": raw_content,
            "metadata": {
                "engine": "crawl4ai",
                "sourceURL": final_url,
                "statusCode": r.get("status_code"),
                "fetchedVia": "crawl4ai",
                "wordCount": len(content.split()),
                "title": title,
            },
        }
        if not content:
            entry["error"] = "Crawl4AI returned an empty page (no extractable content)"
        return entry

    async def extract(self, urls: List[str], **kwargs: Any) -> List[Dict[str, Any]]:
        """Extract ``urls`` via the Crawl4AI service (+ native fallback).

        Accepted kwargs (same surface as the hermes provider; unknown ones are
        ignored for forward compat):

        - ``format``: ``"markdown"`` (default) or ``"html"``.
        - ``query``: bias fit-markdown toward the query via BM25 filtering.
        - ``schema``: **degrades gracefully** — Crawl4AI's LLM extraction is
          not used (the service runs LLM-less here); the tool layer's targeted
          extraction handles schemas over the returned markdown. When called
          directly with a schema, results carry an ``extraction_note`` saying
          the markdown is raw source, not structured data.
        """
        from tools.interrupt import is_interrupted

        if is_interrupted():
            return [{"url": u, "title": "", "content": "", "error": "Interrupted"}
                    for u in urls]

        format = kwargs.get("format")
        query = kwargs.get("query")
        schema = kwargs.get("schema")
        timeout_s = _request_timeout_s()

        sem = asyncio.Semaphore(_MAX_CONCURRENCY)
        entries: List[Optional[Dict[str, Any]]] = [None] * len(urls)

        async with _client_factory(timeout_s) as client:

            async def _one(idx: int, u: str) -> None:
                async with sem:
                    if is_interrupted():
                        entries[idx] = {"url": u, "title": "", "content": "",
                                        "error": "Interrupted"}
                        return
                    guard = await self._guard_url(u)
                    if guard is not None:
                        entries[idx] = guard
                        return
                    try:
                        entries[idx] = await self._crawl_one(
                            client, u, format=format, query=query, timeout_s=timeout_s,
                        )
                    except Crawl4AIError as exc:
                        logger.warning("Crawl4AI extract failed for %s: %s", u, exc)
                        entries[idx] = {
                            "url": u, "title": "", "content": "",
                            "error": f"{type(exc).__name__}: {exc}",
                            "_crawl4ai_failed": True,
                        }
                    except Exception as exc:  # noqa: BLE001 — per-URL isolation
                        logger.warning(
                            "Crawl4AI extract crashed for %s: %s", u, exc, exc_info=True,
                        )
                        entries[idx] = {
                            "url": u, "title": "", "content": "",
                            "error": f"crawl4ai extract failed: {exc}",
                            "_crawl4ai_failed": True,
                        }

            await asyncio.gather(*[_one(i, u) for i, u in enumerate(urls)])

        results: List[Dict[str, Any]] = [e for e in entries if e is not None]
        results = await self._apply_fallback(results, kwargs)

        if schema:
            for r in results:
                if r.get("content") and not r.get("error") and not r.get("extraction_note"):
                    r["extraction_note"] = (
                        "crawl4ai returns page markdown only; schema extraction "
                        "did not run at the provider level. Treat the content as "
                        "raw source text unless the tool layer distilled it."
                    )
        return results

    # -- fallback layering ----------------------------------------------------

    async def _apply_fallback(
        self, results: List[Dict[str, Any]], kwargs: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Re-run crawl4ai-failed URLs through the native hermes scraper.

        Only engine failures are retried — policy/SSRF blocks and interrupts
        stay blocked. Controlled by ``web.extract_fallback`` (default on).
        """
        failed_idx = [
            i for i, r in enumerate(results) if r.pop("_crawl4ai_failed", False)
        ]
        if not failed_idx:
            return results
        if not _extract_fallback_enabled():
            for i in failed_idx:
                results[i].setdefault("metadata", {})["engine"] = "crawl4ai"
            return results

        fallback = _get_fallback_provider()
        if fallback is None:
            return results

        urls = [results[i]["url"] for i in failed_idx]
        reasons = {results[i]["url"]: results[i].get("error", "") for i in failed_idx}
        logger.info(
            "Crawl4AI fallback: retrying %d URL(s) via native hermes scraper", len(urls),
        )
        try:
            fb_kwargs = {
                k: v for k, v in kwargs.items()
                if k in ("format", "query", "schema", "instruction", "use_llm",
                         "citations", "allow_browser")
            }
            fb_results = await fallback.extract(urls, **fb_kwargs)
        except Exception as exc:  # noqa: BLE001 — keep original typed errors
            logger.warning("Native fallback extract failed: %s", exc)
            return results

        by_url = {r.get("url"): r for r in fb_results if isinstance(r, dict)}
        for pos, i in enumerate(failed_idx):
            orig_url = urls[pos]
            fb = by_url.get(orig_url)
            if fb is None and pos < len(fb_results) and isinstance(fb_results[pos], dict):
                fb = fb_results[pos]  # final_url differed — trust ordering
            if not fb:
                continue
            reason = reasons.get(orig_url, "")
            if fb.get("content") and not fb.get("error"):
                fb.setdefault("metadata", {})["engine"] = "hermes-fallback"
                note = (
                    "crawl4ai could not fetch this page "
                    f"({reason[:160]}); content below was produced by the "
                    "native hermes scraper fallback."
                )
                if len(fb.get("content") or "") < 200:
                    note += (
                        " The fallback content is very short — the page is "
                        "likely bot-protected or JS-only; treat it as "
                        "incomplete and consider browser_navigate."
                    )
                fb["extraction_note"] = note
                results[i] = fb
            else:
                # Both engines failed — surface both reasons.
                results[i]["error"] = (
                    f"crawl4ai: {reason[:200]} | hermes fallback: "
                    f"{str(fb.get('error') or 'no content')[:200]}"
                )
        return results

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "Crawl4AI (self-hosted)",
            "badge": "free · self-hosted",
            "tag": (
                "Browser-rendered extraction via your own Crawl4AI service — "
                "handles JS-heavy pages. See docker/crawl4ai/README.md."
            ),
            "env_vars": [
                {
                    "key": "CRAWL4AI_URL",
                    "prompt": "Crawl4AI service URL (e.g. http://127.0.0.1:11235)",
                    "url": "https://github.com/unclecode/crawl4ai",
                },
                {
                    "key": "CRAWL4AI_API_TOKEN",
                    "prompt": "Crawl4AI API token (required by crawl4ai >= 0.9)",
                    "optional": True,
                },
            ],
        }


def _get_fallback_provider() -> Optional[WebSearchProvider]:
    """Native hermes scraper instance for the fallback layer.

    Module-level (monkeypatchable in tests). Prefers the registered plugin
    instance; falls back to direct construction so the layer still works when
    plugin discovery hasn't run (subprocess/delegate contexts).
    """
    try:
        from agent.web_search_registry import get_provider

        prov = get_provider("hermes")
        if prov is not None and prov.supports_extract():
            return prov
    except Exception:  # noqa: BLE001
        pass
    try:
        from plugins.web.hermes.provider import HermesNativeWebSearchProvider

        return HermesNativeWebSearchProvider()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Hermes fallback provider unavailable: %s", exc)
        return None
