"""Tests for the rubric-graded research eval harness (A4).

No live research and no live judge anywhere: the judge is faked/monkeypatched,
and the graceful-skip path is asserted (missing key/model must exit 0 with a
clear message — cron/CI safe).
"""

import json

import pytest

import plugins.web.hermes.eval.research_rubric_eval as rre


class TestCasesFile:
    def test_case_count_in_agreed_range(self):
        cases = rre.load_cases()
        assert 12 <= len(cases) <= 15

    def test_ids_unique_and_fields_present(self):
        cases = rre.load_cases()
        ids = [c["id"] for c in cases]
        assert len(ids) == len(set(ids))
        for c in cases:
            assert c["question"].strip()
            assert isinstance(c["guidance"], list) and c["guidance"]
            assert c["category"] in {"market", "leads", "competitive", "freshness"}
            assert c["tier"] in {"quick", "detailed", "report"}
            assert isinstance(c["freshness_sensitive"], bool)
            assert isinstance(c["expected_source_types"], list)

    def test_covers_the_owners_usage_themes(self):
        cases = rre.load_cases()
        categories = {c["category"] for c in cases}
        assert categories == {"market", "leads", "competitive", "freshness"}
        # 'latest X' questions need enough representation to matter.
        assert sum(1 for c in cases if c["freshness_sensitive"]) >= 4


class TestParseRubricScores:
    def test_plain_json(self):
        raw = json.dumps({
            "factual_accuracy": 0.9, "citation_accuracy": 0.8,
            "completeness": 0.7, "source_quality": 0.6,
            "tool_efficiency": 0.5, "rationale": "solid",
        })
        parsed = rre.parse_rubric_scores(raw)
        assert parsed["factual_accuracy"] == 0.9
        assert parsed["tool_efficiency"] == 0.5
        assert parsed["rationale"] == "solid"

    def test_fenced_json_with_preamble(self):
        raw = ("Here are the scores:\n```json\n"
               '{"factual_accuracy": 1, "citation_accuracy": 0.5, '
               '"completeness": 0.5, "source_quality": 0.5, '
               '"tool_efficiency": null, "rationale": "ok"}\n```')
        parsed = rre.parse_rubric_scores(raw)
        assert parsed["factual_accuracy"] == 1.0
        assert parsed["tool_efficiency"] is None

    def test_scores_clamped_to_unit_interval(self):
        raw = json.dumps({
            "factual_accuracy": 1.7, "citation_accuracy": -0.2,
            "completeness": 0.5, "source_quality": 0.5,
        })
        parsed = rre.parse_rubric_scores(raw)
        assert parsed["factual_accuracy"] == 1.0
        assert parsed["citation_accuracy"] == 0.0

    def test_junk_returns_none(self):
        assert rre.parse_rubric_scores("the answer is great, 10/10") is None
        assert rre.parse_rubric_scores("") is None
        assert rre.parse_rubric_scores(None) is None

    def test_mostly_missing_dimensions_returns_none(self):
        assert rre.parse_rubric_scores(json.dumps({"factual_accuracy": 0.9})) is None


class TestGradeAndAggregate:
    CASE = {
        "id": "x", "category": "market", "tier": "quick",
        "question": "What changed in the latest release?",
        "guidance": ["names the version", "gives the date"],
        "freshness_sensitive": True,
        "expected_source_types": ["changelog"],
    }

    def _judge(self, reply):
        def judge_fn(prompt):
            judge_fn.last_prompt = prompt
            return reply
        judge_fn.last_prompt = ""
        return judge_fn

    def test_grade_case_happy_path_with_bundle(self):
        reply = json.dumps({
            "factual_accuracy": 0.8, "citation_accuracy": 0.9,
            "completeness": 1.0, "source_quality": 0.7,
            "tool_efficiency": 0.6, "rationale": "good",
        })
        judge = self._judge(reply)
        bundle = {"answer": "v0.9 shipped 2026-06-01 per the changelog.",
                  "sources": ["https://github.com/x/releases"],
                  "tool_stats": {"searches": 2, "fetches": 3, "seconds": 40}}
        rec = rre.grade_case(self.CASE, bundle, judge)
        assert rec["overall"] == 0.8
        assert rec["scores"]["completeness"] == 1.0
        # The judge saw the material it needs to score against.
        assert "names the version" in judge.last_prompt
        assert "github.com/x/releases" in judge.last_prompt
        assert '"searches": 2' in judge.last_prompt

    def test_grade_case_accepts_bare_string_and_extracts_inline_sources(self):
        judge = self._judge(json.dumps({
            "factual_accuracy": 0.5, "citation_accuracy": 0.5,
            "completeness": 0.5, "source_quality": 0.5,
        }))
        rre.grade_case(self.CASE, "v0.9 per https://github.com/x/releases notes.", judge)
        assert "https://github.com/x/releases" in judge.last_prompt
        assert "not recorded" in judge.last_prompt  # tool stats absent

    def test_grade_case_null_tool_efficiency_excluded_from_overall(self):
        judge = self._judge(json.dumps({
            "factual_accuracy": 1.0, "citation_accuracy": 1.0,
            "completeness": 1.0, "source_quality": 1.0,
            "tool_efficiency": None,
        }))
        rec = rre.grade_case(self.CASE, "an answer with enough text", judge)
        assert rec["overall"] == 1.0

    def test_grade_case_empty_answer_is_an_error_not_a_judge_call(self):
        def explode(prompt):
            raise AssertionError("judge must not be called")
        rec = rre.grade_case(self.CASE, "", explode)
        assert rec["error"] == "empty_answer" and rec["scores"] is None

    def test_grade_case_judge_exception_degrades(self):
        def boom(prompt):
            raise RuntimeError("429")
        rec = rre.grade_case(self.CASE, "some answer", boom)
        assert rec["error"].startswith("judge_error")

    def test_aggregate_means_skip_none(self):
        records = [
            {"scores": {"factual_accuracy": 1.0, "citation_accuracy": 0.5,
                        "completeness": 1.0, "source_quality": 0.5,
                        "tool_efficiency": None}, "overall": 0.75},
            {"scores": {"factual_accuracy": 0.0, "citation_accuracy": 0.5,
                        "completeness": 1.0, "source_quality": 0.5,
                        "tool_efficiency": 1.0}, "overall": 0.6},
            {"scores": None, "overall": None, "error": "empty_answer"},
        ]
        agg = rre.aggregate_rubric(records)
        assert agg["n"] == 3 and agg["graded"] == 2 and agg["errors"] == 1
        assert agg["factual_accuracy"] == 0.5
        assert agg["tool_efficiency"] == 1.0  # only the non-None value
        assert agg["overall"] == 0.675


class TestRunner:
    def test_dry_run_lists_all_cases_and_exits_zero(self, capsys):
        assert rre.main(["--dry-run"]) == 0
        out = capsys.readouterr().out
        for c in rre.load_cases():
            assert c["id"] in out
        assert "never launches live research" in out

    def test_no_answers_is_a_noop_exit_zero(self, capsys):
        assert rre.main([]) == 0
        assert "nothing to grade" in capsys.readouterr().out

    def test_skips_gracefully_when_no_judge_configured(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setattr(rre, "resolve_judge", lambda: (None, "no key on disk"))
        answers = tmp_path / "answers.json"
        answers.write_text(json.dumps({"crawl4ai-latest-release": "v0.9 shipped."}))
        rc = rre.main(["--answers", str(answers), "--out", str(tmp_path / "r.jsonl")])
        assert rc == 0
        out = capsys.readouterr().out
        assert "SKIP" in out and "no key on disk" in out

    def test_grades_with_fake_judge_and_streams_resumable_jsonl(self, tmp_path, monkeypatch, capsys):
        reply = json.dumps({
            "factual_accuracy": 0.9, "citation_accuracy": 0.9,
            "completeness": 0.8, "source_quality": 0.8,
            "tool_efficiency": None, "rationale": "fine",
        })
        calls = []

        def fake_judge(prompt):
            calls.append(prompt)
            return reply

        monkeypatch.setattr(rre, "resolve_judge", lambda: (fake_judge, "fake"))
        cases = rre.load_cases()
        answers = tmp_path / "answers.json"
        answers.write_text(json.dumps({
            cases[0]["id"]: "First answer citing https://a.com/x today.",
            cases[1]["id"]: {"answer": "Second answer.", "sources": ["https://b.com"],
                             "tool_stats": {"searches": 1, "fetches": 2, "seconds": 30}},
        }))
        out_file = tmp_path / "r.jsonl"

        assert rre.main(["--answers", str(answers), "--out", str(out_file)]) == 0
        lines = [json.loads(l) for l in out_file.read_text().splitlines()]
        assert {r["id"] for r in lines} == {cases[0]["id"], cases[1]["id"]}
        assert all(r["overall"] is not None for r in lines)
        assert len(calls) == 2
        assert "Rubric eval summary" in capsys.readouterr().out

        # Resumable: a second run re-grades nothing.
        calls.clear()
        assert rre.main(["--answers", str(answers), "--out", str(out_file)]) == 0
        assert calls == []
        assert len(out_file.read_text().splitlines()) == 2

    def test_openrouter_key_lookup_prefers_env(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
        assert rre._openrouter_key() == "sk-or-test"


class TestJudgeResolutionShape:
    def test_resolve_judge_returns_tuple_and_never_raises(self):
        # Aux resolution may return a lazily-constructed client even when no
        # provider can actually serve (repo-wide precedent: runtime failures
        # degrade per-record, never crash). Assert the contract shape only.
        judge, desc = rre.resolve_judge()
        assert judge is None or callable(judge)
        assert isinstance(desc, str) and desc

    def test_no_judge_when_aux_and_key_both_absent(self, monkeypatch):
        import tools.web_tools as wt
        monkeypatch.setattr(wt, "_resolve_web_extract_auxiliary",
                            lambda model=None: (None, None, {}))
        monkeypatch.setattr(rre, "_openrouter_key", lambda: "")
        judge, desc = rre.resolve_judge()
        assert judge is None
        assert "no auxiliary model" in desc

    def test_openrouter_fallback_used_when_key_present(self, monkeypatch):
        import tools.web_tools as wt
        monkeypatch.setattr(wt, "_resolve_web_extract_auxiliary",
                            lambda model=None: (None, None, {}))
        monkeypatch.setattr(rre, "_openrouter_key", lambda: "sk-or-test")
        judge, desc = rre.resolve_judge()
        assert callable(judge)
        assert desc.startswith("openrouter (")
