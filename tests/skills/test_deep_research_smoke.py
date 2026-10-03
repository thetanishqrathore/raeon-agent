"""Smoke + unit tests for the research eval harness (P1.5).

Exercises the pure grading logic and the resumable harness with stubbed
answer/judge functions (no LLM, no network), plus validates the case set and
that the live deep-research skill mandates the new research tools.
"""

import json
from pathlib import Path

import pytest

from plugins.web.hermes.eval.research_eval import (
    aggregate,
    classify_grade,
    grade_answer,
    key_point_recall,
    run_eval,
    _load_cases,
)

_CASES_PATH = Path("plugins/web/hermes/eval/research_cases.json")


class TestClassifyGrade:
    @pytest.mark.parametrize("text,expected", [
        ("A", "CORRECT"),
        ("B", "INCORRECT"),
        ("C", "NOT_ATTEMPTED"),
        ("a — correct", "CORRECT"),
        ("INCORRECT", "INCORRECT"),
        ("not attempted", "NOT_ATTEMPTED"),
        ("", "NOT_ATTEMPTED"),
        ("???", "NOT_ATTEMPTED"),
    ])
    def test_classify(self, text, expected):
        assert classify_grade(text) == expected


class TestKeyPointRecall:
    def test_all_present(self):
        assert key_point_recall("Einstein in 1915", ["Einstein", "1915"]) == 1.0

    def test_partial(self):
        assert key_point_recall("Einstein", ["Einstein", "1915"]) == 0.5

    def test_none(self):
        assert key_point_recall("nothing here", ["Einstein"]) == 0.0

    def test_empty_keypoints_is_full(self):
        assert key_point_recall("anything", []) == 1.0


class TestGradeAnswer:
    def test_judge_correct(self):
        case = {"id": "x", "question": "q", "answer": "ref", "key_points": ["foo"]}
        rec = grade_answer(case, "foo is the answer", judge_fn=lambda p: "A")
        assert rec["grade"] == "CORRECT"
        assert rec["key_point_recall"] == 1.0

    def test_empty_answer_not_attempted(self):
        case = {"id": "x", "key_points": ["foo"]}
        rec = grade_answer(case, "", judge_fn=lambda p: "A")
        assert rec["grade"] == "NOT_ATTEMPTED"

    def test_judge_exception_defaults_not_attempted(self):
        case = {"id": "x", "key_points": []}
        def boom(_p):
            raise RuntimeError("judge down")
        rec = grade_answer(case, "some answer", judge_fn=boom)
        assert rec["grade"] == "NOT_ATTEMPTED"

    def test_no_judge_uses_recall_proxy(self):
        case = {"id": "x", "key_points": ["foo", "bar"]}
        assert grade_answer(case, "foo bar", judge_fn=None)["grade"] == "CORRECT"
        assert grade_answer(case, "foo", judge_fn=None)["grade"] == "INCORRECT"
        assert grade_answer(case, "nope", judge_fn=None)["grade"] == "NOT_ATTEMPTED"


class TestAggregate:
    def test_metrics(self):
        recs = [
            {"grade": "CORRECT", "key_point_recall": 1.0},
            {"grade": "INCORRECT", "key_point_recall": 0.5},
            {"grade": "NOT_ATTEMPTED", "key_point_recall": 0.0},
            {"grade": "CORRECT", "key_point_recall": 1.0},
        ]
        agg = aggregate(recs)
        assert agg["n"] == 4
        assert agg["correct"] == 2
        assert agg["accuracy"] == 0.5            # 2/4
        assert agg["attempted_accuracy"] == round(2 / 3, 4)  # 2 correct of 3 attempted
        assert agg["not_attempted_rate"] == 0.25
        assert agg["mean_key_point_recall"] == round(2.5 / 4, 4)

    def test_empty(self):
        assert aggregate([])["n"] == 0


class TestRunEvalResumable:
    def test_streams_and_resumes(self, tmp_path):
        cases = [
            {"id": "c1", "question": "q1", "answer": "a1", "key_points": ["x"]},
            {"id": "c2", "question": "q2", "answer": "a2", "key_points": ["y"]},
        ]
        out = tmp_path / "res.jsonl"
        answers = {"c1": "x found", "c2": "y found"}
        recs = run_eval(cases, lambda c: answers[c["id"]], lambda p: "A", out)
        assert len(recs) == 2
        assert out.exists()
        first_lines = out.read_text().strip().splitlines()
        assert len(first_lines) == 2

        # Re-run: both already graded -> no new lines appended.
        calls = []
        run_eval(cases, lambda c: calls.append(c["id"]) or "x", lambda p: "A", out)
        assert calls == []  # answer_fn never called for already-done cases
        assert len(out.read_text().strip().splitlines()) == 2


class TestCaseSet:
    def test_cases_load_and_have_required_fields(self):
        cases = _load_cases(_CASES_PATH)
        assert len(cases) >= 5
        for c in cases:
            assert c["id"] and c["question"] and c["answer"]
            assert isinstance(c["key_points"], list) and c["key_points"]


class TestSkillMentionsTools:
    def test_live_skill_mandates_research_tools(self):
        skill = Path.home() / ".hermes/skills/research/deep-research/SKILL.md"
        if not skill.exists():
            pytest.skip("live skill not installed")
        text = skill.read_text(encoding="utf-8")
        assert "think" in text
        # Research-shaped fan-out rides the wired delegate_task substrate.
        assert "delegate_task" in text
        assert "rank_sources" in text
