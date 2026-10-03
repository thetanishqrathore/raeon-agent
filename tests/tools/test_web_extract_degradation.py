#!/usr/bin/env python3
"""Tests for web_extract's targeted-extraction degradation markers.

The grounding contract: when `query`/`schema` extraction is requested, the
result the model sees must either BE a distilled answer/structured object, or
LOUDLY say it isn't. The failure mode this guards against is silently returning
raw page markdown as if it were a distilled answer when no auxiliary model is
configured (or when distillation finds nothing / errors).

Run with:  python -m pytest tests/tools/test_web_extract_degradation.py -v
"""

import asyncio
import json
from unittest import mock

import tools.web_tools as wt


_RAW = "# Heading\n\nSome raw page markdown body that was never distilled.\n"


class _StubProvider:
    name = "stub"
    display_name = "Stub"

    def supports_extract(self):
        return True

    def extract(self, urls, format=None):
        return [
            {"url": u, "title": "Stub Title", "content": _RAW, "raw_content": _RAW}
            for u in urls
        ]


async def _safe_url(_url):
    return True


def _mk_async(val):
    async def _f(*_a, **_k):
        return val
    return _f


def _run(*, query=None, schema=None, aux=True, answer="DISTILLED ANSWER", structured=None):
    """Drive web_extract_tool with the network + aux layers stubbed out."""
    import plugins.web.hermes.scraper.llm_extract as llmx

    structured = structured if structured is not None else {"data": {"k": "v"}}
    with mock.patch.object(wt, "async_is_safe_url", _safe_url), \
         mock.patch.object(wt, "_ensure_web_plugins_loaded", lambda: None), \
         mock.patch.object(wt, "_get_extract_backend", lambda: ""), \
         mock.patch.object(wt, "check_auxiliary_model", lambda: aux), \
         mock.patch("agent.web_search_registry.get_active_extract_provider",
                    lambda: _StubProvider()), \
         mock.patch.object(llmx, "extract_answer", new=_mk_async(answer)), \
         mock.patch.object(llmx, "extract_structured", new=_mk_async(structured)):
        out = asyncio.run(
            wt.web_extract_tool(["https://example.com/a"], "markdown",
                                query=query, schema=schema)
        )
    return json.loads(out)["results"][0]


class TestNoAuxModelDegradation:
    def test_query_without_aux_is_marked_degraded(self):
        r = _run(query="what is X?", aux=False)
        assert r["extraction_status"] == "degraded_no_aux_model"
        assert "did NOT run" in r["extraction_note"]
        assert "auxiliary" in r["extraction_note"].lower()
        # Raw content is preserved as the safety net.
        assert "raw page markdown" in r["content"]

    def test_schema_without_aux_is_marked_degraded(self):
        r = _run(schema={"type": "object"}, aux=False)
        assert r["extraction_status"] == "degraded_no_aux_model"
        assert "schema" in r["extraction_note"].lower()

    def test_degraded_note_points_at_config_recipe(self):
        r = _run(query="q", aux=False)
        assert "auxiliary.web_extract" in r["extraction_note"]


class TestAuxAvailableMarkers:
    def test_query_happy_path_has_no_degradation_marker(self):
        r = _run(query="what is X?", aux=True, answer="DISTILLED ANSWER")
        assert r["content"] == "DISTILLED ANSWER"
        assert "extraction_status" not in r
        assert "extraction_note" not in r

    def test_query_empty_answer_is_marked(self):
        r = _run(query="what is X?", aux=True, answer="")
        assert "extraction_note" in r
        assert "no passage" in r["extraction_note"]
        # Falls back to raw content, not an empty string.
        assert "raw page markdown" in r["content"]

    def test_schema_validation_miss_is_marked(self):
        r = _run(schema={"type": "object"}, aux=True,
                 structured={"data": {"k": "v"}, "schema_warning": "missing field"})
        assert "extraction_note" in r
        assert "validate" in r["extraction_note"].lower()

    def test_schema_happy_path_has_no_marker(self):
        r = _run(schema={"type": "object"}, aux=True, structured={"data": {"k": "v"}})
        assert json.loads(r["content"]) == {"k": "v"}
        assert "extraction_note" not in r
