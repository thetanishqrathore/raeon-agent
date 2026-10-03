"""End-to-end: web_search_tool dispatches through the resilient router (R6)."""

import json

import pytest

from agent.web_search_provider import WebSearchProvider
from agent.web_search_registry import register_provider


class _Stub(WebSearchProvider):
    def __init__(self, name, url):
        self._n, self._url = name, url

    @property
    def name(self):
        return self._n

    def is_available(self):
        return True

    def supports_search(self):
        return True

    def search(self, query, limit=5):
        return {"success": True, "data": {"web": [
            {"title": "MRO technical records", "url": self._url,
             "description": "aircraft maintenance records", "position": 1}]}}


@pytest.fixture
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))  # router state DB → tmp
    # Hermetic: on a dev box with a live search stack, plugin discovery
    # re-exports ~/.hermes/.env into the process env (SEARXNG_URL etc.), which
    # makes a real provider available and the router serve real network
    # results instead of falling through to the stub. Discovery is idempotent
    # (flag-guarded), so run it FIRST, then strip the leaked credentials.
    from hermes_cli.plugins import _ensure_plugins_discovered
    _ensure_plugins_discovered()
    for var in ("SEARXNG_URL", "BRAVE_SEARCH_API_KEY", "TAVILY_API_KEY",
                "GOOGLE_CSE_API_KEY", "GOOGLE_CSE_CX"):
        monkeypatch.delenv(var, raising=False)
    # Make the configured search backend "resilient".
    monkeypatch.setattr("tools.web_tools._get_search_backend", lambda: "resilient")
    yield


def test_web_search_routes_through_resilient(_env):
    from hermes_cli.plugins import _ensure_plugins_discovered
    _ensure_plugins_discovered()  # registers resilient + real providers (all unavailable)
    # Override ddgs with an available stub; real searxng/brave/etc. stay unavailable,
    # so the router falls through to our stub.
    register_provider(_Stub("ddgs", "https://stub-ddgs.com"))

    from tools.web_tools import web_search_tool
    out = web_search_tool("aircraft technical records manager MRO", 5)

    assert "https://stub-ddgs.com" in out          # served by the routed provider
    parsed = json.loads(out)
    # web_search_tool preserves the {success, data:{web:[...]}} shape.
    web = (parsed.get("data") or {}).get("web") or parsed.get("results") or []
    assert any("stub-ddgs.com" in (w.get("url", "")) for w in web)


def test_resilient_resolves_as_active_provider(_env):
    from hermes_cli.plugins import _ensure_plugins_discovered
    _ensure_plugins_discovered()
    from agent.web_search_registry import get_provider
    # The configured backend name resolves to the resilient meta-provider.
    assert get_provider("resilient") is not None
    assert get_provider("resilient").supports_search() is True
