"""Tests for cited-docs-only report assembly (P1.6)."""

from agent.research_report import (
    assemble_writer_context,
    build_sources_section,
    estimate_tokens,
    finalize_report,
    fit_docs_to_budget,
    select_cited_docs,
)


class TestSelectCitedDocs:
    def test_keeps_only_cited(self):
        docs = {"https://a.com/x": "A", "https://b.com/y": "B", "https://c.com": "C"}
        kept = select_cited_docs(docs, "We rely on https://a.com/x and https://c.com.")
        assert set(kept) == {"https://a.com/x", "https://c.com"}

    def test_canonical_match(self):
        docs = {"https://a.com/p": "A"}
        # Draft cites a tracking-param variant — must still match.
        kept = select_cited_docs(docs, "see https://a.com/p/?utm_source=x")
        assert "https://a.com/p" in kept

    def test_no_citations_keeps_nothing(self):
        assert select_cited_docs({"https://a.com": "A"}, "no links here") == {}


class TestFitToBudget:
    def test_under_budget_unchanged(self):
        docs = {"u": "short"}
        fitted, trunc = fit_docs_to_budget(docs, token_budget=1000, counter=len)
        assert fitted == docs and trunc is False

    def test_over_budget_truncates_but_keeps_all_docs(self):
        docs = {"big": "x" * 1000, "small": "y" * 50}
        fitted, trunc = fit_docs_to_budget(docs, token_budget=400, counter=len)
        assert trunc is True
        assert set(fitted) == {"big", "small"}            # no source dropped
        assert sum(len(c) for c in fitted.values()) <= 400 + 50  # ~within budget
        assert "[truncated]" in fitted["big"]

    def test_no_compounding_marker(self):
        docs = {"big": "z" * 5000}
        fitted, _ = fit_docs_to_budget(docs, token_budget=100, counter=len)
        assert fitted["big"].count("[truncated]") == 1

    def test_zero_budget_is_noop(self):
        docs = {"u": "x" * 100}
        fitted, trunc = fit_docs_to_budget(docs, token_budget=0, counter=len)
        assert fitted == docs and trunc is False


class TestAssembleWriterContext:
    def test_only_cited_docs_in_context(self):
        docs = {"https://a.com": "ALPHA", "https://b.com": "BETA"}
        out = assemble_writer_context(
            "I will compare things.", docs, "Per https://a.com …", token_budget=0
        )
        assert "ALPHA" in out["context"]
        assert "BETA" not in out["context"]
        assert out["cited_urls"] == ["https://a.com"]
        assert "I will compare things." in out["context"]


class TestFinalizeReport:
    def test_appends_sources_section(self):
        draft = "Claim one (https://a.com/x). Claim two (https://b.com/y)."
        text, sources = finalize_report(draft)
        assert "## Sources" in text
        assert sources == [(1, "https://a.com/x"), (2, "https://b.com/y")]
        assert "[1] https://a.com/x" in text

    def test_dense_renumber_of_numeric_markers(self):
        urls = [f"https://s{i}.com" for i in range(1, 11)]
        draft = "Foo [3] and bar [7]. " + " ".join(urls)  # urls present so they're "cited"
        text, _ = finalize_report(draft, append_sources=False)
        # Markers 3,7 collapse to 1,2 in appearance order.
        assert "[1]" in text and "[2]" in text

    def test_empty_draft(self):
        text, sources = finalize_report("")
        assert text == "" and sources == []

    def test_no_sources_when_none_cited(self):
        text, sources = finalize_report("A plain conclusion with no links.")
        assert "## Sources" not in text
        assert sources == []


class TestMisc:
    def test_estimate_tokens(self):
        assert estimate_tokens("") == 0
        assert estimate_tokens("abcd" * 10) >= 5

    def test_build_sources_section(self):
        s = build_sources_section(["https://a.com", "https://b.com"])
        assert s.startswith("## Sources")
        assert "[2] https://b.com" in s
        assert build_sources_section([]) == ""
