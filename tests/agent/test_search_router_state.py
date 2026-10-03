"""Tests for the resilient-search-router state store (R1)."""

import time as _time

from agent.search_router_state import (
    RouterState, OK, RATE_LIMITED, QUOTA_EXCEEDED, AUTH_ERROR, UNAVAILABLE, EMPTY,
)

_BASE = 1_700_000_000.0  # 2023-11-14 ~22:13 UTC (mid-day, mid-month)


def _state(tmp_path, clock, **kw):
    return RouterState(
        db_path=str(tmp_path / "rs.db"),
        now=lambda: clock[0],
        rate_limit_cooldown_seconds=30.0,
        max_cooldown_seconds=900.0,
        auth_error_disable_seconds=86400.0,
        **kw,
    )


class TestQuota:
    def test_record_and_usage(self, tmp_path):
        s = _state(tmp_path, [_BASE])
        for _ in range(3):
            s.record_use("brave-free")
        assert s.usage("brave-free") == (3, 3)

    def test_remaining_with_caps(self, tmp_path):
        s = _state(tmp_path, [_BASE])
        for _ in range(5):
            s.record_use("google-cse")
        assert s.remaining("google-cse", daily_cap=100, monthly_cap=0) == 95
        # 0/None caps = unlimited
        assert s.remaining("ddgs", daily_cap=0, monthly_cap=None) == float("inf")

    def test_daily_rollover_resets_day_not_month(self, tmp_path):
        clock = [_BASE]
        s = _state(tmp_path, clock)
        s.record_use("brave-free")
        s.record_use("brave-free")
        assert s.usage("brave-free") == (2, 2)
        clock[0] = _BASE + 86400  # next UTC day
        assert s.usage("brave-free") == (0, 2)  # day reset, month kept

    def test_monthly_rollover_resets_both(self, tmp_path):
        clock = [_BASE]
        s = _state(tmp_path, clock)
        s.record_use("brave-free")
        clock[0] = _BASE + 32 * 86400  # next month
        assert s.usage("brave-free") == (0, 0)

    def test_quota_exhaustion_blocks_available(self, tmp_path):
        s = _state(tmp_path, [_BASE])
        s.record_use("google-cse")
        assert s.available("google-cse", daily_cap=1, monthly_cap=0) is False
        assert s.available("google-cse", daily_cap=2, monthly_cap=0) is True


class TestCircuit:
    def test_rate_limit_opens_circuit_then_recovers(self, tmp_path):
        clock = [_BASE]
        s = _state(tmp_path, clock)
        s.record_failure("ddgs", RATE_LIMITED)
        assert s.is_circuit_open("ddgs") is True
        # base cooldown 30s
        assert 29 <= s.cooldown_remaining("ddgs") <= 31
        clock[0] = _BASE + 31
        assert s.is_circuit_open("ddgs") is False

    def test_exponential_backoff_capped(self, tmp_path):
        clock = [_BASE]
        s = _state(tmp_path, clock)
        for _ in range(10):
            s.record_failure("ddgs", UNAVAILABLE)
        # 30 * 2^9 = 15360, capped at 900
        assert s.cooldown_remaining("ddgs") <= 900

    def test_auth_error_long_disable(self, tmp_path):
        s = _state(tmp_path, [_BASE])
        s.record_failure("brave-free", AUTH_ERROR)
        assert s.cooldown_remaining("brave-free") >= 86000

    def test_quota_exceeded_defers_to_midnight(self, tmp_path):
        s = _state(tmp_path, [_BASE])
        s.record_failure("google-cse", QUOTA_EXCEEDED)
        cd = s.cooldown_remaining("google-cse")
        assert 0 < cd <= 86400

    def test_success_resets(self, tmp_path):
        s = _state(tmp_path, [_BASE])
        s.record_failure("ddgs", RATE_LIMITED)
        s.record_success("ddgs")
        assert s.is_circuit_open("ddgs") is False
        assert s.cooldown_remaining("ddgs") == 0

    def test_available_combines_circuit_and_quota(self, tmp_path):
        clock = [_BASE]
        s = _state(tmp_path, clock)
        # healthy + quota → available
        assert s.available("brave-free", 0, 2000) is True
        # open circuit → unavailable even with quota
        s.record_failure("brave-free", RATE_LIMITED)
        assert s.available("brave-free", 0, 2000) is False


class TestEmptyCooldown:
    """EMPTY (zero results) is a healthy response — never escalating backoff."""

    def test_empty_uses_flat_short_cooldown(self, tmp_path):
        clock = [_BASE]
        s = _state(tmp_path, clock, empty_cooldown_s=30.0)
        s.record_failure("searxng", EMPTY)
        assert 29 <= s.cooldown_remaining("searxng") <= 31
        clock[0] = _BASE + 31
        assert s.is_circuit_open("searxng") is False

    def test_repeated_empties_never_escalate(self, tmp_path):
        clock = [_BASE]
        s = _state(tmp_path, clock, empty_cooldown_s=30.0)
        for i in range(10):  # a burst of zero-result queries
            clock[0] = _BASE + i * 40
            s.record_failure("searxng", EMPTY)
        # Still the flat 30s window — 10 real failures would sit at the 900 cap.
        assert s.cooldown_remaining("searxng") <= 30.0

    def test_empty_does_not_touch_failure_counter(self, tmp_path):
        clock = [_BASE]
        s = _state(tmp_path, clock, empty_cooldown_s=30.0)
        for _ in range(5):
            s.record_failure("searxng", EMPTY)
        # A real failure afterwards starts at the BASE backoff (n=1 → 30s),
        # not the escalated 30*2^5 it would get if EMPTY counted as failure.
        clock[0] = _BASE + 60
        s.record_failure("searxng", UNAVAILABLE)
        assert s.cooldown_remaining("searxng") <= 30.0
        assert s.snapshot()["searxng"]["consecutive_failures"] == 1

    def test_zero_disables_empty_cooldown(self, tmp_path):
        s = _state(tmp_path, [_BASE], empty_cooldown_s=0.0)
        s.record_failure("searxng", EMPTY)
        assert s.is_circuit_open("searxng") is False

    def test_empty_never_shortens_an_existing_real_cooldown(self, tmp_path):
        clock = [_BASE]
        s = _state(tmp_path, clock, empty_cooldown_s=30.0)
        for _ in range(5):  # real failures → escalated cooldown (480s)
            s.record_failure("searxng", UNAVAILABLE)
        before = s.cooldown_remaining("searxng")
        s.record_failure("searxng", EMPTY)
        assert s.cooldown_remaining("searxng") >= before - 1

    def test_empty_recorded_as_last_failure(self, tmp_path):
        s = _state(tmp_path, [_BASE], empty_cooldown_s=30.0)
        s.record_failure("searxng", EMPTY)
        assert s.snapshot()["searxng"]["last_failure"] == EMPTY


class TestSnapshot:
    def test_snapshot_shape(self, tmp_path):
        s = _state(tmp_path, [_BASE])
        s.record_use("ddgs")
        s.record_failure("brave-free", RATE_LIMITED)
        snap = s.snapshot()
        assert snap["ddgs"]["day_count"] == 1
        assert snap["brave-free"]["circuit_open"] is True


def _result(url="https://a.com"):
    return {"success": True, "data": {"web": [
        {"title": "t", "url": url, "description": "d", "position": 1}]}}


class TestSearchCache:
    """Persistent query→results cache (A7)."""

    def test_put_get_roundtrip(self, tmp_path):
        s = _state(tmp_path, [_BASE])
        s.cache_put("q", 5, _result())
        got = s.cache_get("q", 5)
        assert got == _result()

    def test_get_returns_deep_copies(self, tmp_path):
        s = _state(tmp_path, [_BASE])
        s.cache_put("q", 5, _result())
        first = s.cache_get("q", 5)
        first["data"]["web"][0]["title"] = "POISONED"
        first["data"]["web"].append({"junk": True})
        assert s.cache_get("q", 5) == _result()  # cache unaffected

    def test_put_snapshots_input(self, tmp_path):
        s = _state(tmp_path, [_BASE])
        res = _result()
        s.cache_put("q", 5, res)
        res["data"]["web"][0]["title"] = "MUTATED AFTER PUT"
        assert s.cache_get("q", 5) == _result()

    def test_ttl_expiry(self, tmp_path):
        clock = [_BASE]
        s = _state(tmp_path, clock, cache_ttl_s=100.0)
        s.cache_put("q", 5, _result())
        clock[0] = _BASE + 99
        assert s.cache_get("q", 5) is not None
        clock[0] = _BASE + 101
        assert s.cache_get("q", 5) is None
        assert s.cache_stats()["entries"] == 0  # expired row pruned on read

    def test_ttl_zero_disables_cache(self, tmp_path):
        s = _state(tmp_path, [_BASE], cache_ttl_s=0.0)
        s.cache_put("q", 5, _result())
        assert s.cache_get("q", 5) is None
        assert s.cache_stats()["entries"] == 0

    def test_only_successful_non_empty_results_cached(self, tmp_path):
        s = _state(tmp_path, [_BASE])
        s.cache_put("fail", 5, {"success": False, "error": "x", "data": {"web": []}})
        s.cache_put("empty", 5, {"success": True, "data": {"web": []}})
        s.cache_put("junk", 5, "not a dict")
        assert s.cache_stats()["entries"] == 0

    def test_max_entries_eviction_keeps_newest(self, tmp_path):
        clock = [_BASE]
        s = _state(tmp_path, clock, cache_max_entries=3)
        for i in range(5):
            clock[0] = _BASE + i
            s.cache_put(f"q{i}", 5, _result(f"https://a.com/{i}"))
        assert s.cache_stats()["entries"] == 3
        assert s.cache_get("q0", 5) is None          # oldest evicted
        assert s.cache_get("q4", 5) is not None      # newest kept

    def test_limit_is_part_of_the_key(self, tmp_path):
        s = _state(tmp_path, [_BASE])
        s.cache_put("q", 5, _result())
        assert s.cache_get("q", 10) is None

    def test_persists_across_instances(self, tmp_path):
        clock = [_BASE]
        _state(tmp_path, clock).cache_put("q", 5, _result())
        again = _state(tmp_path, clock)  # same db_path → same cache
        assert again.cache_get("q", 5) == _result()

    def test_hit_counter_in_stats(self, tmp_path):
        s = _state(tmp_path, [_BASE])
        s.cache_put("q", 5, _result())
        s.cache_get("q", 5)
        s.cache_get("q", 5)
        assert s.cache_stats() == {"entries": 1, "hits": 2}

    def test_full_reset_clears_cache(self, tmp_path):
        s = _state(tmp_path, [_BASE])
        s.cache_put("q", 5, _result())
        s.reset()
        assert s.cache_get("q", 5) is None
