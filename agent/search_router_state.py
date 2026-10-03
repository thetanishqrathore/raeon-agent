#!/usr/bin/env python3
"""Persistent state for the resilient search router (R1).

No single free search backend is reliable, so reliability is an algorithm: the
router tries providers in a quality-first order and reroutes on failure. This
module is the router's memory — it survives across daily runs so quota and
circuit-breaker state aren't reset every process:

* **Quota counters** — per-provider day/month usage with automatic window
  rollover, so we can keep capped free tiers (Brave ~2k/mo, Google CSE 100/day)
  strictly under their limits.
* **Circuit breaker** — per-provider consecutive-failure count + a
  ``cooldown_until`` timestamp so a rate-limited/unavailable/quota-exhausted
  provider is skipped for a backoff window instead of hammered.
* **Search cache (A7)** — query→results cache with a TTL, shared across runs,
  so the daily lead-gen batch re-issuing identical discovery queries doesn't
  burn capped free quota every morning. Only successful non-empty results are
  cached; reads return fresh deep copies (JSON round-trip) so a caller
  mutating a result can never poison the cache.

Pure storage + policy; no network. SQLite-backed (mirrors
:mod:`agent.research_state`); ``db_path`` and ``now`` are injectable for tests.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple


_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS router_state ("
    "provider TEXT PRIMARY KEY, "
    "day_key TEXT, day_count INTEGER DEFAULT 0, "
    "month_key TEXT, month_count INTEGER DEFAULT 0, "
    "consecutive_failures INTEGER DEFAULT 0, "
    "last_failure TEXT DEFAULT '', "
    "cooldown_until REAL DEFAULT 0, "
    "updated_at REAL DEFAULT 0)"
)

_CACHE_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS search_cache ("
    "cache_key TEXT PRIMARY KEY, "
    "query TEXT DEFAULT '', "
    "result_json TEXT NOT NULL, "
    "hits INTEGER DEFAULT 0, "
    "created_at REAL DEFAULT 0)"
)

# Failure actions the router classifies into.
OK = "OK"
RATE_LIMITED = "RATE_LIMITED"
QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
AUTH_ERROR = "AUTH_ERROR"
UNAVAILABLE = "UNAVAILABLE"
EMPTY = "EMPTY"

_UNLIMITED = float("inf")


def _day_key(now: float) -> str:
    t = time.gmtime(now)
    return f"{t.tm_year:04d}-{t.tm_mon:02d}-{t.tm_mday:02d}"


def _month_key(now: float) -> str:
    t = time.gmtime(now)
    return f"{t.tm_year:04d}-{t.tm_mon:02d}"


def _secs_to_next_utc_midnight(now: float) -> float:
    t = time.gmtime(now)
    return 86400 - (t.tm_hour * 3600 + t.tm_min * 60 + t.tm_sec)


def _cap_value(cap: Optional[int]) -> float:
    """A cap of None/0/negative means unlimited."""
    if cap is None:
        return _UNLIMITED
    try:
        c = int(cap)
    except (TypeError, ValueError):
        return _UNLIMITED
    return _UNLIMITED if c <= 0 else float(c)


class RouterState:
    """SQLite-backed quota + circuit-breaker state for the search router."""

    def __init__(
        self,
        db_path: Optional[str] = None,
        *,
        now: Callable[[], float] = time.time,
        rate_limit_cooldown_seconds: float = 30.0,
        max_cooldown_seconds: float = 900.0,
        auth_error_disable_seconds: float = 86400.0,
        empty_cooldown_s: float = 30.0,
        cache_ttl_s: float = 86400.0,
        cache_max_entries: int = 512,
    ):
        if db_path is None:
            try:
                from hermes_constants import get_hermes_home
                db_path = str(Path(get_hermes_home()) / "search_router.db")
            except Exception:
                db_path = str(Path.home() / ".hermes" / "search_router.db")
        self.db_path = str(db_path)
        self._now = now
        self._base_cd = float(rate_limit_cooldown_seconds)
        self._max_cd = float(max_cooldown_seconds)
        self._auth_cd = float(auth_error_disable_seconds)
        self._empty_cd = float(empty_cooldown_s)
        self._cache_ttl = float(cache_ttl_s)
        self._cache_max = max(0, int(cache_max_entries))
        self._lock = threading.RLock()
        self._ensure()

    # -- storage ----------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(self.db_path, timeout=10)

    def _ensure(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(_SCHEMA)
            conn.execute(_CACHE_SCHEMA)

    def _row(self, provider: str) -> Dict[str, Any]:
        with self._lock, self._connect() as conn:
            r = conn.execute(
                "SELECT day_key, day_count, month_key, month_count, "
                "consecutive_failures, last_failure, cooldown_until "
                "FROM router_state WHERE provider=?", (provider,)
            ).fetchone()
        if not r:
            return {"day_key": "", "day_count": 0, "month_key": "", "month_count": 0,
                    "consecutive_failures": 0, "last_failure": "", "cooldown_until": 0.0}
        return {"day_key": r[0] or "", "day_count": r[1] or 0,
                "month_key": r[2] or "", "month_count": r[3] or 0,
                "consecutive_failures": r[4] or 0, "last_failure": r[5] or "",
                "cooldown_until": r[6] or 0.0}

    def _save(self, provider: str, row: Dict[str, Any]) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO router_state (provider, day_key, day_count, month_key, "
                "month_count, consecutive_failures, last_failure, cooldown_until, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(provider) DO UPDATE SET "
                "day_key=excluded.day_key, day_count=excluded.day_count, "
                "month_key=excluded.month_key, month_count=excluded.month_count, "
                "consecutive_failures=excluded.consecutive_failures, "
                "last_failure=excluded.last_failure, cooldown_until=excluded.cooldown_until, "
                "updated_at=excluded.updated_at",
                (provider, row["day_key"], row["day_count"], row["month_key"],
                 row["month_count"], row["consecutive_failures"], row["last_failure"],
                 row["cooldown_until"], self._now()),
            )

    def _rolled(self, row: Dict[str, Any], now: float) -> Dict[str, Any]:
        """Zero day/month counters whose window has changed since last write."""
        dk, mk = _day_key(now), _month_key(now)
        if row["day_key"] != dk:
            row["day_key"], row["day_count"] = dk, 0
        if row["month_key"] != mk:
            row["month_key"], row["month_count"] = mk, 0
        return row

    # -- quota ------------------------------------------------------------
    def record_use(self, provider: str) -> None:
        now = self._now()
        with self._lock:
            row = self._rolled(self._row(provider), now)
            row["day_count"] += 1
            row["month_count"] += 1
            self._save(provider, row)

    def usage(self, provider: str) -> Tuple[int, int]:
        """Return (day_count, month_count) for the CURRENT windows."""
        row = self._rolled(self._row(provider), self._now())
        return int(row["day_count"]), int(row["month_count"])

    def remaining(self, provider: str, daily_cap: Optional[int], monthly_cap: Optional[int]) -> float:
        day_used, month_used = self.usage(provider)
        d = _cap_value(daily_cap) - day_used
        m = _cap_value(monthly_cap) - month_used
        return max(0.0, min(d, m))

    # -- circuit breaker --------------------------------------------------
    def record_failure(self, provider: str, ftype: str) -> None:
        now = self._now()
        with self._lock:
            row = self._rolled(self._row(provider), now)
            if ftype == EMPTY:
                # EMPTY is a healthy response with zero hits, not a failure: a
                # burst of legitimately-zero-result queries must not open every
                # circuit. Flat, short cooldown (``empty_cooldown_s`` config;
                # 0 disables it entirely), no escalation, and the consecutive-
                # failure counter is left untouched so real failures before/
                # after keep their own backoff trajectory.
                row["last_failure"] = ftype
                if self._empty_cd > 0:
                    row["cooldown_until"] = max(
                        float(row["cooldown_until"]), now + self._empty_cd
                    )
                self._save(provider, row)
                return
            row["consecutive_failures"] = int(row["consecutive_failures"]) + 1
            row["last_failure"] = ftype
            n = row["consecutive_failures"]
            if ftype == AUTH_ERROR:
                cooldown = self._auth_cd
            elif ftype == QUOTA_EXCEEDED:
                # Defer until the next daily window resets (covers daily caps;
                # monthly caps just keep deferring a day at a time until reset).
                cooldown = _secs_to_next_utc_midnight(now)
            else:  # RATE_LIMITED / UNAVAILABLE → exponential backoff
                cooldown = min(self._max_cd, self._base_cd * (2 ** (n - 1)))
            row["cooldown_until"] = now + cooldown
            self._save(provider, row)

    def record_success(self, provider: str) -> None:
        with self._lock:
            row = self._rolled(self._row(provider), self._now())
            row["consecutive_failures"] = 0
            row["last_failure"] = ""
            row["cooldown_until"] = 0.0
            self._save(provider, row)

    def is_circuit_open(self, provider: str) -> bool:
        return float(self._row(provider)["cooldown_until"]) > self._now()

    def cooldown_remaining(self, provider: str) -> float:
        return max(0.0, float(self._row(provider)["cooldown_until"]) - self._now())

    def available(self, provider: str, daily_cap: Optional[int], monthly_cap: Optional[int]) -> bool:
        """True when the circuit is closed AND quota remains."""
        if self.is_circuit_open(provider):
            return False
        return self.remaining(provider, daily_cap, monthly_cap) > 0

    def snapshot(self) -> Dict[str, Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT provider, day_count, month_count, consecutive_failures, "
                "last_failure, cooldown_until FROM router_state"
            ).fetchall()
        now = self._now()
        return {
            r[0]: {"day_count": r[1], "month_count": r[2], "consecutive_failures": r[3],
                   "last_failure": r[4], "circuit_open": (r[5] or 0) > now,
                   "cooldown_remaining": max(0.0, (r[5] or 0) - now)}
            for r in rows
        }

    def reset(self, provider: Optional[str] = None) -> None:
        with self._lock, self._connect() as conn:
            if provider:
                conn.execute("DELETE FROM router_state WHERE provider=?", (provider,))
            else:
                conn.execute("DELETE FROM router_state")
                conn.execute("DELETE FROM search_cache")

    # -- persistent search cache (A7) --------------------------------------
    @staticmethod
    def _cache_key(query: str, limit: int) -> str:
        return f"{int(limit)}|{(query or '').strip()}"

    @staticmethod
    def _cacheable(result: Any) -> bool:
        """Only successful, non-empty search results are worth persisting."""
        if not isinstance(result, dict) or not result.get("success"):
            return False
        return bool((result.get("data") or {}).get("web"))

    def cache_get(self, query: str, limit: int) -> Optional[Dict[str, Any]]:
        """Return a cached result as a fresh deep copy, or None.

        Expired entries are pruned on read. The JSON round-trip guarantees the
        caller can mutate the returned dict without poisoning the cache.
        """
        if self._cache_ttl <= 0:
            return None
        key = self._cache_key(query, limit)
        now = self._now()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT result_json, created_at FROM search_cache WHERE cache_key=?",
                (key,),
            ).fetchone()
            if not row:
                return None
            if (row[1] or 0) + self._cache_ttl <= now:
                conn.execute("DELETE FROM search_cache WHERE cache_key=?", (key,))
                return None
            conn.execute(
                "UPDATE search_cache SET hits = hits + 1 WHERE cache_key=?", (key,)
            )
        try:
            return json.loads(row[0])
        except (TypeError, ValueError):
            return None

    def cache_put(self, query: str, limit: int, result: Any) -> None:
        """Persist a successful non-empty result; evict beyond the entry cap."""
        if self._cache_ttl <= 0 or self._cache_max <= 0 or not self._cacheable(result):
            return
        try:
            blob = json.dumps(result, ensure_ascii=False)
        except (TypeError, ValueError):
            return  # non-serializable payload — never let caching break a search
        key = self._cache_key(query, limit)
        now = self._now()
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO search_cache (cache_key, query, result_json, hits, created_at) "
                "VALUES (?,?,?,0,?) ON CONFLICT(cache_key) DO UPDATE SET "
                "result_json=excluded.result_json, created_at=excluded.created_at",
                (key, (query or "").strip(), blob, now),
            )
            # Opportunistic TTL prune + newest-N eviction keep the table bounded.
            conn.execute(
                "DELETE FROM search_cache WHERE created_at + ? <= ?",
                (self._cache_ttl, now),
            )
            conn.execute(
                "DELETE FROM search_cache WHERE cache_key NOT IN ("
                "SELECT cache_key FROM search_cache "
                "ORDER BY created_at DESC, rowid DESC LIMIT ?)",
                (self._cache_max,),
            )

    def cache_stats(self) -> Dict[str, int]:
        """Diagnostics: entry count + cumulative hits (never touches quotas)."""
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(hits), 0) FROM search_cache"
            ).fetchone()
        return {"entries": int(row[0] or 0), "hits": int(row[1] or 0)}
