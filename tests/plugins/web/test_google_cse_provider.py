"""Tests for the Google CSE search provider (R3) — httpx mocked, no network."""

from unittest import mock

import pytest

from plugins.web.google_cse.provider import GoogleCSEWebSearchProvider


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._p = payload or {}

    def json(self):
        return self._p

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            raise httpx.HTTPStatusError("err", request=None, response=self)


@pytest.fixture
def prov():
    return GoogleCSEWebSearchProvider()


class TestAvailability:
    def test_unavailable_without_keys(self, prov, monkeypatch):
        monkeypatch.delenv("GOOGLE_CSE_API_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_CSE_CX", raising=False)
        assert prov.is_available() is False

    def test_available_with_both_keys(self, prov, monkeypatch):
        monkeypatch.setenv("GOOGLE_CSE_API_KEY", "k")
        monkeypatch.setenv("GOOGLE_CSE_CX", "cx")
        assert prov.is_available() is True

    def test_search_without_keys_errors(self, prov, monkeypatch):
        monkeypatch.delenv("GOOGLE_CSE_API_KEY", raising=False)
        out = prov.search("q")
        assert out["success"] is False and "not set" in out["error"]


class TestSearch:
    def test_success_maps_items(self, prov, monkeypatch):
        monkeypatch.setenv("GOOGLE_CSE_API_KEY", "k")
        monkeypatch.setenv("GOOGLE_CSE_CX", "cx")
        payload = {"items": [
            {"title": "MRO Co", "link": "https://mro.com", "snippet": "technical records"},
            {"title": "Air Co", "link": "https://air.com", "snippet": "maintenance"},
        ]}
        with mock.patch("httpx.get", return_value=_Resp(200, payload)):
            out = prov.search("aircraft technical records", 5)
        assert out["success"] is True
        web = out["data"]["web"]
        assert web[0] == {"title": "MRO Co", "url": "https://mro.com",
                          "description": "technical records", "position": 1}
        assert web[1]["position"] == 2

    def test_quota_429_surfaces_quota_error(self, prov, monkeypatch):
        monkeypatch.setenv("GOOGLE_CSE_API_KEY", "k")
        monkeypatch.setenv("GOOGLE_CSE_CX", "cx")
        payload = {"error": {"code": 429, "errors": [{"reason": "dailyLimitExceeded"}]}}
        with mock.patch("httpx.get", return_value=_Resp(429, payload)):
            out = prov.search("q")
        assert out["success"] is False
        # Router must read this as quota, not a generic error.
        from plugins.web.resilient.provider import classify_result, QUOTA_EXCEEDED
        assert classify_result(out) == QUOTA_EXCEEDED

    def test_empty_items(self, prov, monkeypatch):
        monkeypatch.setenv("GOOGLE_CSE_API_KEY", "k")
        monkeypatch.setenv("GOOGLE_CSE_CX", "cx")
        with mock.patch("httpx.get", return_value=_Resp(200, {})):
            out = prov.search("q")
        assert out["success"] is True and out["data"]["web"] == []


class TestRegistration:
    def test_registers_via_discovery(self):
        from hermes_cli.plugins import _ensure_plugins_discovered
        from agent.web_search_registry import get_provider
        _ensure_plugins_discovered()
        assert get_provider("google-cse") is not None
