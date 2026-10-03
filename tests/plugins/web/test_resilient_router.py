"""Tests for the resilient search router (R2) — stub providers, no network."""

import pytest

from agent.web_search_provider import WebSearchProvider
from agent.web_search_registry import register_provider, _reset_for_tests
from plugins.web.resilient.provider import (
    ResilientSearchProvider, classify_error_text, classify_result,
    RATE_LIMITED, QUOTA_EXCEEDED, AUTH_ERROR, UNAVAILABLE, OK, EMPTY,
)


def _ok(url="https://a.com", title="t"):
    return {"success": True, "data": {"web": [
        {"title": title, "url": url, "description": "d", "position": 1}]}}


def _fail(error):
    return {"success": False, "error": error}


def _empty():
    return {"success": True, "data": {"web": []}}


class Stub(WebSearchProvider):
    def __init__(self, name, behavior, available=True):
        self._n = name
        self._b = behavior
        self._avail = available
        self.calls = 0

    @property
    def name(self):
        return self._n

    def is_available(self):
        return self._avail

    def supports_search(self):
        return True

    def search(self, query, limit=5):
        self.calls += 1
        b = self._b
        if callable(b):
            return b(query, limit)
        if isinstance(b, BaseException):
            raise b
        return b


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))  # router state DB → tmp
    _reset_for_tests()
    yield
    _reset_for_tests()


def _router(tiers, merge=False, empty_cooldown=30.0, cache_ttl=86400.0):
    p = ResilientSearchProvider()
    p._cfg = lambda: {  # type: ignore
        "tiers": tiers, "merge_uncapped": merge,
        "rate_limit_cooldown_seconds": 30.0, "max_cooldown_seconds": 900.0,
        "auth_error_disable_seconds": 86400.0, "empty_cooldown_s": empty_cooldown,
        "cache_ttl_s": cache_ttl, "cache_max_entries": 512,
    }
    return p


class TestClassify:
    @pytest.mark.parametrize("text,expected", [
        ("HTTP 429 rate limit exceeded", QUOTA_EXCEEDED),  # 'exceeded' → quota wins
        ("429 too many requests", RATE_LIMITED),
        ("monthly limit reached", QUOTA_EXCEEDED),
        ("BRAVE_SEARCH_API_KEY is not set", AUTH_ERROR),
        ("401 unauthorized", AUTH_ERROR),
        ("connection timed out", UNAVAILABLE),
        ("something weird", UNAVAILABLE),
    ])
    def test_error_text(self, text, expected):
        assert classify_error_text(text) == expected

    def test_result(self):
        assert classify_result(_ok()) == OK
        assert classify_result(_empty()) == EMPTY
        assert classify_result(_fail("quota exceeded")) == QUOTA_EXCEEDED
        assert classify_result("nonsense") == UNAVAILABLE


class TestRouting:
    def test_primary_ok_serves_and_no_fallthrough(self):
        a, b = Stub("searxng", _ok("https://a.com")), Stub("ddgs", _ok("https://b.com"))
        register_provider(a)
        register_provider(b)
        r = _router([{"name": "searxng", "daily_cap": 0, "monthly_cap": 0},
                     {"name": "ddgs", "daily_cap": 0, "monthly_cap": 0}])
        out = r.search("q", 5)
        assert out["success"] and out["router"]["served_by"] == "searxng"
        assert a.calls == 1 and b.calls == 0

    def test_rate_limited_falls_through(self):
        a = Stub("searxng", _fail("429 rate limit"))
        b = Stub("ddgs", _ok("https://b.com"))
        register_provider(a)
        register_provider(b)
        r = _router([{"name": "searxng", "daily_cap": 0, "monthly_cap": 0},
                     {"name": "ddgs", "daily_cap": 0, "monthly_cap": 0}])
        out = r.search("q", 5)
        assert out["router"]["served_by"] == "ddgs"
        assert {x["provider"]: x["outcome"] for x in r.last_route["attempted"]}["searxng"] == RATE_LIMITED

    def test_exception_is_caught_and_rerouted(self):
        register_provider(Stub("searxng", RuntimeError("connection refused")))
        register_provider(Stub("ddgs", _ok()))
        r = _router([{"name": "searxng", "daily_cap": 0, "monthly_cap": 0},
                     {"name": "ddgs", "daily_cap": 0, "monthly_cap": 0}])
        assert r.search("q", 5)["router"]["served_by"] == "ddgs"

    def test_all_fail_returns_explicit_error_not_silent_empty(self):
        register_provider(Stub("ddgs", _fail("429 rate limit")))
        r = _router([{"name": "ddgs", "daily_cap": 0, "monthly_cap": 0}])
        out = r.search("q", 5)
        assert out["success"] is False
        assert "unavailable or quota-exhausted" in out["error"]
        assert out["data"]["web"] == []

    def test_empty_result_is_not_success(self):
        register_provider(Stub("ddgs", _empty()))
        r = _router([{"name": "ddgs", "daily_cap": 0, "monthly_cap": 0}])
        out = r.search("q", 5)
        assert out["success"] is False  # EMPTY → treated as failure, fell through to nothing

    def test_absent_provider_skipped(self):
        register_provider(Stub("ddgs", _ok()))
        r = _router([{"name": "searxng", "daily_cap": 0, "monthly_cap": 0},  # not registered
                     {"name": "ddgs", "daily_cap": 0, "monthly_cap": 0}])
        assert r.search("q", 5)["router"]["served_by"] == "ddgs"

    def test_unconfigured_provider_skipped(self):
        register_provider(Stub("brave-free", _ok(), available=False))
        register_provider(Stub("ddgs", _ok("https://b.com")))
        r = _router([{"name": "brave-free", "daily_cap": 0, "monthly_cap": 2000},
                     {"name": "ddgs", "daily_cap": 0, "monthly_cap": 0}])
        out = r.search("q", 5)
        assert out["router"]["served_by"] == "ddgs"


class TestQuotaAndCircuit:
    def test_daily_cap_exhaustion_skips_provider(self):
        a = Stub("google-cse", _ok("https://a.com"))
        b = Stub("ddgs", _ok("https://b.com"))
        register_provider(a)
        register_provider(b)
        # google-cse first with daily cap 1
        r = _router([{"name": "google-cse", "daily_cap": 1, "monthly_cap": 0},
                     {"name": "ddgs", "daily_cap": 0, "monthly_cap": 0}])
        assert r.search("q1", 5)["router"]["served_by"] == "google-cse"   # uses its 1 quota
        assert r.search("q2", 5)["router"]["served_by"] == "ddgs"          # cap hit → fall through
        assert a.calls == 1

    def test_rate_limit_opens_circuit_for_next_query(self):
        a = Stub("searxng", _fail("429 rate limit"))
        b = Stub("ddgs", _ok())
        register_provider(a)
        register_provider(b)
        r = _router([{"name": "searxng", "daily_cap": 0, "monthly_cap": 0},
                     {"name": "ddgs", "daily_cap": 0, "monthly_cap": 0}])
        r.search("q1", 5)              # searxng fails → circuit opens
        r.search("q2", 5)              # searxng skipped (circuit), ddgs serves
        assert a.calls == 1            # not retried while circuit open

    def test_empty_cooldown_is_flat_not_escalating(self):
        # A burst of zero-result queries must NOT open the circuit like real
        # failures do: cooldown stays at the flat empty_cooldown_s window.
        a = Stub("searxng", _empty())
        register_provider(a)
        register_provider(Stub("ddgs", _empty()))
        r = _router([{"name": "searxng", "daily_cap": 0, "monthly_cap": 0},
                     {"name": "ddgs", "daily_cap": 0, "monthly_cap": 0}])
        state = r._ensure_state(r._cfg())
        for i in range(6):
            r.search(f"rare query {i}", 5)
        # Flat window (≤30s) — six REAL failures would have escalated to 900s.
        assert state.cooldown_remaining("searxng") <= 30.0
        assert state.snapshot()["searxng"]["consecutive_failures"] == 0

    def test_empty_cooldown_zero_keeps_provider_hot(self):
        a = Stub("searxng", _empty())
        b = Stub("ddgs", _ok())
        register_provider(a)
        register_provider(b)
        r = _router([{"name": "searxng", "daily_cap": 0, "monthly_cap": 0},
                     {"name": "ddgs", "daily_cap": 0, "monthly_cap": 0}],
                    empty_cooldown=0.0)
        r.search("q1", 5)
        r.search("q2", 5)
        assert a.calls == 2  # no circuit ever opened on EMPTY

    def test_auth_error_does_not_consume_quota(self):
        # brave returns auth error; quota counter must not decrement.
        a = Stub("brave-free", _fail("API key is not set"))
        b = Stub("ddgs", _ok())
        register_provider(a)
        register_provider(b)
        r = _router([{"name": "brave-free", "daily_cap": 0, "monthly_cap": 2000},
                     {"name": "ddgs", "daily_cap": 0, "monthly_cap": 0}])
        r.search("q", 5)
        day, month = r._ensure_state(r._cfg()).usage("brave-free")
        assert month == 0


class TestCacheAndMerge:
    def test_cache_avoids_second_call(self):
        a = Stub("ddgs", _ok())
        register_provider(a)
        r = _router([{"name": "ddgs", "daily_cap": 0, "monthly_cap": 0}])
        r.search("same", 5)
        r.search("same", 5)
        assert a.calls == 1  # second served from cache

    def test_cache_is_bounded(self):
        from plugins.web.resilient.provider import _CACHE_MAX
        register_provider(Stub("ddgs", _ok()))
        r = _router([{"name": "ddgs", "daily_cap": 0, "monthly_cap": 0}])
        for i in range(_CACHE_MAX + 10):
            r.search(f"q{i}", 5)
        assert len(r._cache) <= _CACHE_MAX  # long-lived process must not grow forever

    def test_memory_cache_serves_deep_copies(self):
        # Reference-poisoning regression (July 2026 audit): mutating a served
        # result must not corrupt what the next identical query receives.
        register_provider(Stub("ddgs", _ok("https://a.com", "clean")))
        r = _router([{"name": "ddgs", "daily_cap": 0, "monthly_cap": 0}])
        first = r.search("q", 5)
        first["data"]["web"][0]["title"] = "POISONED"
        first["data"]["web"].append({"junk": True})
        second = r.search("q", 5)
        assert second["data"]["web"][0]["title"] == "clean"
        assert len(second["data"]["web"]) == 1

    def test_persistent_cache_survives_process_restart(self):
        # Same query in a FRESH provider instance (same HERMES_HOME DB) is
        # served from the persistent cache — zero provider calls, zero quota.
        a = Stub("brave-free", _ok("https://a.com"))
        register_provider(a)
        tiers = [{"name": "brave-free", "daily_cap": 0, "monthly_cap": 2000}]
        r1 = _router(tiers)
        out1 = r1.search("daily discovery query", 5)
        assert out1["router"]["served_by"] == "brave-free" and a.calls == 1

        r2 = _router(tiers)  # simulates the next day's process
        out2 = r2.search("daily discovery query", 5)
        assert a.calls == 1  # no second provider hit
        assert out2["data"]["web"] == out1["data"]["web"]
        assert out2["router"]["cache"] == "persistent"
        assert r2.last_route["cache"] == "persistent"
        assert r2.cache_hits["persistent"] == 1
        # Quota untouched by the hit: still exactly one recorded use.
        day, month = r2._ensure_state(r2._cfg()).usage("brave-free")
        assert (day, month) == (1, 1)

    def test_persistent_cache_hit_is_copy_isolated(self):
        register_provider(Stub("ddgs", _ok("https://a.com", "clean")))
        tiers = [{"name": "ddgs", "daily_cap": 0, "monthly_cap": 0}]
        _router(tiers).search("q", 5)
        r2 = _router(tiers)
        hit = r2.search("q", 5)
        hit["data"]["web"][0]["title"] = "POISONED"
        r3 = _router(tiers)
        assert r3.search("q", 5)["data"]["web"][0]["title"] == "clean"

    def test_persistent_cache_ttl_zero_disables(self):
        a = Stub("ddgs", _ok())
        register_provider(a)
        tiers = [{"name": "ddgs", "daily_cap": 0, "monthly_cap": 0}]
        _router(tiers, cache_ttl=0.0).search("q", 5)
        _router(tiers, cache_ttl=0.0).search("q", 5)
        assert a.calls == 2  # nothing persisted between instances

    def test_failures_and_empties_are_never_cached(self):
        a = Stub("ddgs", _empty())
        register_provider(a)
        tiers = [{"name": "ddgs", "daily_cap": 0, "monthly_cap": 0}]
        _router(tiers, empty_cooldown=0.0).search("q", 5)
        _router(tiers, empty_cooldown=0.0).search("q", 5)
        assert a.calls == 2  # second run re-queried; no poisoned cache entry

    def test_merge_uncapped_fuses_results(self):
        register_provider(Stub("searxng", _ok("https://x.com", "X")))
        register_provider(Stub("ddgs", _ok("https://y.com", "Y")))
        r = _router([{"name": "searxng", "daily_cap": 0, "monthly_cap": 0},
                     {"name": "ddgs", "daily_cap": 0, "monthly_cap": 0}], merge=True)
        out = r.search("q", 5)
        assert out["success"] and out["router"]["served_by"] == "merge:uncapped"
        urls = {w["url"] for w in out["data"]["web"]}
        assert urls == {"https://x.com", "https://y.com"}
