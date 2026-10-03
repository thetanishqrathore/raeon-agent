#!/usr/bin/env python3
"""Search-router eval (R5).

Exercises the resilient search router on representative lead-gen queries and
measures what matters for a daily, free, high-volume run:

* **coverage** — fraction of queries that returned usable results
* **provider distribution** — which backend actually served each query
* **fallback rate** — how often the primary failed and we rerouted
* **quota consumed** — per-provider, so we can confirm we stay under free caps
* **quality proxy** — result count, domain diversity, query-term relevance

The grading/metric helpers are pure and unit-tested; the live runner drives the
real `resilient` provider (so it reads the actual fallback chain + quota state).
Resumable JSONL output. Live mode needs at least one search backend enabled.

Usage:
  venv/bin/python -m plugins.web.hermes.eval.search_router_eval [--out results.jsonl] [--limit 8]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

# Representative queries lifted from the aviation lead-gen prompt.
SEED_QUERIES: List[str] = [
    '"technical records manager" MRO',
    '"aircraft technical records" maintenance',
    '"AMOS" "technical records"',
    '"AirVault" "technical records"',
    'aircraft records indexing',
    '"maintenance records" "aircraft redelivery"',
    'aircraft lease return documentation',
    '"CAMO" technical records manager',
    'continuing airworthiness records',
    'MRO document controller',
    'aircraft maintenance planning records',
    'aviation document control manager',
    '"back to birth traceability" aircraft',
    'component records aviation',
    'aircraft redelivery records manager',
]

_MIN_USABLE = 3  # a query "covered" if it yields >= this many results


def _domain(url: str) -> str:
    try:
        net = urlsplit(url).netloc.lower()
        return net[4:] if net.startswith("www.") else net
    except Exception:
        return ""


def _tokens(query: str) -> List[str]:
    return [w.strip('"').lower() for w in query.split() if len(w.strip('"')) > 3]


def evaluate_result(query: str, result: Dict[str, Any], route: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Compute per-query metrics from a search result + route info."""
    route = route or {}
    web = ((result or {}).get("data") or {}).get("web") or []
    domains = {_domain(w.get("url", "")) for w in web}
    domains.discard("")
    toks = _tokens(query)
    if toks and web:
        hits = sum(1 for w in web
                   if any(t in (str(w.get("title", "")) + " " + str(w.get("description", ""))).lower()
                          for t in toks))
        relevance = round(hits / len(web), 3)
    else:
        relevance = 0.0
    attempted = route.get("attempted") or []
    served_by = route.get("served_by") or (result or {}).get("router", {}).get("served_by")
    return {
        "query": query,
        "served_by": served_by,
        "count": len(web),
        "domain_diversity": round(len(domains) / len(web), 3) if web else 0.0,
        "relevance": relevance,
        "ok": bool((result or {}).get("success") and len(web) >= 1),
        "covered": len(web) >= _MIN_USABLE,
        "fallback": len([a for a in attempted if a.get("outcome") not in ("ok", "ok_merged")]) > 0,
        "attempts": len(attempted),
    }


def aggregate(records: List[Dict[str, Any]], quota: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    n = len(records)
    if n == 0:
        return {"n": 0}
    dist: Dict[str, int] = {}
    for r in records:
        dist[str(r.get("served_by"))] = dist.get(str(r.get("served_by")), 0) + 1
    return {
        "n": n,
        "coverage": round(sum(1 for r in records if r["covered"]) / n, 3),
        "ok_rate": round(sum(1 for r in records if r["ok"]) / n, 3),
        "fallback_rate": round(sum(1 for r in records if r["fallback"]) / n, 3),
        "mean_relevance": round(sum(r["relevance"] for r in records) / n, 3),
        "mean_diversity": round(sum(r["domain_diversity"] for r in records) / n, 3),
        "provider_distribution": dist,
        "quota_consumed": quota or {},
    }


def _load_done(out_path: Path) -> set:
    done = set()
    if out_path.exists():
        for line in out_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    done.add(json.loads(line)["query"])
                except (ValueError, KeyError):
                    pass
    return done


def run_router_eval(
    queries: List[str],
    search_fn: Callable[[str, int], Tuple[Dict[str, Any], Dict[str, Any]]],
    out_path: Path,
    *,
    limit: int = 8,
    quota_fn: Optional[Callable[[], Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Run each query through search_fn -> (result, route), grade, stream JSONL."""
    done = _load_done(out_path)
    records: List[Dict[str, Any]] = []
    if out_path.exists():
        for line in out_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    records.append(json.loads(line))
                except ValueError:
                    pass
    with out_path.open("a", encoding="utf-8") as fh:
        for q in queries:
            if q in done:
                continue
            t0 = time.time()
            try:
                result, route = search_fn(q, limit)
            except Exception as exc:  # noqa: BLE001
                result, route = {"success": False, "error": str(exc), "data": {"web": []}}, {}
            rec = evaluate_result(q, result, route)
            rec["latency_s"] = round(time.time() - t0, 2)
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fh.flush()
            records.append(rec)
            print(f"  {q[:45]:45} served={rec['served_by']} n={rec['count']} "
                  f"rel={rec['relevance']} fb={rec['fallback']} ({rec['latency_s']}s)")
    return records


def _live_search_fn():
    """Build a search_fn driving the real resilient provider (reads route + quota)."""
    from hermes_cli.plugins import _ensure_plugins_discovered
    from agent.web_search_registry import get_provider
    _ensure_plugins_discovered()
    prov = get_provider("resilient")
    if prov is None:
        raise RuntimeError("resilient provider not registered")

    def _fn(query, limit):
        result = prov.search(query, limit)
        return result, getattr(prov, "last_route", {}) or {}

    quota_fn = (lambda: prov._ensure_state(prov._cfg()).snapshot()) if hasattr(prov, "_ensure_state") else None
    return _fn, quota_fn, prov


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Resilient search-router eval")
    here = Path(__file__).resolve().parent
    ap.add_argument("--out", default=str(here / "search_router_results.jsonl"))
    ap.add_argument("--limit", type=int, default=8)
    args = ap.parse_args(argv)

    try:
        search_fn, quota_fn, _prov = _live_search_fn()
    except Exception as exc:
        print(f"Cannot build live router: {exc}")
        return 1
    print(f"Running {len(SEED_QUERIES)} lead-gen queries through the resilient router...\n")
    records = run_router_eval(SEED_QUERIES, search_fn, Path(args.out), limit=args.limit,
                              quota_fn=quota_fn)
    summary = aggregate(records, quota=quota_fn() if quota_fn else {})
    print("\n=== Search-router eval summary ===")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
