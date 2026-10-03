"""Smoke + unit tests for the search-router eval (R5)."""

from plugins.web.hermes.eval.search_router_eval import (
    SEED_QUERIES, evaluate_result, aggregate, run_router_eval,
)


def _web(*urls):
    return {"success": True, "data": {"web": [
        {"title": "aircraft technical records", "url": u, "description": "maintenance records"}
        for u in urls]}}


class TestEvaluateResult:
    def test_covered_and_relevance(self):
        res = _web("https://a.com", "https://b.com", "https://c.com")
        route = {"served_by": "ddgs", "attempted": [{"provider": "ddgs", "outcome": "ok"}]}
        rec = evaluate_result('"aircraft technical records" maintenance', res, route)
        assert rec["served_by"] == "ddgs"
        assert rec["count"] == 3 and rec["covered"] is True
        assert rec["relevance"] == 1.0          # titles contain query tokens
        assert rec["domain_diversity"] == 1.0   # 3 distinct domains
        assert rec["fallback"] is False

    def test_fallback_detected_from_route(self):
        res = _web("https://a.com")
        route = {"served_by": "ddgs", "attempted": [
            {"provider": "searxng", "outcome": "RATE_LIMITED"},
            {"provider": "ddgs", "outcome": "ok"}]}
        rec = evaluate_result("q", res, route)
        assert rec["fallback"] is True and rec["attempts"] == 2

    def test_empty_result(self):
        rec = evaluate_result("q", {"success": False, "data": {"web": []}}, {})
        assert rec["ok"] is False and rec["covered"] is False and rec["count"] == 0


class TestAggregate:
    def test_metrics(self):
        recs = [
            {"served_by": "searxng", "covered": True, "ok": True, "fallback": False, "relevance": 1.0, "domain_diversity": 1.0},
            {"served_by": "ddgs", "covered": True, "ok": True, "fallback": True, "relevance": 0.5, "domain_diversity": 0.8},
            {"served_by": "ddgs", "covered": False, "ok": False, "fallback": True, "relevance": 0.0, "domain_diversity": 0.0},
        ]
        agg = aggregate(recs, quota={"brave-free": {"month_count": 3}})
        assert agg["n"] == 3
        assert agg["coverage"] == round(2 / 3, 3)
        assert agg["fallback_rate"] == round(2 / 3, 3)
        assert agg["provider_distribution"] == {"searxng": 1, "ddgs": 2}
        assert agg["quota_consumed"] == {"brave-free": {"month_count": 3}}

    def test_empty(self):
        assert aggregate([])["n"] == 0


class TestRunResumable:
    def test_streams_and_resumes(self, tmp_path):
        out = tmp_path / "r.jsonl"
        calls = []

        def fn(q, limit):
            calls.append(q)
            return _web("https://a.com", "https://b.com", "https://c.com"), \
                {"served_by": "ddgs", "attempted": [{"provider": "ddgs", "outcome": "ok"}]}

        recs = run_router_eval(["q1", "q2"], fn, out, limit=5)
        assert len(recs) == 2 and len(out.read_text().strip().splitlines()) == 2
        calls.clear()
        run_router_eval(["q1", "q2"], fn, out, limit=5)  # all done → no new calls
        assert calls == []


class TestSeed:
    def test_seed_queries_are_lead_gen_shaped(self):
        assert len(SEED_QUERIES) >= 15
        joined = " ".join(SEED_QUERIES).lower()
        for kw in ("technical records", "mro", "amos", "redelivery", "camo", "airworthiness"):
            assert kw in joined
