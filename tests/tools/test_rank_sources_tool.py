"""Tests for the rank_sources tool (P1.2)."""

import json

from tools.rank_sources_tool import rank_sources, RANK_SOURCES_SCHEMA, check_rank_sources_requirements


def _hit(url, title=""):
    return {"url": url, "title": title}


class TestRankSources:
    def test_merges_and_diversifies(self):
        l1 = [_hit("https://a.com/1"), _hit("https://a.com/2"), _hit("https://b.com")]
        l2 = [_hit("https://b.com"), _hit("https://c.com")]
        out = json.loads(rank_sources([l1, l2], max_results=3, per_domain=1))
        domains = [s["domain"] for s in out["selected"]]
        assert out["selected"][0]["domain"] == "b.com"  # fused highest
        assert len(set(domains)) == len(domains)         # one per domain
        assert out["input_count"] == 5

    def test_flat_list_accepted(self):
        out = json.loads(rank_sources([_hit("https://a.com"), _hit("https://b.com")]))
        assert out["selected_count"] == 2

    def test_empty_is_error(self):
        out = json.loads(rank_sources([]))
        assert "error" in out

    def test_non_list_is_error(self):
        out = json.loads(rank_sources("nope"))
        assert "error" in out

    def test_bad_numbers_fall_back_to_defaults(self):
        out = json.loads(rank_sources([[_hit("https://a.com")]], max_results="x", per_domain=None))
        assert out["selected_count"] == 1

    def test_requirements(self):
        assert check_rank_sources_requirements() is True

    def test_schema(self):
        assert RANK_SOURCES_SCHEMA["name"] == "rank_sources"
        assert RANK_SOURCES_SCHEMA["parameters"]["required"] == ["result_sets"]


class TestRegistration:
    def test_registered_in_web_toolset(self):
        from tools.registry import registry, discover_builtin_tools
        import toolsets

        discover_builtin_tools()
        assert registry._tools.get("rank_sources") is not None
        assert "rank_sources" in toolsets.resolve_toolset("web")
