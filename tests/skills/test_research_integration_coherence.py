"""Integration coherence (I3): the layers that drive autonomous tool selection
must stay consistent. If someone edits the guidance, skill, toolsets, or backend
selection and breaks the cross-references, these fail loudly.

The chain under test:
  WEB_RESEARCH_QUALITY_GUIDANCE  -> points to the deep-research skill + think
  deep-research SKILL.md         -> names every research tool, hides backends
  _HERMES_CORE_TOOLS / web toolset -> the tools are actually reachable
  registry                       -> resilient + google-cse registered
  web_tools._is_backend_available-> "resilient" is selectable
  config                         -> web.search_router present
"""

from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1].parent
_SKILL = _REPO / "skills/research/deep-research/SKILL.md"
_RESEARCH_TOOLS = ["think", "rank_sources", "research_ledger", "delegate_task",
                   "web_search", "web_extract"]
# Search backends must NEVER be named in the skill — the router owns selection.
_BACKEND_NAMES = ["searxng", "brave", "ddgs", "tavily", "google-cse", "duckduckgo"]


class TestGuidanceLayer:
    def test_guidance_points_to_skill_and_think(self):
        from agent.prompt_builder import WEB_RESEARCH_QUALITY_GUIDANCE as g
        assert "deep-research" in g
        assert "think" in g


class TestSkillLayer:
    def test_skill_names_all_research_tools(self):
        text = _SKILL.read_text(encoding="utf-8")
        for t in _RESEARCH_TOOLS:
            assert t in text, f"skill no longer mentions {t}"

    def test_skill_hides_search_backends(self):
        # Backends are an implementation detail of the router; the model must
        # not be told to pick one. (Firecrawl appears only as methodology
        # provenance, so it's excluded from this check.)
        text = _SKILL.read_text(encoding="utf-8").lower()
        for b in _BACKEND_NAMES:
            assert b not in text, f"skill leaks a search backend name: {b}"

    def test_skill_tool_inventory_complete(self):
        # The "Tool-usage summary" section must list the full toolset.
        text = _SKILL.read_text(encoding="utf-8")
        summary = text.split("## Tool-usage summary", 1)[-1].split("\n## ", 1)[0]
        for t in ["think", "rank_sources", "research_ledger", "delegate_task"]:
            assert t in summary, f"tool inventory missing {t}"


class TestToolsReachable:
    def test_core_tools_include_research_aids(self):
        from toolsets import _HERMES_CORE_TOOLS
        for t in ["think", "rank_sources", "research_ledger", "web_search", "web_extract"]:
            assert t in _HERMES_CORE_TOOLS, f"{t} not in core tools"

    def test_web_toolset_resolves_research_aids(self):
        from tools.registry import discover_builtin_tools
        import toolsets
        discover_builtin_tools()
        web = toolsets.resolve_toolset("web")
        for t in ["think", "rank_sources", "research_ledger"]:
            assert t in web, f"{t} not resolved in web toolset"


class TestSearchStack:
    def test_router_and_cse_registered(self):
        from hermes_cli.plugins import _ensure_plugins_discovered
        from agent.web_search_registry import get_provider
        _ensure_plugins_discovered()
        assert get_provider("resilient") is not None
        assert get_provider("google-cse") is not None

    def test_resilient_backend_is_selectable(self):
        from tools.web_tools import _is_backend_available
        assert _is_backend_available("resilient") is True

    def test_shared_backend_config_honors_resilient(self, monkeypatch):
        # `web.backend: resilient` must select the router, not silently fall
        # back to key auto-detect (which would bypass the router entirely).
        from tools import web_tools
        monkeypatch.setattr(web_tools, "_load_web_config", lambda: {"backend": "resilient"})
        assert web_tools._get_backend() == "resilient"
        # ... and the tool-availability gate must light up for it too.
        assert web_tools.check_web_api_key() is True

    def test_shared_backend_config_honors_google_cse(self, monkeypatch):
        from tools import web_tools
        monkeypatch.setattr(web_tools, "_load_web_config", lambda: {"backend": "google-cse"})
        monkeypatch.setenv("GOOGLE_CSE_API_KEY", "k")
        monkeypatch.setenv("GOOGLE_CSE_CX", "cx")
        assert web_tools._get_backend() == "google-cse"
        assert web_tools.check_web_api_key() is True

    def test_search_router_config_present(self):
        from hermes_cli.config import DEFAULT_CONFIG
        sr = DEFAULT_CONFIG["web"]["search_router"]
        names = [p["name"] for p in sr["providers"]]
        # All four tiers represented (+ tavily).
        for tier in ["searxng", "ddgs", "brave-free", "google-cse"]:
            assert tier in names
