"""Tests for the research.* config namespace (P1.1).

All defaults must preserve current behavior, the keys must be present and
correctly typed, and verify_citations must drive the citation gate.
"""

from hermes_cli.config import DEFAULT_CONFIG, load_config


_EXPECTED_KEYS = {
    "effort", "max_rounds", "max_parallel", "max_fetches",
    "wall_clock_seconds", "verify", "verify_citations",
    "verify_citation_support", "citation_support_max_checks",
}


class TestResearchDefaults:
    def test_namespace_present_with_expected_keys(self):
        research = DEFAULT_CONFIG["research"]
        assert _EXPECTED_KEYS <= set(research.keys())

    def test_default_values(self):
        r = DEFAULT_CONFIG["research"]
        assert r["effort"] == "detailed"
        assert r["verify"] is True
        assert r["verify_citations"] is True
        assert r["wall_clock_seconds"] == 0  # off by default → no behavior change
        assert isinstance(r["max_rounds"], int)
        assert isinstance(r["max_parallel"], int)
        assert isinstance(r["max_fetches"], int)
        # Semantic citation-support pass (A1): on by default, bounded checks.
        assert r["verify_citation_support"] is True
        assert r["citation_support_max_checks"] == 12

    def test_load_config_merges_research(self):
        cfg = load_config()
        assert "research" in cfg
        assert isinstance(cfg["research"].get("verify_citations"), bool)


class TestVerifyCitationsWiring:
    def test_default_config_enables_citation_gate(self):
        from agent.verification_stop import research_citation_check_enabled

        assert research_citation_check_enabled(config=DEFAULT_CONFIG) is True

    def test_config_toggle_disables_gate(self):
        from agent.verification_stop import research_citation_check_enabled

        assert research_citation_check_enabled(
            config={"research": {"verify_citations": False}}
        ) is False
