"""Tests for the Crawl4AI extract provider (plugins/web/crawl4ai).

Unit tests mock HTTP at the httpx transport layer (no network). The
integration smoke at the bottom is marked ``integration`` and additionally
skips itself when no live service is reachable, so the default test run
(``-m 'not integration'``) never touches the network.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional

import httpx
import pytest

import plugins.web.crawl4ai.provider as c4a
from plugins.web.crawl4ai.provider import (
    Crawl4AIWebSearchProvider,
    _build_crawl_payload,
    _pick_content,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _crawl_response(
    *,
    url: str = "https://site.test/page",
    raw_md: str = "raw " * 100,
    fit_md: str = "# Title\n\nfit content " * 20,
    title: str = "Page Title",
    status_code: int = 200,
    success: bool = True,
    redirected_url: Optional[str] = None,
    cleaned_html: str = "<main>hi</main>",
) -> Dict[str, Any]:
    return {
        "success": True,
        "results": [
            {
                "url": url,
                "redirected_url": redirected_url,
                "status_code": status_code,
                "success": success,
                "error_message": "" if success else "crawl exploded",
                "metadata": {"title": title},
                "cleaned_html": cleaned_html,
                "markdown": {
                    "raw_markdown": raw_md,
                    "fit_markdown": fit_md,
                    "markdown_with_citations": "",
                    "references_markdown": "",
                },
            }
        ],
    }


class _Recorder:
    """MockTransport handler that records requests and plays back responses."""

    def __init__(self, responses: List[httpx.Response]):
        self.requests: List[httpx.Request] = []
        self._responses = list(responses)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self._responses:
            return httpx.Response(500, json={"error": "no scripted response"})
        return self._responses.pop(0)


async def _offline_is_safe_url(url: str) -> bool:
    """DNS-free stand-in for the SSRF guard in unit tests.

    Blocks literal private/link-local/loopback IPs (mirroring the real
    guard's IP rules) and allows fake ``*.test`` hostnames that would
    otherwise fail DNS resolution and be blocked. The REAL guard's IP
    verdicts are pinned separately in
    :func:`test_real_url_safety_blocks_metadata_and_private`.
    """
    import ipaddress
    from urllib.parse import urlparse

    host = urlparse(url).hostname or ""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True  # non-IP test hostname — allow without DNS
    return not (ip.is_private or ip.is_link_local or ip.is_loopback)


@pytest.fixture()
def provider(monkeypatch):
    monkeypatch.setattr(c4a, "_crawl4ai_url", lambda: "http://c4a.test:11235")
    monkeypatch.setattr(c4a, "_crawl4ai_token", lambda: "tok-123")
    monkeypatch.setattr(c4a, "_extract_fallback_enabled", lambda: False)
    monkeypatch.setattr(c4a, "async_is_safe_url", _offline_is_safe_url)
    return Crawl4AIWebSearchProvider()


def _install_transport(monkeypatch, recorder: _Recorder) -> None:
    def factory(timeout_s: float) -> httpx.AsyncClient:
        headers = {"Content-Type": "application/json"}
        token = c4a._crawl4ai_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return httpx.AsyncClient(
            base_url=c4a._crawl4ai_url(),
            headers=headers,
            transport=httpx.MockTransport(recorder),
        )

    monkeypatch.setattr(c4a, "_client_factory", factory)


def _extract(provider: Crawl4AIWebSearchProvider, urls: List[str], **kw) -> List[Dict[str, Any]]:
    return asyncio.run(provider.extract(urls, **kw))


# ---------------------------------------------------------------------------
# Interface surface
# ---------------------------------------------------------------------------

def test_capabilities(provider):
    assert provider.name == "crawl4ai"
    assert provider.supports_extract() is True
    assert provider.supports_search() is False
    assert provider.is_available() is True


def test_unavailable_without_url(monkeypatch):
    monkeypatch.setattr(c4a, "_crawl4ai_url", lambda: "")
    assert Crawl4AIWebSearchProvider().is_available() is False


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_extract_happy_path(provider, monkeypatch):
    rec = _Recorder([httpx.Response(200, json=_crawl_response())])
    _install_transport(monkeypatch, rec)

    results = _extract(provider, ["https://site.test/page"])
    assert len(results) == 1
    r = results[0]
    assert r["title"] == "Page Title"
    assert r["content"].startswith("# Title")          # fit markdown chosen
    assert r["raw_content"].startswith("raw ")
    assert r.get("error") is None
    assert r["metadata"]["engine"] == "crawl4ai"
    assert r["metadata"]["statusCode"] == 200

    # Exactly one POST /crawl with bearer auth and our URL in the payload.
    assert len(rec.requests) == 1
    req = rec.requests[0]
    assert req.url.path == "/crawl"
    assert req.headers["Authorization"] == "Bearer tok-123"
    body = json.loads(req.content)
    assert body["urls"] == ["https://site.test/page"]
    assert body["crawler_config"]["type"] == "CrawlerRunConfig"


def test_html_format_uses_cleaned_html(provider, monkeypatch):
    rec = _Recorder([httpx.Response(200, json=_crawl_response())])
    _install_transport(monkeypatch, rec)
    r = _extract(provider, ["https://site.test/page"], format="html")[0]
    assert r["content"] == "<main>hi</main>"


def test_query_switches_to_bm25_filter(provider, monkeypatch):
    rec = _Recorder([httpx.Response(200, json=_crawl_response())])
    _install_transport(monkeypatch, rec)
    _extract(provider, ["https://site.test/page"], query="pricing tiers")
    body = json.loads(rec.requests[0].content)
    gen = body["crawler_config"]["params"]["markdown_generator"]
    cf = gen["params"]["content_filter"]
    assert cf["type"] == "BM25ContentFilter"
    assert cf["params"]["user_query"] == "pricing tiers"


def test_schema_degrades_with_note(provider, monkeypatch):
    rec = _Recorder([httpx.Response(200, json=_crawl_response())])
    _install_transport(monkeypatch, rec)
    r = _extract(provider, ["https://site.test/page"], schema={"type": "object"})[0]
    assert r["content"]                      # markdown still returned
    assert "schema extraction" in r["extraction_note"]


# ---------------------------------------------------------------------------
# Content selection
# ---------------------------------------------------------------------------

def test_pick_content_falls_back_to_raw_when_fit_overpruned():
    result = {
        "markdown": {"raw_markdown": "x" * 5000, "fit_markdown": "tiny"},
        "cleaned_html": "",
    }
    content, raw = _pick_content(result, None)
    assert content == "x" * 5000
    assert raw == "x" * 5000


def test_pick_content_accepts_plain_string_markdown():
    content, _ = _pick_content({"markdown": "plain md"}, None)
    assert content == "plain md"


def test_payload_defaults_use_pruning_filter():
    body = _build_crawl_payload(
        "https://a.test", format=None, query=None, page_timeout_ms=30000,
    )
    cf = body["crawler_config"]["params"]["markdown_generator"]["params"]["content_filter"]
    assert cf["type"] == "PruningContentFilter"
    assert body["crawler_config"]["params"]["page_timeout"] == 30000


# ---------------------------------------------------------------------------
# SSRF / policy guards — must block BEFORE any HTTP request leaves
# ---------------------------------------------------------------------------

def test_ssrf_private_ip_blocked_before_request(provider, monkeypatch):
    rec = _Recorder([httpx.Response(200, json=_crawl_response())])
    _install_transport(monkeypatch, rec)

    results = _extract(provider, [
        "http://169.254.169.254/latest/meta-data/",
        "http://127.0.0.1:8888/",
        "http://10.0.0.7/internal",
    ])
    assert len(results) == 3
    for r in results:
        assert "private or internal network address" in r["error"]
        assert r["content"] == ""
    assert rec.requests == []            # nothing left the process


def test_real_url_safety_blocks_metadata_and_private():
    """Pin the REAL guard's verdicts on literal IPs (no DNS involved)."""
    from tools.url_safety import is_safe_url

    for url in (
        "http://169.254.169.254/latest/meta-data/",   # cloud metadata
        "http://169.254.170.2/v2/credentials",        # ECS task creds
        "http://127.0.0.1:8888/",                     # loopback (searxng)
        "http://10.0.0.7/internal",                   # RFC1918
        "http://192.168.1.1/",                        # RFC1918
        "http://100.64.0.1/",                         # CGNAT
        "http://metadata.google.internal/computeMetadata/v1/",
    ):
        assert is_safe_url(url) is False, url
    assert is_safe_url("https://example.com/") is True


def test_policy_block_before_request_and_no_fallback(provider, monkeypatch):
    rec = _Recorder([httpx.Response(200, json=_crawl_response())])
    _install_transport(monkeypatch, rec)
    monkeypatch.setattr(c4a, "check_website_access", lambda url: {
        "host": "evil.test", "rule": "evil.test", "source": "config",
        "message": "Access to evil.test is blocked by policy",
    })

    fallback_calls: List[List[str]] = []

    class _FB:
        def supports_extract(self):
            return True

        async def extract(self, urls, **kw):
            fallback_calls.append(list(urls))
            return [{"url": u, "title": "", "content": "should not appear"} for u in urls]

    monkeypatch.setattr(c4a, "_extract_fallback_enabled", lambda: True)
    monkeypatch.setattr(c4a, "_get_fallback_provider", lambda: _FB())

    r = _extract(provider, ["https://evil.test/x"])[0]
    assert r["blocked_by_policy"]["host"] == "evil.test"
    assert r["content"] == ""
    assert rec.requests == []            # guard fired pre-request
    assert fallback_calls == []          # blocks are never retried


def test_redirect_to_private_address_dropped(provider, monkeypatch):
    resp = _crawl_response(redirected_url="http://127.0.0.1:6379/")
    rec = _Recorder([httpx.Response(200, json=resp)])
    _install_transport(monkeypatch, rec)

    r = _extract(provider, ["https://site.test/page"])[0]
    assert "private or internal network address" in r["error"]
    assert r["content"] == ""


# ---------------------------------------------------------------------------
# Typed errors
# ---------------------------------------------------------------------------

def test_auth_error_message(provider, monkeypatch):
    rec = _Recorder([httpx.Response(401, json={"detail": "nope"})])
    _install_transport(monkeypatch, rec)
    r = _extract(provider, ["https://site.test/page"])[0]
    assert "Crawl4AIAuthError" in r["error"]
    assert "CRAWL4AI_API_TOKEN" in r["error"]


def test_server_500_surfaces_reason(provider, monkeypatch):
    rec = _Recorder([httpx.Response(500, json={
        "error": "Crawl request failed: Blocked by anti-bot protection: DataDome captcha",
    })])
    _install_transport(monkeypatch, rec)
    r = _extract(provider, ["https://site.test/page"])[0]
    assert "Crawl4AICrawlError" in r["error"]
    assert "DataDome" in r["error"]


def test_timeout_is_typed(provider, monkeypatch):
    def raise_timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("boom", request=request)

    _install_transport(monkeypatch, _Recorder([]))
    monkeypatch.setattr(
        c4a, "_client_factory",
        lambda t: httpx.AsyncClient(
            base_url="http://c4a.test:11235",
            transport=httpx.MockTransport(raise_timeout),
        ),
    )
    r = _extract(provider, ["https://site.test/page"])[0]
    assert "Crawl4AITimeoutError" in r["error"]


def test_connection_error_is_typed(provider, monkeypatch):
    def raise_conn(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    monkeypatch.setattr(
        c4a, "_client_factory",
        lambda t: httpx.AsyncClient(
            base_url="http://c4a.test:11235",
            transport=httpx.MockTransport(raise_conn),
        ),
    )
    r = _extract(provider, ["https://site.test/page"])[0]
    assert "Crawl4AIUnavailableError" in r["error"]


def test_per_url_isolation(provider, monkeypatch):
    """One failing URL must not poison the other."""
    ok = _crawl_response(url="https://ok.test/")
    rec = _Recorder([
        httpx.Response(500, json={"error": "boom"}),
        httpx.Response(200, json=ok),
    ])
    _install_transport(monkeypatch, rec)
    results = _extract(provider, ["https://bad.test/", "https://ok.test/"])
    assert len(results) == 2
    # Order preserved: index 0 errored, index 1 succeeded (semaphore(5) keeps
    # request order for two URLs, but assert by URL to stay robust).
    by_url = {r["url"]: r for r in results}
    assert "error" in by_url["https://bad.test/"]
    assert by_url["https://ok.test/"]["content"]


# ---------------------------------------------------------------------------
# Fallback layering
# ---------------------------------------------------------------------------

class _StubFallback:
    def __init__(self, results: List[Dict[str, Any]]):
        self._results = results
        self.calls: List[Dict[str, Any]] = []

    def supports_extract(self):
        return True

    async def extract(self, urls: List[str], **kw) -> List[Dict[str, Any]]:
        self.calls.append({"urls": list(urls), "kwargs": kw})
        return self._results


def test_fallback_on_engine_failure(provider, monkeypatch):
    rec = _Recorder([httpx.Response(500, json={"error": "bot wall"})])
    _install_transport(monkeypatch, rec)
    fb = _StubFallback([{
        "url": "https://site.test/page", "title": "Native",
        "content": "native content " * 30, "metadata": {},
    }])
    monkeypatch.setattr(c4a, "_extract_fallback_enabled", lambda: True)
    monkeypatch.setattr(c4a, "_get_fallback_provider", lambda: fb)

    r = _extract(provider, ["https://site.test/page"], format="markdown")[0]
    assert r["title"] == "Native"
    assert r["metadata"]["engine"] == "hermes-fallback"
    assert "native hermes scraper fallback" in r["extraction_note"]
    assert "bot wall" in r["extraction_note"]
    assert fb.calls[0]["urls"] == ["https://site.test/page"]
    assert fb.calls[0]["kwargs"].get("format") == "markdown"


def test_fallback_disabled_keeps_typed_error(provider, monkeypatch):
    rec = _Recorder([httpx.Response(500, json={"error": "bot wall"})])
    _install_transport(monkeypatch, rec)
    called = []
    monkeypatch.setattr(c4a, "_extract_fallback_enabled", lambda: False)
    monkeypatch.setattr(c4a, "_get_fallback_provider", lambda: called.append(1))

    r = _extract(provider, ["https://site.test/page"])[0]
    assert "Crawl4AICrawlError" in r["error"]
    assert called == []                       # provider never even resolved
    assert "_crawl4ai_failed" not in r        # internal marker stripped


def test_fallback_short_content_flagged(provider, monkeypatch):
    rec = _Recorder([httpx.Response(500, json={"error": "DataDome captcha"})])
    _install_transport(monkeypatch, rec)
    fb = _StubFallback([{
        "url": "https://site.test/page", "title": "",
        "content": "Please enable JS", "metadata": {},
    }])
    monkeypatch.setattr(c4a, "_extract_fallback_enabled", lambda: True)
    monkeypatch.setattr(c4a, "_get_fallback_provider", lambda: fb)

    r = _extract(provider, ["https://site.test/page"])[0]
    assert "likely bot-protected" in r["extraction_note"]


def test_both_engines_failed_merges_reasons(provider, monkeypatch):
    rec = _Recorder([httpx.Response(500, json={"error": "bot wall"})])
    _install_transport(monkeypatch, rec)
    fb = _StubFallback([{
        "url": "https://site.test/page", "title": "", "content": "",
        "error": "scrape failed: 403",
    }])
    monkeypatch.setattr(c4a, "_extract_fallback_enabled", lambda: True)
    monkeypatch.setattr(c4a, "_get_fallback_provider", lambda: fb)

    r = _extract(provider, ["https://site.test/page"])[0]
    assert "crawl4ai:" in r["error"]
    assert "hermes fallback:" in r["error"]
    assert "403" in r["error"]


def test_fallback_only_retries_failed_urls(provider, monkeypatch):
    ok = _crawl_response(url="https://ok.test/")
    rec = _Recorder([
        httpx.Response(200, json=ok),
        httpx.Response(500, json={"error": "boom"}),
    ])
    _install_transport(monkeypatch, rec)
    fb = _StubFallback([{
        "url": "https://bad.test/", "title": "N", "content": "from native " * 30,
        "metadata": {},
    }])
    monkeypatch.setattr(c4a, "_extract_fallback_enabled", lambda: True)
    monkeypatch.setattr(c4a, "_get_fallback_provider", lambda: fb)

    results = _extract(provider, ["https://ok.test/", "https://bad.test/"])
    assert fb.calls[0]["urls"] == ["https://bad.test/"]
    by_url = {r["url"]: r for r in results}
    assert by_url["https://ok.test/"]["metadata"]["engine"] == "crawl4ai"
    assert by_url["https://bad.test/"]["metadata"]["engine"] == "hermes-fallback"


# ---------------------------------------------------------------------------
# web_tools wiring
# ---------------------------------------------------------------------------

def test_backend_available_wiring(monkeypatch):
    import tools.web_tools as wt

    monkeypatch.setattr(wt, "_env_value", lambda name: (
        "http://127.0.0.1:11235" if name == "CRAWL4AI_URL" else ""
    ))
    assert wt._is_backend_available("crawl4ai") is True
    monkeypatch.setattr(wt, "_env_value", lambda name: "")
    assert wt._is_backend_available("crawl4ai") is False


def test_extract_backend_selection(monkeypatch):
    import tools.web_tools as wt

    monkeypatch.setattr(wt, "_load_web_config", lambda: {
        "backend": "brave-free", "extract_backend": "crawl4ai",
    })
    monkeypatch.setattr(wt, "_env_value", lambda name: (
        "http://127.0.0.1:11235" if name == "CRAWL4AI_URL" else ""
    ))
    assert wt._get_extract_backend() == "crawl4ai"


def test_check_web_api_key_with_crawl4ai_backend(monkeypatch):
    import tools.web_tools as wt

    monkeypatch.setattr(wt, "_load_web_config", lambda: {"backend": "crawl4ai"})
    monkeypatch.setattr(wt, "_env_value", lambda name: (
        "http://127.0.0.1:11235" if name == "CRAWL4AI_URL" else ""
    ))
    assert wt.check_web_api_key() is True


def test_requires_env_lists_crawl4ai():
    import tools.web_tools as wt

    env = wt._web_requires_env()
    assert "CRAWL4AI_URL" in env
    assert "CRAWL4AI_API_TOKEN" in env


# ---------------------------------------------------------------------------
# Integration smoke (skipped unless a live service is reachable)
# ---------------------------------------------------------------------------

# Captured at collection time — the autouse hermetic-environment fixture
# strips env vars and redirects HERMES_HOME before each test body runs, so
# the live service coordinates must be resolved *now* and re-injected.
_LIVE_URL = ""
_LIVE_TOKEN = ""
try:
    _info = c4a.check_crawl4ai_service(timeout_s=3.0)
    if _info["configured"] and _info["reachable"]:
        _LIVE_URL = _info["url"]
        _LIVE_TOKEN = c4a._crawl4ai_token()
except Exception:  # noqa: BLE001
    pass


@pytest.mark.integration
@pytest.mark.skipif(not _LIVE_URL, reason="no live Crawl4AI service")
def test_live_extract_smoke(monkeypatch):
    monkeypatch.setattr(c4a, "_crawl4ai_url", lambda: _LIVE_URL)
    monkeypatch.setattr(c4a, "_crawl4ai_token", lambda: _LIVE_TOKEN)
    monkeypatch.setattr(c4a, "_extract_fallback_enabled", lambda: False)
    provider = Crawl4AIWebSearchProvider()
    r = asyncio.run(provider.extract(["https://example.com"]))[0]
    assert r.get("error") is None
    assert "Example Domain" in (r.get("content") or "")
    assert r["metadata"]["engine"] == "crawl4ai"
