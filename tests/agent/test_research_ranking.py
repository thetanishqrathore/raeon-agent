"""Tests for RRF source fusion + domain-diversity selection (P1.2)."""

from agent.research_ranking import (
    domain_of,
    reciprocal_rank_fusion,
    select_diverse,
    rank_and_select,
)


def _hit(url, title=""):
    return {"url": url, "title": title}


class TestDomainOf:
    def test_strips_www_and_port(self):
        assert domain_of("https://www.example.com:443/path") == "example.com"

    def test_subdomains_are_distinct(self):
        assert domain_of("https://docs.example.com/x") == "docs.example.com"
        assert domain_of("https://blog.example.com/y") == "blog.example.com"


class TestRRF:
    def test_url_from_multiple_lists_outranks_single(self):
        l1 = [_hit("https://a.com"), _hit("https://b.com")]
        l2 = [_hit("https://b.com"), _hit("https://c.com")]
        ranked = reciprocal_rank_fusion([l1, l2])
        # b appears in both lists -> highest fused score.
        assert ranked[0]["canonical_url"] == "https://b.com"

    def test_dedup_by_canonical_url(self):
        l1 = [_hit("https://a.com/p/?utm_source=x")]
        l2 = [_hit("https://a.com/p")]
        ranked = reciprocal_rank_fusion([l1, l2])
        assert len(ranked) == 1
        # Score accumulated across both appearances.
        assert ranked[0]["rrf_score"] > 1.0 / (60 + 1)

    def test_preserves_title(self):
        ranked = reciprocal_rank_fusion([[_hit("https://a.com", "Title A")]])
        assert ranked[0]["title"] == "Title A"

    def test_deterministic_tie_break_by_first_seen(self):
        # Two URLs each appearing once at rank 1 -> equal score -> first-seen wins.
        ranked = reciprocal_rank_fusion([[_hit("https://a.com")], [_hit("https://z.com")]])
        assert [r["canonical_url"] for r in ranked] == ["https://a.com", "https://z.com"]


class TestDiversity:
    def test_one_per_domain_floor(self):
        ranked = [
            _hit("https://x.com/1"), _hit("https://x.com/2"), _hit("https://y.com/1"),
        ]
        sel = select_diverse(ranked, max_results=2, per_domain=1)
        domains = {domain_of(s["url"]) for s in sel}
        assert domains == {"x.com", "y.com"}

    def test_backfill_when_sources_cluster(self):
        # All on one domain; with per_domain=1 we'd get 1, but max_results=2 must
        # backfill rather than under-select.
        ranked = [_hit("https://x.com/1"), _hit("https://x.com/2")]
        sel = select_diverse(ranked, max_results=2, per_domain=1)
        assert len(sel) == 2

    def test_zero_max_results(self):
        assert select_diverse([_hit("https://x.com")], max_results=0) == []


class TestRankAndSelect:
    def test_end_to_end(self):
        l1 = [_hit("https://a.com/1"), _hit("https://a.com/2"), _hit("https://b.com")]
        l2 = [_hit("https://b.com"), _hit("https://c.com")]
        sel = rank_and_select([l1, l2], max_results=3, per_domain=1)
        domains = [domain_of(s["url"]) for s in sel]
        # b.com fused highest; diversity keeps one per domain first.
        assert domains[0] == "b.com"
        assert len(set(domains)) == len(domains)  # all distinct domains at per_domain=1
