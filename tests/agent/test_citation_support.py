"""Tests for the citation-SUPPORT verification pass (A1).

Two layers, matching the implementation split:

* pure claim/citation/source plumbing in ``agent/research_citations.py``
  (claim↔URL pairing, source-content collection, excerpt selection);
* the bounded, fail-open aux-LLM orchestration in
  ``agent/verification_stop.py`` (mocked aux client — support / contradict /
  unavailable / timeout — never a live call).
"""

import asyncio
import json

import pytest

import agent.verification_stop as vs
from agent.research_citations import (
    collect_source_contents,
    extract_claim_citation_pairs,
    interleave_pairs_by_url,
    parse_sources_map,
    select_support_excerpt,
)


def _extract_msg(*pages):
    """A web_extract tool result: pages = (url, content) tuples."""
    return {
        "role": "tool",
        "name": "web_extract",
        "content": json.dumps(
            {"results": [{"url": u, "title": "t", "content": c} for u, c in pages]}
        ),
    }


def _search_msg(*urls):
    return {
        "role": "tool",
        "name": "web_search",
        "content": json.dumps(
            {"results": [{"title": "t", "url": u, "description": "snippet"} for u in urls]}
        ),
    }


_ANSWER = (
    "## Findings\n\n"
    "- Rust 1.0 was released in May 2015 under Mozilla's sponsorship. [1]\n"
    "- IPv4 offers roughly 4.3 billion addresses because addresses are 32-bit. [2]\n\n"
    "The Brave Search free tier allows about 2000 queries per month according to "
    "https://c.com/brave-pricing which documents the quota.\n\n"
    "## Sources\n"
    "[1] https://a.com/rust\n"
    "[2] https://b.com/ipv4\n"
)


class TestSourcesMap:
    def test_parses_plain_and_colon_forms(self):
        text = "[1] https://a.com/x\n[2]: https://b.com/y\nnot a source line"
        assert parse_sources_map(text) == {1: "https://a.com/x", 2: "https://b.com/y"}

    def test_first_binding_wins(self):
        text = "[1] https://a.com\n[1] https://evil.com"
        assert parse_sources_map(text) == {1: "https://a.com"}


class TestClaimCitationPairs:
    def test_markers_resolve_through_sources_section(self):
        pairs = extract_claim_citation_pairs(_ANSWER)
        by_url = {u: c for c, u in pairs}
        assert "https://a.com/rust" in by_url and "Mozilla" in by_url["https://a.com/rust"]
        assert "https://b.com/ipv4" in by_url and "32-bit" in by_url["https://b.com/ipv4"]

    def test_inline_urls_pair_with_their_paragraph(self):
        pairs = extract_claim_citation_pairs(_ANSWER)
        inline = [c for c, u in pairs if u.startswith("https://c.com")]
        assert inline and "2000 queries" in inline[0]
        # The URL itself is stripped from the claim text shown to the judge.
        assert "https://c.com" not in inline[0]

    def test_bibliography_lines_are_not_claims(self):
        pairs = extract_claim_citation_pairs(_ANSWER)
        assert all("## Sources" not in c for c, _ in pairs)
        # No pair whose claim is just a bare source listing.
        assert all(len(c) >= 30 for c, _ in pairs)

    def test_markdown_links_count_as_citations(self):
        text = (
            "The framework retired its supervisor architecture in favor of a "
            "single loop, per [the changelog](https://d.com/changelog) notes.\n"
        )
        pairs = extract_claim_citation_pairs(text)
        assert [u for _, u in pairs] == ["https://d.com/changelog"]
        assert "changelog" in pairs[0][0]  # link text kept in the claim

    def test_uncited_prose_produces_no_pairs(self):
        assert extract_claim_citation_pairs("Just an opinion, no sources here.") == []

    def test_dedupes_repeated_claim_url(self):
        text = (
            "- The quota is 2000 per month per the docs https://a.com/q and "
            "again https://a.com/q for emphasis.\n"
        )
        pairs = extract_claim_citation_pairs(text)
        assert len(pairs) == 1

    def test_code_fences_are_skipped(self):
        text = "```\ncurl https://a.com/api this is code not a claim at all\n```\n"
        assert extract_claim_citation_pairs(text) == []


class TestInterleave:
    def test_round_robin_across_urls(self):
        pairs = [
            ("a1", "https://a.com"), ("a2", "https://a.com"), ("a3", "https://a.com"),
            ("b1", "https://b.com"), ("c1", "https://c.com"),
        ]
        order = [c for c, _ in interleave_pairs_by_url(pairs)]
        assert order[:3] == ["a1", "b1", "c1"]  # budget of 3 covers 3 sources
        assert sorted(order) == sorted(c for c, _ in pairs)


class TestCollectSourceContents:
    def test_maps_extracted_pages_by_canonical_url(self):
        msgs = [_extract_msg(("https://a.com/rust/", "Rust 1.0 shipped in 2015."))]
        contents = collect_source_contents(msgs)
        assert contents == {"https://a.com/rust": "Rust 1.0 shipped in 2015."}

    def test_search_snippets_are_not_source_content(self):
        msgs = [_search_msg("https://a.com/rust")]
        assert collect_source_contents(msgs) == {}

    def test_longest_body_wins_on_duplicate_extracts(self):
        msgs = [
            _extract_msg(("https://a.com", "short")),
            _extract_msg(("https://a.com", "a much longer extracted body")),
        ]
        assert collect_source_contents(msgs)["https://a.com"] == "a much longer extracted body"

    def test_non_json_tool_content_is_ignored(self):
        msgs = [{"role": "tool", "name": "web_extract", "content": "plain text, no json"}]
        assert collect_source_contents(msgs) == {}


class TestSelectSupportExcerpt:
    def test_short_content_returned_whole(self):
        assert select_support_excerpt("tiny body", "any claim", 100) == "tiny body"

    def test_picks_paragraph_overlapping_the_claim(self):
        filler = "irrelevant padding paragraph about weather patterns. " * 20
        target = "Mozilla sponsored Rust and version 1.0 released in 2015."
        content = "\n\n".join([filler, filler, target, filler])
        out = select_support_excerpt(content, "Rust 1.0 was released in 2015 by Mozilla", 300)
        assert "Mozilla sponsored Rust" in out
        assert len(out) <= 300

    def test_no_overlap_falls_back_to_head(self):
        content = ("head paragraph first. " * 30 + "\n\n" + "tail paragraph. " * 30)
        out = select_support_excerpt(content, "zzz qqqq xxxxx unrelated", 200)
        assert out.startswith("head paragraph")


class TestClassifyVerdict:
    @pytest.mark.parametrize("raw,expected", [
        ("SUPPORTED", "SUPPORTED"),
        ("supported.", "SUPPORTED"),
        ("NOT_SUPPORTED", "NOT_SUPPORTED"),
        ("not supported", "NOT_SUPPORTED"),
        ("Unsupported claim", "NOT_SUPPORTED"),
        ("CONTRADICTED", "CONTRADICTED"),
        ("The excerpt contradicts the claim", "CONTRADICTED"),
        ("PARTIAL", "PARTIAL"),
        ("", "UNKNOWN"),
        (None, "UNKNOWN"),
        ("no idea", "UNKNOWN"),
    ])
    def test_mapping(self, raw, expected):
        assert vs.classify_support_verdict(raw) == expected


# ── orchestration ──────────────────────────────────────────────────────────

_CFG_ON = {"research": {"verify_citations": True, "verify_citation_support": True,
                        "citation_support_max_checks": 12}}


def _msgs_with_content():
    return [
        _search_msg("https://a.com/rust", "https://b.com/ipv4", "https://c.com/brave-pricing"),
        _extract_msg(
            ("https://a.com/rust", "Mozilla sponsored Rust; 1.0 released May 2015."),
            ("https://b.com/ipv4", "IPv4 uses 32-bit addresses: ~4.3 billion total."),
            ("https://c.com/brave-pricing", "Brave free plan: 2000 queries/month."),
        ),
    ]


def _aux(reply="SUPPORTED", *, per_claim=None):
    """Fake aux client. per_claim maps a claim substring → reply."""
    async def call(system, user):
        for needle, verdict in (per_claim or {}).items():
            if needle in user:
                return verdict
        return reply
    return call


class TestRunCitationSupportCheck:
    def test_all_supported_no_revise(self):
        report = vs.run_citation_support_check(
            _ANSWER, _msgs_with_content(), config=_CFG_ON, aux_call=_aux("SUPPORTED"))
        assert report["status"] == "checked"
        assert report["checked"] == 3
        assert report["should_revise"] is False
        assert vs.render_citation_support_nudge(report) is None

    def test_contradicted_claim_triggers_revise(self):
        report = vs.run_citation_support_check(
            _ANSWER, _msgs_with_content(), config=_CFG_ON,
            aux_call=_aux(per_claim={"32-bit": "CONTRADICTED"}))
        assert report["should_revise"] is True
        assert len(report["contradicted"]) == 1
        nudge = vs.render_citation_support_nudge(report)
        assert nudge and "CONTRADICTED" in nudge and "b.com" in nudge

    def test_single_unsupported_is_tolerated(self):
        report = vs.run_citation_support_check(
            _ANSWER, _msgs_with_content(), config=_CFG_ON,
            aux_call=_aux(per_claim={"32-bit": "NOT_SUPPORTED"}))
        assert report["status"] == "checked"
        assert report["should_revise"] is False  # excerpt noise, not a pattern

    def test_two_unsupported_trigger_revise(self):
        report = vs.run_citation_support_check(
            _ANSWER, _msgs_with_content(), config=_CFG_ON,
            aux_call=_aux(per_claim={"32-bit": "NOT_SUPPORTED", "Mozilla": "NOT_SUPPORTED"}))
        assert report["should_revise"] is True
        nudge = vs.render_citation_support_nudge(report)
        assert nudge and "does NOT contain" in nudge

    def test_aux_unavailable_skips_with_structured_note(self, monkeypatch):
        monkeypatch.setattr(vs, "_build_support_aux_call", lambda: None)
        report = vs.run_citation_support_check(
            _ANSWER, _msgs_with_content(), config=_CFG_ON, aux_call=None)
        assert report["status"] == "skipped"
        assert report["reason"] == "aux_unavailable"
        assert any("fail-open" in n for n in report["notes"])
        assert vs.render_citation_support_nudge(report) is None  # never blocks

    def test_per_check_timeout_fails_open_as_unknown(self, monkeypatch):
        monkeypatch.setattr(vs, "_SUPPORT_PER_CHECK_TIMEOUT_S", 0.05)

        async def slow(system, user):
            await asyncio.sleep(0.5)
            return "CONTRADICTED"

        report = vs.run_citation_support_check(
            _ANSWER, _msgs_with_content(), config=_CFG_ON, aux_call=slow)
        assert report["status"] == "checked"
        assert all(v["verdict"] == "UNKNOWN" for v in report["verdicts"])
        assert report["should_revise"] is False  # inconclusive never flags

    def test_aux_exception_fails_open(self):
        async def boom(system, user):
            raise RuntimeError("aux exploded")

        report = vs.run_citation_support_check(
            _ANSWER, _msgs_with_content(), config=_CFG_ON, aux_call=boom)
        assert report["status"] == "checked"
        assert report["should_revise"] is False

    def test_max_checks_caps_the_pass(self):
        bullets = "\n".join(
            f"- Long enough factual claim number {i} about provider quotas today. "
            f"[{i}]" for i in range(1, 21)
        )
        sources = "\n".join(f"[{i}] https://s{i}.com/page" for i in range(1, 21))
        answer = f"{bullets}\n\n## Sources\n{sources}\n"
        msgs = [_extract_msg(*[(f"https://s{i}.com/page", f"body {i}") for i in range(1, 21)])]
        calls = []

        async def counting(system, user):
            calls.append(user)
            return "SUPPORTED"

        cfg = {"research": {"citation_support_max_checks": 5}}
        report = vs.run_citation_support_check(answer, msgs, config=cfg, aux_call=counting)
        assert report["checked"] == 5 and len(calls) == 5

    def test_no_extracted_content_skips(self):
        # Sources only ever seen in SEARCH results → nothing to judge against.
        msgs = [_search_msg("https://a.com/rust", "https://b.com/ipv4", "https://c.com/brave-pricing")]
        report = vs.run_citation_support_check(
            _ANSWER, msgs, config=_CFG_ON, aux_call=_aux())
        assert report["status"] == "skipped"
        assert report["reason"] == "no_source_content"

    def test_uncited_answer_skips(self):
        report = vs.run_citation_support_check(
            "No citations in this answer at all.", _msgs_with_content(),
            config=_CFG_ON, aux_call=_aux())
        assert report["reason"] == "no_claim_citation_pairs"


class TestStopPathIntegration:
    """build_research_citation_nudge = membership check, then support pass."""

    def _final(self, text=_ANSWER):
        return {"role": "assistant", "content": text}

    def test_fabricated_url_still_wins_over_support_pass(self, monkeypatch):
        called = []
        monkeypatch.setattr(
            vs, "run_citation_support_check",
            lambda *a, **k: called.append(1) or {"status": "skipped"})
        answer = _ANSWER + "\nAlso see https://never-retrieved.com/page for more.\n"
        nudge = vs.build_research_citation_nudge(
            final_msg=self._final(answer), messages=_msgs_with_content(),
            attempts=0, config=_CFG_ON)
        assert nudge and "never retrieved" in nudge  # membership nudge
        assert not called  # support pass not reached

    def test_clean_membership_runs_support_and_nudges(self, monkeypatch):
        fake_report = {
            "status": "checked", "should_revise": True, "checked": 1,
            "verdicts": [], "notes": [],
            "contradicted": [{"claim": "IPv4 claim", "url": "https://b.com/ipv4",
                              "verdict": "CONTRADICTED"}],
            "unsupported": [],
        }
        monkeypatch.setattr(vs, "run_citation_support_check", lambda *a, **k: fake_report)
        nudge = vs.build_research_citation_nudge(
            final_msg=self._final(), messages=_msgs_with_content(),
            attempts=0, config=_CFG_ON)
        assert nudge and "citation support check" in nudge

    def test_support_disabled_by_config(self, monkeypatch):
        monkeypatch.setattr(
            vs, "run_citation_support_check",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run")))
        cfg = {"research": {"verify_citations": True, "verify_citation_support": False}}
        assert vs.build_research_citation_nudge(
            final_msg=self._final(), messages=_msgs_with_content(),
            attempts=0, config=cfg) is None

    def test_support_disabled_by_env(self, monkeypatch):
        monkeypatch.setenv("HERMES_VERIFY_CITATION_SUPPORT", "0")
        monkeypatch.setattr(
            vs, "run_citation_support_check",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run")))
        assert vs.build_research_citation_nudge(
            final_msg=self._final(), messages=_msgs_with_content(),
            attempts=0, config=_CFG_ON) is None

    def test_attempt_budget_shared_with_membership_check(self, monkeypatch):
        monkeypatch.setattr(
            vs, "run_citation_support_check",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run")))
        assert vs.build_research_citation_nudge(
            final_msg=self._final(), messages=_msgs_with_content(),
            attempts=1, max_attempts=1, config=_CFG_ON) is None

    def test_no_web_research_is_inert(self, monkeypatch):
        monkeypatch.setattr(
            vs, "run_citation_support_check",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run")))
        assert vs.build_research_citation_nudge(
            final_msg=self._final("Claim citing https://a.com/rust in passing text here."),
            messages=[], attempts=0, config=_CFG_ON) is None

    def test_support_check_exception_never_blocks(self, monkeypatch):
        monkeypatch.setattr(
            vs, "run_citation_support_check",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        assert vs.build_research_citation_nudge(
            final_msg=self._final(), messages=_msgs_with_content(),
            attempts=0, config=_CFG_ON) is None


class TestSupportConfig:
    def test_defaults(self):
        from hermes_cli.config import DEFAULT_CONFIG
        r = DEFAULT_CONFIG["research"]
        assert r["verify_citation_support"] is True
        assert r["citation_support_max_checks"] == 12

    def test_enabled_helpers(self):
        assert vs.citation_support_enabled({"research": {"verify_citation_support": False}}) is False
        assert vs.citation_support_enabled({"research": {}}) is True
        assert vs.citation_support_max_checks({"research": {"citation_support_max_checks": 3}}) == 3
        assert vs.citation_support_max_checks({"research": {"citation_support_max_checks": -5}}) == 0
        assert vs.citation_support_max_checks({"research": {}}) == vs._SUPPORT_MAX_CHECKS_DEFAULT

    def test_runs_inside_a_running_event_loop(self):
        # The stop path may be reached from async contexts; the sync bridge
        # must not deadlock or raise "asyncio.run in running loop".
        async def outer():
            return vs.run_citation_support_check(
                _ANSWER, _msgs_with_content(), config=_CFG_ON, aux_call=_aux())

        report = asyncio.run(outer())
        assert report["status"] == "checked" and report["checked"] == 3
