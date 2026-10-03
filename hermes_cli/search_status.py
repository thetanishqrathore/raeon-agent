"""`hermes search-status` — one-screen health view of the free web stack.

Renders, as a compact table:

* the resilient search router's per-provider state (quota counters, circuit
  breaker, cooldowns) via a **read-only** ``RouterState.snapshot()``;
* extract-backend health: SearXNG ping, Crawl4AI service ping (+ version),
  Brave key presence, and which backends config currently selects.

No writes, no crawls, no searches — safe to run any time. Network activity
is limited to two loopback HTTP pings with short timeouts.
"""

from __future__ import annotations

import argparse
from typing import Any, Dict, List, Optional

from hermes_cli.colors import Colors, should_use_color


# ---------------------------------------------------------------------------
# Data collection (read-only)
# ---------------------------------------------------------------------------

def _env_value(name: str) -> str:
    """Config-aware env lookup (``hermes config set`` / ``~/.hermes/.env``)."""
    import os

    try:
        from hermes_cli.config import get_env_value

        val = get_env_value(name)
    except Exception:  # noqa: BLE001
        val = None
    if val is None:
        val = os.getenv(name, "")
    return (val or "").strip()


def _web_config() -> Dict[str, Any]:
    try:
        from hermes_cli.config import load_config

        return load_config().get("web", {}) or {}
    except Exception:  # noqa: BLE001
        return {}


def _router_snapshot() -> Dict[str, Dict[str, Any]]:
    """READ-ONLY view of the router's sqlite state. Never raises."""
    try:
        from agent.search_router_state import RouterState

        return RouterState().snapshot()
    except Exception:  # noqa: BLE001
        return {}


def _router_tiers() -> List[Dict[str, Any]]:
    """Configured tier order — config first, router defaults as fallback."""
    rc = _web_config().get("search_router") or {}
    tiers = rc.get("providers")
    if isinstance(tiers, list) and tiers:
        return [t for t in tiers if isinstance(t, dict) and t.get("name")]
    try:  # read-only import of the router's default tier table
        from plugins.web.resilient.provider import _DEFAULT_TIERS

        return list(_DEFAULT_TIERS)
    except Exception:  # noqa: BLE001
        return []


def _ping_searxng(timeout_s: float = 4.0) -> Dict[str, Any]:
    """Liveness probe for the SearXNG instance. Never raises."""
    url = _env_value("SEARXNG_URL").rstrip("/")
    out: Dict[str, Any] = {"configured": bool(url), "reachable": False,
                           "url": url, "error": None}
    if not url:
        out["error"] = "SEARXNG_URL is not set"
        return out
    try:
        import httpx

        # /healthz is SearXNG's liveness endpoint; fall back to / for old builds.
        for path in ("/healthz", "/"):
            try:
                resp = httpx.get(f"{url}{path}", timeout=timeout_s,
                                 follow_redirects=True)
                if resp.status_code < 500:
                    out["reachable"] = True
                    return out
            except httpx.HTTPError:
                continue
        out["error"] = "no response on /healthz or /"
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def _ping_crawl4ai() -> Dict[str, Any]:
    try:
        from plugins.web.crawl4ai.provider import check_crawl4ai_service

        return check_crawl4ai_service(timeout_s=4.0)
    except Exception as exc:  # noqa: BLE001
        return {"configured": False, "reachable": False, "version": None,
                "url": "", "error": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _c(code: str, text: str, color: bool) -> str:
    return f"{code}{text}{Colors.RESET}" if color else text


def _fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds >= 3600:
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"
    if seconds >= 60:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds}s"


def _render_router_table(color: bool) -> List[str]:
    snap = _router_snapshot()
    tiers = _router_tiers()
    tier_names = [t["name"] for t in tiers]
    caps = {t["name"]: t for t in tiers}
    # Providers with recorded state but no configured tier still show up.
    names = tier_names + sorted(n for n in snap if n not in tier_names)

    lines = [_c(Colors.BOLD, "Search router (web.search_backend: resilient)", color)]
    if not names:
        lines.append("  no router state yet — run a search to populate it")
        return lines

    header = f"  {'provider':<12} {'today':>7} {'month':>7} {'fails':>5}  {'state':<10} note"
    lines.append(_c(Colors.DIM, header, color))
    for name in names:
        st = snap.get(name, {})
        cap = caps.get(name, {})
        day = st.get("day_count", 0)
        month = st.get("month_count", 0)
        dcap = cap.get("daily_cap") or 0
        mcap = cap.get("monthly_cap") or 0
        day_s = f"{day}/{dcap}" if dcap else f"{day}"
        month_s = f"{month}/{mcap}" if mcap else f"{month}"
        fails = st.get("consecutive_failures", 0)

        note = ""
        if st.get("circuit_open"):
            state = _c(Colors.RED, "cooldown", color)
            note = f"retry in {_fmt_duration(st.get('cooldown_remaining', 0))}"
        elif dcap and day >= dcap:
            state = _c(Colors.YELLOW, "day-cap", color)
        elif mcap and month >= mcap:
            state = _c(Colors.YELLOW, "month-cap", color)
        elif not st:
            state = _c(Colors.DIM, "idle", color)
        else:
            state = _c(Colors.GREEN, "ok", color)
        if name not in caps and st:
            note = (note + " " if note else "") + "(not in tier config)"

        lines.append(
            f"  {name:<12} {day_s:>7} {month_s:>7} {fails:>5}  {state:<19} {note}".rstrip()
        )
    return lines


def _render_extract_table(color: bool) -> List[str]:
    cfg = _web_config()
    extract_backend = cfg.get("extract_backend") or cfg.get("backend") or "auto"
    fallback = cfg.get("extract_fallback", True)

    lines = [_c(Colors.BOLD, f"Extract backends (web.extract_backend: {extract_backend}, "
                             f"fallback: {'on' if fallback else 'off'})", color)]

    def _row(name: str, ok: Optional[bool], detail: str) -> str:
        if ok is True:
            mark = _c(Colors.GREEN, "up", color)
        elif ok is False:
            mark = _c(Colors.RED, "DOWN", color)
        else:
            mark = _c(Colors.DIM, "n/a", color)
        return f"  {name:<12} {mark:<15} {detail}".rstrip()

    c4a = _ping_crawl4ai()
    if c4a["configured"]:
        detail = c4a["url"] + (f"  (v{c4a['version']})" if c4a.get("version") else "")
        if not c4a["reachable"]:
            detail += f"  — {c4a.get('error') or 'unreachable'}"
        lines.append(_row("crawl4ai", c4a["reachable"], detail))
    else:
        lines.append(_row("crawl4ai", None, "CRAWL4AI_URL not set (see docker/crawl4ai/README.md)"))

    sx = _ping_searxng()
    if sx["configured"]:
        detail = sx["url"] + ("" if sx["reachable"] else f"  — {sx.get('error') or 'unreachable'}")
        lines.append(_row("searxng", sx["reachable"], detail))
    else:
        lines.append(_row("searxng", None, "SEARXNG_URL not set"))

    lines.append(_row(
        "hermes", True, "native scraper — always available (no service)",
    ))
    brave = bool(_env_value("BRAVE_SEARCH_API_KEY"))
    lines.append(_row(
        "brave-free", True if brave else None,
        "API key present" if brave else "BRAVE_SEARCH_API_KEY not set",
    ))
    return lines


def _cmd_search_status(args: argparse.Namespace) -> int:  # noqa: ARG001
    color = should_use_color()
    sections = [
        _render_extract_table(color),
        [""],
        _render_router_table(color),
    ]
    for section in sections:
        for line in section:
            print(line)
    return 0


def register_cli(parent: argparse.ArgumentParser) -> None:
    """Attach the ``search-status`` handler. main.py calls this."""
    parent.set_defaults(func=_cmd_search_status)
