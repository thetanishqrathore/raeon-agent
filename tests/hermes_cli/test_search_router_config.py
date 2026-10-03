"""Tests for the web.search_router config block (R4)."""

from hermes_cli.config import DEFAULT_CONFIG, load_config


class TestDefaults:
    def test_block_present(self):
        sr = DEFAULT_CONFIG["web"]["search_router"]
        names = [p["name"] for p in sr["providers"]]
        assert names == ["searxng", "ddgs", "brave-free", "tavily", "google-cse"]
        assert sr["merge_uncapped"] is False
        assert sr["rate_limit_cooldown_seconds"] == 30
        assert sr["max_cooldown_seconds"] == 900
        # EMPTY gets its own flat, much-shorter cooldown than real failures.
        assert sr["empty_cooldown_s"] == 30
        # Persistent cross-run search cache (A7).
        assert sr["cache_ttl_s"] == 86400
        assert sr["cache_max_entries"] == 512

    def test_provider_cfg_preserves_zero_empty_cooldown(self, monkeypatch):
        from plugins.web.resilient import provider as rp
        monkeypatch.setattr(
            "hermes_cli.config.load_config",
            lambda: {"web": {"search_router": {"empty_cooldown_s": 0}}},
        )
        assert rp.ResilientSearchProvider()._cfg()["empty_cooldown_s"] == 0.0

    def test_caps_preserve_quota_order(self):
        sr = DEFAULT_CONFIG["web"]["search_router"]
        caps = {p["name"]: (p["daily_cap"], p["monthly_cap"]) for p in sr["providers"]}
        # uncapped first two, capped fallbacks
        assert caps["searxng"] == (0, 0) and caps["ddgs"] == (0, 0)
        assert caps["brave-free"] == (0, 2000)
        assert caps["google-cse"] == (100, 0)

    def test_load_config_merges(self):
        cfg = load_config()
        assert "search_router" in cfg["web"]
        assert isinstance(cfg["web"]["search_router"]["providers"], list)


class TestProviderReadsConfig:
    def test_provider_falls_back_to_defaults_when_absent(self, monkeypatch):
        from plugins.web.resilient import provider as rp
        monkeypatch.setattr("hermes_cli.config.load_config", lambda: {})
        cfg = rp.ResilientSearchProvider()._cfg()
        assert [t["name"] for t in cfg["tiers"]] == [t["name"] for t in rp._DEFAULT_TIERS]

    def test_provider_uses_configured_tiers(self, monkeypatch):
        from plugins.web.resilient import provider as rp
        monkeypatch.setattr(
            "hermes_cli.config.load_config",
            lambda: {"web": {"search_router": {"providers": [{"name": "ddgs", "daily_cap": 0, "monthly_cap": 0}],
                                               "merge_uncapped": True}}},
        )
        cfg = rp.ResilientSearchProvider()._cfg()
        assert [t["name"] for t in cfg["tiers"]] == ["ddgs"]
        assert cfg["merge_uncapped"] is True
