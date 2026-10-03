"""Tests for the machine-checked citation/provenance validator.

Covers: URL canonicalization, citation extraction, retrieved-set collection
from tool messages, the fabricated/uncorroborated split, deterministic
renumbering, and the warning-then-revise nudge (which must never block a clean
final answer or spin past its attempt budget).
"""

from agent.research_citations import (
    CitationReport,
    assign_source_ids,
    build_citation_revise_nudge,
    canonicalize_url,
    collect_known_urls,
    extract_urls,
    renumber_citations,
    validate_answer_citations,
    web_research_occurred,
)


def _extract_msg(*urls):
    """A web_extract tool result that retrieved *urls*."""
    results = [{"url": u, "title": "T", "content": "body"} for u in urls]
    import json
    return {"role": "tool", "name": "web_extract", "content": json.dumps({"results": results})}


def _search_msg(*urls):
    import json
    results = [{"url": u, "title": "T", "description": "d"} for u in urls]
    return {"role": "tool", "name": "web_search", "content": json.dumps({"results": results})}


class TestCanonicalize:
    def test_strips_tracking_and_fragment_and_trailing_slash(self):
        a = canonicalize_url("https://Example.com/Path/?utm_source=x&id=5#frag")
        b = canonicalize_url("https://example.com/Path?id=5")
        assert a == b

    def test_default_port_normalized(self):
        assert canonicalize_url("https://example.com:443/p") == canonicalize_url("https://example.com/p")

    def test_trailing_punctuation_dropped(self):
        assert canonicalize_url("https://example.com/p.") == canonicalize_url("https://example.com/p")

    def test_unparseable_is_lowercased_not_raised(self):
        assert canonicalize_url("not a url") == "not a url"
        assert canonicalize_url("") == ""


class TestExtractUrls:
    def test_bare_and_markdown_links(self):
        text = "See https://a.com/x and [docs](https://b.com/y)."
        urls = extract_urls(text)
        assert "https://a.com/x" in urls
        assert "https://b.com/y" in urls

    def test_dedup_preserves_order(self):
        text = "https://a.com https://b.com https://a.com"
        assert extract_urls(text) == ["https://a.com", "https://b.com"]

    def test_trailing_paren_not_captured(self):
        # Markdown link close-paren must not become part of the URL.
        assert extract_urls("([link](https://a.com/p))")[0] == "https://a.com/p"

    def test_balanced_parens_inside_url_kept(self):
        # Wiki-style URLs contain "(...)"; the closing paren belongs to the URL.
        wiki = "https://en.wikipedia.org/wiki/Mercury_(element)"
        assert extract_urls(f"See {wiki} for details.") == [wiki]
        # ... including when the whole URL is itself wrapped in prose parens.
        assert extract_urls(f"(see {wiki})") == [wiki]
        # ... and as a markdown link target.
        assert extract_urls(f"[Hg]({wiki})") == [wiki]

    def test_paren_url_cited_after_extract_not_fabricated(self):
        wiki = "https://en.wikipedia.org/wiki/Mercury_(element)"
        msgs = [_extract_msg(wiki)]
        rep = validate_answer_citations(f"Mercury is a metal ({wiki}).", msgs)
        assert rep.ok and rep.fabricated == []


class TestCollectKnownUrls:
    def test_extract_populates_known_and_extracted(self):
        msgs = [_extract_msg("https://a.com/x")]
        known, extracted = collect_known_urls(msgs)
        assert canonicalize_url("https://a.com/x") in known
        assert canonicalize_url("https://a.com/x") in extracted

    def test_search_populates_known_only(self):
        msgs = [_search_msg("https://s.com/x")]
        known, extracted = collect_known_urls(msgs)
        c = canonicalize_url("https://s.com/x")
        assert c in known
        assert c not in extracted

    def test_web_research_occurred(self):
        assert web_research_occurred([_search_msg("https://s.com")]) is True
        assert web_research_occurred([{"role": "tool", "name": "read_file", "content": "{}"}]) is False
        assert web_research_occurred([]) is False

    def test_delegate_task_urls_count_as_retrieved(self):
        # A sub-researcher's cited URLs (returned via delegate_task) are
        # legitimate provenance — citing them from the parent must NOT be
        # flagged as fabricated.
        import json
        sub = {
            "role": "tool",
            "name": "delegate_task",
            "content": json.dumps({"results": [
                {"task_index": 0, "status": "completed",
                 "summary": "Finding (https://sub.com/x)\nSources:\n- https://sub.com/x"},
            ]}),
        }
        known, extracted = collect_known_urls([sub])
        c = canonicalize_url("https://sub.com/x")
        assert c in known and c in extracted
        rep = validate_answer_citations("Per the sub-research, https://sub.com/x shows …", [sub])
        assert rep.ok and not rep.fabricated


class TestValidate:
    def test_clean_answer_passes(self):
        msgs = [_extract_msg("https://a.com/x", "https://b.com/y")]
        rep = validate_answer_citations("Per https://a.com/x the answer is 42.", msgs)
        assert rep.ok
        assert rep.fabricated == []

    def test_fabricated_url_flagged(self):
        msgs = [_extract_msg("https://a.com/x")]
        rep = validate_answer_citations("As shown in https://evil-invented.com/z, …", msgs)
        assert not rep.ok
        assert canonicalize_url("https://evil-invented.com/z") in rep.fabricated

    def test_uncorroborated_seen_in_search_not_read(self):
        msgs = [_search_msg("https://s.com/x")]
        rep = validate_answer_citations("According to https://s.com/x …", msgs)
        # Seen in search → not fabricated, but not read → uncorroborated.
        assert rep.ok
        assert canonicalize_url("https://s.com/x") in rep.uncorroborated

    def test_no_citations_has_citations_false(self):
        rep = validate_answer_citations("A plain answer with no links.", [_extract_msg("https://a.com")])
        assert rep.has_citations is False
        assert rep.ok


class TestRenumber:
    def test_collapses_to_dense_ids(self):
        urls = [f"https://s{i}.com" for i in range(1, 11)]  # markers 1..10 valid
        text = "Foo [3]. Bar [7]. Baz [3]."
        new_text, sources = renumber_citations(text, urls)
        # First-seen 3 -> 1, 7 -> 2.
        assert "[1]" in new_text and "[2]" in new_text
        assert "[3]" not in new_text and "[7]" not in new_text
        assert sources == [(1, "https://s3.com"), (2, "https://s7.com")]

    def test_unknown_marker_left_alone(self):
        new_text, sources = renumber_citations("Ref [99].", ["https://only.com"])
        assert new_text == "Ref [99]."
        assert sources == []

    def test_assign_source_ids_stable(self):
        ids = assign_source_ids(["https://a.com/", "https://a.com", "https://b.com"])
        # The two a.com spellings canonicalize equal -> one id.
        assert ids[canonicalize_url("https://a.com")] == 1
        assert ids[canonicalize_url("https://b.com")] == 2


class TestReviseNudge:
    def test_none_when_clean(self):
        msgs = [_extract_msg("https://a.com/x")]
        assert build_citation_revise_nudge("See https://a.com/x.", msgs) is None

    def test_none_when_no_research(self):
        assert build_citation_revise_nudge("See https://made-up.com.", []) is None

    def test_nudge_when_fabricated(self):
        msgs = [_extract_msg("https://a.com/x")]
        nudge = build_citation_revise_nudge("See https://fabricated.com/z.", msgs)
        assert nudge is not None
        assert "fabricated.com/z" in nudge
        assert "never retrieved" in nudge

    def test_attempt_budget_stops_nudging(self):
        msgs = [_extract_msg("https://a.com/x")]
        # Even with a fabricated URL, once attempts hit the cap we stop.
        assert build_citation_revise_nudge(
            "See https://fabricated.com/z.", msgs, attempts=1, max_attempts=1
        ) is None


class TestVerificationStopHook:
    """The policy/gating layer in verification_stop.py."""

    def test_enabled_default_true(self):
        from agent.verification_stop import research_citation_check_enabled

        assert research_citation_check_enabled(config={}) is True

    def test_config_can_disable(self):
        from agent.verification_stop import research_citation_check_enabled

        assert research_citation_check_enabled(
            config={"research": {"verify_citations": False}}
        ) is False

    def test_env_overrides_config(self, monkeypatch):
        from agent.verification_stop import research_citation_check_enabled

        monkeypatch.setenv("HERMES_VERIFY_CITATIONS", "0")
        # Env says off even though config says on.
        assert research_citation_check_enabled(
            config={"research": {"verify_citations": True}}
        ) is False

    def test_nudge_fires_on_fabricated_string_content(self):
        from agent.verification_stop import build_research_citation_nudge

        msgs = [_extract_msg("https://a.com/x")]
        final_msg = {"role": "assistant", "content": "See https://fabricated.com/z."}
        nudge = build_research_citation_nudge(final_msg=final_msg, messages=msgs, config={})
        assert nudge is not None and "fabricated.com/z" in nudge

    def test_nudge_handles_list_content(self):
        from agent.verification_stop import build_research_citation_nudge

        msgs = [_extract_msg("https://a.com/x")]
        final_msg = {
            "role": "assistant",
            "content": [{"type": "text", "text": "See https://fabricated.com/z."}],
        }
        nudge = build_research_citation_nudge(final_msg=final_msg, messages=msgs, config={})
        assert nudge is not None and "fabricated.com/z" in nudge

    def test_no_nudge_when_clean(self):
        from agent.verification_stop import build_research_citation_nudge

        msgs = [_extract_msg("https://a.com/x")]
        final_msg = {"role": "assistant", "content": "Per https://a.com/x, 42."}
        assert build_research_citation_nudge(final_msg=final_msg, messages=msgs, config={}) is None

    def test_no_nudge_when_disabled(self):
        from agent.verification_stop import build_research_citation_nudge

        msgs = [_extract_msg("https://a.com/x")]
        final_msg = {"role": "assistant", "content": "See https://fabricated.com/z."}
        assert build_research_citation_nudge(
            final_msg=final_msg, messages=msgs,
            config={"research": {"verify_citations": False}},
        ) is None
