"""Tests for the think tool (forced reflection turn).

The think tool is a deliberately side-effect-free reflection primitive. These
tests pin three things: (1) it returns a clean acknowledgement with zero side
effects, (2) it is discoverable and lands in the `web` toolset, and (3) its
schema is surfaced to the model under the expected name.
"""

import json

from tools.think_tool import think_tool, check_think_requirements, THINK_SCHEMA


class TestThinkHandler:
    def test_acknowledges_reflection(self):
        out = json.loads(
            think_tool("Vendor docs confirm the price; the 2026 tier is unverified.")
        )
        assert out["acknowledged"] is True
        assert "2026 tier" in out["reflection"]
        # No next_step provided -> key absent.
        assert "next_step" not in out

    def test_includes_next_step_when_given(self):
        out = json.loads(
            think_tool("Two sources agree on the date.", next_step="synthesize the answer")
        )
        assert out["next_step"] == "synthesize the answer"

    def test_empty_reflection_is_error(self):
        out = json.loads(think_tool("   "))
        assert "error" in out
        assert "acknowledged" not in out

    def test_short_reflection_gets_soft_note_not_error(self):
        out = json.loads(think_tool("ok"))
        # Soft nudge, never a hard failure — the loop must keep moving.
        assert out["acknowledged"] is True
        assert "note" in out

    def test_strips_whitespace(self):
        out = json.loads(think_tool("  padded reflection text  "))
        assert out["reflection"] == "padded reflection text"

    def test_requirements_always_available(self):
        assert check_think_requirements() is True

    def test_schema_shape(self):
        assert THINK_SCHEMA["name"] == "think"
        props = THINK_SCHEMA["parameters"]["properties"]
        assert "reflection" in props
        assert "next_step" in props
        assert THINK_SCHEMA["parameters"]["required"] == ["reflection"]


class TestThinkRegistration:
    def test_registered_in_web_toolset(self):
        from tools.registry import registry, discover_builtin_tools

        discover_builtin_tools()
        entry = registry._tools.get("think")
        assert entry is not None
        assert entry.toolset == "web"
        assert entry.is_async is False

    def test_resolves_into_web_toolset(self):
        import toolsets

        assert "think" in toolsets.resolve_toolset("web")

    def test_definition_surfaced_to_model(self):
        from tools.registry import registry, discover_builtin_tools

        discover_builtin_tools()
        defs = registry.get_definitions({"think"})
        assert defs and defs[0]["function"]["name"] == "think"

    def test_handler_dispatch_returns_ack(self):
        from tools.registry import registry, discover_builtin_tools

        discover_builtin_tools()
        out = json.loads(registry.dispatch("think", {"reflection": "gap analysis here"}))
        assert out["acknowledged"] is True
