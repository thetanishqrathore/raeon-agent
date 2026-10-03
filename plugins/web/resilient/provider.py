#!/usr/bin/env python3
"""Resilient search router (R2).

A meta-`WebSearchProvider` that turns Hermes' pool of unreliable free search
backends into one robust search source: it tries providers in a quality-first,
quota-preserving tier order, classifies each failure, and reroutes — so a
rate-limited / quota-exhausted / down provider is skipped (with backoff) instead
of failing the whole research run. Activate with ``web.search_backend: resilient``.

Design:
- **Tier order** (config ``web.search_router.providers``) spends free *uncapped*
  providers first (SearXNG, ddgs) and reserves *capped* ones (Brave, Tavily,
  Google CSE) as fallbacks so we never burn a precious free quota on a routine
  query.
- **Health + quota** come from :mod:`agent.search_router_state` (persists across
  daily runs): circuit breaker for transient failures, counters for capped tiers.
- **Failure classification** maps provider errors/exceptions to an action
  (rate-limited → short backoff, quota → defer to window reset, auth → long
  disable, unavailable → retry-elsewhere).
- **Optional RRF merge** (``merge_uncapped``) fans out to the uncapped providers
  and fuses results with :func:`agent.research_ranking.reciprocal_rank_fusion`
  for a free quality boost.
- **Two-level result cache** — a per-process dict for repeats within one run,
  plus a persistent TTL cache in the RouterState SQLite (A7) so identical
  discovery queries across daily runs don't re-spend capped quota. Both levels
  serve DEEP COPIES: a caller mutating a returned result can never poison the
  cache. Cache hits never touch provider quotas.
- **Never silently empty**: if every provider is unavailable/exhausted it returns
  an explicit error so the agent pauses/retries rather than fabricating.
"""

from __future__ import annotations

import copy
import logging
from typing import Any, Dict, List, Optional

from agent.web_search_provider import WebSearchProvider
from agent.search_router_state import (
    RouterState, OK, RATE_LIMITED, QUOTA_EXCEEDED, AUTH_ERROR, UNAVAILABLE, EMPTY,
)

logger = logging.getLogger(__name__)

# Quality-first, quota-preserving default tiers. Uncapped (cap 0 = unlimited)
# first; capped free tiers last. Providers not registered/available are skipped.
_DEFAULT_TIERS = [
    {"name": "searxng", "daily_cap": 0, "monthly_cap": 0},
    {"name": "ddgs", "daily_cap": 0, "monthly_cap": 0},
    {"name": "brave-free", "daily_cap": 0, "monthly_cap": 2000},
    {"name": "tavily", "daily_cap": 0, "monthly_cap": 1000},
    {"name": "google-cse", "daily_cap": 100, "monthly_cap": 0},
]


def classify_error_text(text: str) -> str:
    """Map an error/exception string to a router action. Order matters."""
    s = (text or "").lower()
    if any(k in s for k in ("quota", "exceeded", "insufficient", "credit", "402",
                            "monthly limit", "out of searches", "limit reached")):
        return QUOTA_EXCEEDED
    if any(k in s for k in ("401", "403", "unauthorized", "forbidden", "api key",
                            "api_key", "is not set", "invalid key", "no api key",
                            "authentication")):
        return AUTH_ERROR
    if any(k in s for k in ("429", "rate limit", "ratelimit", "too many", "throttle")):
        return RATE_LIMITED
    if any(k in s for k in ("timeout", "timed out", "connection", "unreachable",
                            "refused", "could not resolve", "dns", "502", "503", "504")):
        return UNAVAILABLE
    return UNAVAILABLE


def classify_result(result: Any) -> str:
    """Classify a provider.search() return value."""
    if not isinstance(result, dict):
        return UNAVAILABLE
    if result.get("success"):
        web = (result.get("data") or {}).get("web") or []
        return OK if web else EMPTY
    return classify_error_text(str(result.get("error") or ""))


def classify_exception(exc: BaseException) -> str:
    return classify_error_text(f"{type(exc).__name__}: {exc}")


def _empty_result(error: str) -> Dict[str, Any]:
    return {"success": False, "error": error, "data": {"web": []}}


def _float_or(value: Any, default: float) -> float:
    """Float coercion that preserves an explicit 0 (unlike ``or default``)."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


# Per-process result cache bound — the provider instance lives for the whole
# process (web server / long agent run), so the cache must not grow unbounded.
_CACHE_MAX = 256


class ResilientSearchProvider(WebSearchProvider):
    """Routes web_search across the configured provider tiers with fallback."""

    def __init__(self):
        self._state: Optional[RouterState] = None
        # Small per-process cache so a repeated query in one run doesn't re-spend
        # quota / re-hit a provider. Stores private copies; hits are served as
        # deep copies (the July 2026 audit flagged reference-poisoning here).
        self._cache: Dict[tuple, Dict[str, Any]] = {}
        self.last_route: Dict[str, Any] = {}
        # Diagnostics only — cache hits never count against provider quotas.
        self.cache_hits: Dict[str, int] = {"memory": 0, "persistent": 0}

    @property
    def name(self) -> str:
        return "resilient"

    @property
    def display_name(self) -> str:
        return "Resilient Router (multi-provider, quota-aware fallback)"

    def is_available(self) -> bool:
        # The router is always 'available' as a meta-provider; it surfaces a
        # precise error from search() if nothing is behind it. (No network here.)
        return True

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        # Extraction stays on the native scraper / configured extract backend.
        return False

    # -- config -----------------------------------------------------------
    def _cfg(self) -> Dict[str, Any]:
        try:
            from hermes_cli.config import load_config
            rc = (load_config().get("web") or {}).get("search_router") or {}
        except Exception:
            rc = {}
        tiers = rc.get("providers") if isinstance(rc.get("providers"), list) else None
        return {
            "tiers": tiers or _DEFAULT_TIERS,
            "merge_uncapped": bool(rc.get("merge_uncapped", False)),
            "rate_limit_cooldown_seconds": float(rc.get("rate_limit_cooldown_seconds", 30) or 30),
            "max_cooldown_seconds": float(rc.get("max_cooldown_seconds", 900) or 900),
            "auth_error_disable_seconds": float(rc.get("auth_error_disable_seconds", 86400) or 86400),
            # 0 is meaningful for these (disable), so no `or default`.
            "empty_cooldown_s": _float_or(rc.get("empty_cooldown_s"), 30.0),
            "cache_ttl_s": _float_or(rc.get("cache_ttl_s"), 86400.0),
            "cache_max_entries": int(_float_or(rc.get("cache_max_entries"), 512)),
        }

    def _ensure_state(self, cfg: Dict[str, Any]) -> RouterState:
        if self._state is None:
            self._state = RouterState(
                rate_limit_cooldown_seconds=cfg["rate_limit_cooldown_seconds"],
                max_cooldown_seconds=cfg["max_cooldown_seconds"],
                auth_error_disable_seconds=cfg["auth_error_disable_seconds"],
                empty_cooldown_s=cfg.get("empty_cooldown_s", 30.0),
                cache_ttl_s=cfg.get("cache_ttl_s", 86400.0),
                cache_max_entries=cfg.get("cache_max_entries", 512),
            )
        return self._state

    @staticmethod
    def _is_available_safe(p: WebSearchProvider) -> bool:
        try:
            return bool(p.is_available())
        except Exception:
            return False

    # -- routing ----------------------------------------------------------
    def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        cfg = self._cfg()
        state = self._ensure_state(cfg)
        ckey = (query, int(limit))
        if ckey in self._cache:
            return self._cache_hit(copy.deepcopy(self._cache[ckey]), tier="memory")
        cached = state.cache_get(query, int(limit))
        if cached is not None:
            # Seed the per-process cache so repeats in this run stay off SQLite.
            self._cache_store(ckey, cached)
            return self._cache_hit(cached, tier="persistent")

        from agent.web_search_registry import get_provider

        tiers = cfg["tiers"]
        attempted: List[Dict[str, Any]] = []

        # Optional quality boost: merge the uncapped providers via RRF.
        if cfg["merge_uncapped"]:
            merged = self._merge_uncapped(query, limit, tiers, state, get_provider, attempted)
            if merged is not None:
                return self._finish(ckey, merged, served_by="merge:uncapped", attempted=attempted)

        for tier in tiers:
            name = str(tier.get("name") or "").strip()
            if not name or name == self.name:
                continue
            daily = tier.get("daily_cap")
            monthly = tier.get("monthly_cap")
            p = get_provider(name)
            if p is None or not p.supports_search():
                attempted.append({"provider": name, "outcome": "absent"})
                continue
            if not state.available(name, daily, monthly):
                attempted.append({"provider": name, "outcome": "circuit_or_quota"})
                continue
            if not self._is_available_safe(p):
                attempted.append({"provider": name, "outcome": "not_configured"})
                continue

            action, result = self._call(p, query, limit)
            # The request hit the provider (unless it was an auth/no-key issue);
            # count it against quota so capped tiers stay under their cap.
            if action != AUTH_ERROR:
                state.record_use(name)
            if action == OK:
                state.record_success(name)
                attempted.append({"provider": name, "outcome": "ok"})
                return self._finish(ckey, result, served_by=name, attempted=attempted)
            state.record_failure(name, action)
            attempted.append({"provider": name, "outcome": action})

        # Everything failed/unavailable — explicit, never a silent empty.
        summary = ", ".join(f"{a['provider']}:{a['outcome']}" for a in attempted) or "none registered"
        logger.warning("Resilient router: all providers exhausted (%s)", summary)
        out = _empty_result(
            "All configured search providers are unavailable or quota-exhausted "
            f"({summary}). Configure another backend or wait for quota/cooldown to reset."
        )
        self.last_route = {"served_by": None, "attempted": attempted}
        return out

    def _call(self, p: WebSearchProvider, query: str, limit: int):
        try:
            result = p.search(query, limit)
            return classify_result(result), result
        except Exception as exc:  # noqa: BLE001 — classify, never propagate
            logger.debug("provider %s.search raised: %s", p.name, exc)
            return classify_exception(exc), None

    def _cache_hit(self, result: Dict[str, Any], *, tier: str) -> Dict[str, Any]:
        """Annotate + account a cache hit (quotas untouched by design)."""
        self.cache_hits[tier] = self.cache_hits.get(tier, 0) + 1
        router = result.setdefault("router", {})
        router["cache"] = tier
        self.last_route = {
            "served_by": router.get("served_by"),
            "cache": tier,
            "attempted": [],
        }
        return result

    def _cache_store(self, ckey, result) -> None:
        """Keep a private deep copy in the bounded per-process cache."""
        while len(self._cache) >= _CACHE_MAX:  # FIFO-evict oldest entries
            self._cache.pop(next(iter(self._cache)))
        self._cache[ckey] = copy.deepcopy(result)

    def _finish(self, ckey, result, *, served_by, attempted):
        self.last_route = {"served_by": served_by, "attempted": attempted}
        if isinstance(result, dict):
            result.setdefault("router", {})["served_by"] = served_by
        self._cache_store(ckey, result)
        if self._state is not None:
            try:
                self._state.cache_put(ckey[0], ckey[1], result)
            except Exception:  # noqa: BLE001 — caching must never break a search
                logger.debug("persistent search-cache write failed", exc_info=True)
        return result

    def _merge_uncapped(self, query, limit, tiers, state, get_provider, attempted):
        """Query the available uncapped providers and RRF-fuse their results."""
        lists: List[List[Dict[str, Any]]] = []
        used: List[str] = []
        for tier in tiers:
            name = str(tier.get("name") or "").strip()
            cap_d, cap_m = tier.get("daily_cap"), tier.get("monthly_cap")
            uncapped = (not cap_d) and (not cap_m)
            if not uncapped or name == self.name:
                continue
            p = get_provider(name)
            if p is None or not p.supports_search() or not self._is_available_safe(p):
                continue
            if not state.available(name, cap_d, cap_m):
                continue
            action, result = self._call(p, query, limit)
            state.record_use(name)
            if action == OK:
                state.record_success(name)
                lists.append((result.get("data") or {}).get("web") or [])
                used.append(name)
            else:
                state.record_failure(name, action)
                attempted.append({"provider": name, "outcome": action})
        if not lists:
            return None
        try:
            from agent.research_ranking import reciprocal_rank_fusion
            fused = reciprocal_rank_fusion(lists)[: int(limit)]
        except Exception:
            fused = [item for lst in lists for item in lst][: int(limit)]
        web = [{"title": it.get("title", ""), "url": it.get("url", ""),
                "description": it.get("description", ""), "position": i + 1}
               for i, it in enumerate(fused)]
        attempted.append({"provider": "+".join(used), "outcome": "ok_merged"})
        return {"success": True, "data": {"web": web}}
