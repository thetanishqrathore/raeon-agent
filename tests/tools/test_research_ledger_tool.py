"""Tests for the research_ledger tool (P1.3).

Isolated from the real HERMES_HOME via the HERMES_HOME env var so the ledger DB
lands in a tmp dir. Session ids are unique per test.
"""

import json

import pytest

from tools.research_ledger_tool import (
    research_ledger,
    check_research_ledger_requirements,
    RESEARCH_LEDGER_SCHEMA,
)


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    yield


class TestActions:
    def test_set_brief_then_get(self):
        assert json.loads(research_ledger({"action": "set_brief", "brief": "Brief X"}, session_id="t1"))["ok"]
        digest = research_ledger({"action": "get"}, session_id="t1")
        assert "Brief X" in digest

    def test_add_learning(self):
        out = json.loads(research_ledger(
            {"action": "add_learning", "claim": "Fact", "source_url": "https://a.com", "confidence": "high"},
            session_id="t2",
        ))
        assert out["ok"]
        assert "Fact" in research_ledger({"action": "get"}, session_id="t2")

    def test_add_visited_new_then_dup(self):
        first = json.loads(research_ledger({"action": "add_visited", "url": "https://a.com/p"}, session_id="t3"))
        dup = json.loads(research_ledger({"action": "add_visited", "url": "https://a.com/p"}, session_id="t3"))
        assert first["new"] is True
        assert dup["new"] is False

    def test_add_claim(self):
        out = json.loads(research_ledger(
            {"action": "add_claim", "claim": "C", "status": "supported"}, session_id="t4"
        ))
        assert out["ok"]

    def test_clear(self):
        research_ledger({"action": "set_brief", "brief": "B"}, session_id="t5")
        assert json.loads(research_ledger({"action": "clear"}, session_id="t5"))["ok"]
        assert "Brief" not in research_ledger({"action": "get"}, session_id="t5")

    def test_unknown_action_is_error(self):
        assert "error" in json.loads(research_ledger({"action": "frobnicate"}, session_id="t6"))

    def test_missing_required_field_is_error(self):
        assert "error" in json.loads(research_ledger({"action": "set_brief"}, session_id="t7"))
        assert "error" in json.loads(research_ledger({"action": "add_visited"}, session_id="t7"))


class TestRegistration:
    def test_registered_in_web_toolset(self):
        from tools.registry import registry, discover_builtin_tools
        import toolsets

        discover_builtin_tools()
        assert registry._tools.get("research_ledger") is not None
        assert "research_ledger" in toolsets.resolve_toolset("web")
        assert "research_ledger" in toolsets.resolve_toolset("hermes-cli")

    def test_requirements_and_schema(self):
        assert check_research_ledger_requirements() is True
        assert RESEARCH_LEDGER_SCHEMA["name"] == "research_ledger"
        assert RESEARCH_LEDGER_SCHEMA["parameters"]["required"] == ["action"]
