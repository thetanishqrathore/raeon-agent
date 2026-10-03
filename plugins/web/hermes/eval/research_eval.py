#!/usr/bin/env python3
"""Research-quality eval harness (P1.5).

Measures the deep-research flow end-to-end with an LLM-as-judge, so changes to
the research engine (think_tool, ranking, delegation, model routing) can be
checked for regressions instead of vibes. Mirrors the scraper eval
(``run_eval.py``) in shape and the field's grading conventions:

* **SimpleQA-style verdict** — a judge classifies each answer as CORRECT /
  INCORRECT / NOT_ATTEMPTED against a reference answer. We report accuracy,
  attempted-accuracy (correct / attempted), and not-attempted rate.
  *(OpenResearcher / Tongyi SimpleQA grading — patterns only.)*
* **Key-Point-Recall** — fraction of each case's required facts present in the
  answer; deterministic, judge-free. *(efficient-deep-research.)*

The grading logic (classify_grade, key_point_recall, aggregate) is pure and
unit-tested. The answer generator and judge are injected, so the harness runs
with stubs in tests and with the real agent + auxiliary model from the CLI.
Results stream to a resumable JSONL file (re-running skips graded cases).

Usage:
  venv/bin/python -m plugins.web.hermes.eval.research_eval [research_cases.json] \\
      [--out results.jsonl] [--answers answers.json]

``--answers`` lets you grade pre-generated answers (``{id: answer_text}``)
without invoking the agent — useful for comparing model tiers offline.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

_WS = re.compile(r"\s+")

GRADES = ("CORRECT", "INCORRECT", "NOT_ATTEMPTED")

# SimpleQA-style judge prompt. The judge sees the reference answer and the
# candidate, and returns exactly one letter.
SIMPLEQA_JUDGE_TEMPLATE = (
    "You are grading a research answer against a known-correct reference.\n"
    "Question: {question}\n"
    "Reference answer (ground truth): {target}\n"
    "Candidate answer: {answer}\n\n"
    "Grade the candidate:\n"
    "A = CORRECT: contains the reference answer's key facts with no contradiction.\n"
    "B = INCORRECT: contradicts the reference or states a wrong key fact.\n"
    "C = NOT_ATTEMPTED: hedges/declines/omits the key facts without contradicting.\n"
    "Reply with ONLY the single letter A, B, or C."
)


def _norm(s: str) -> str:
    return _WS.sub(" ", (s or "").lower()).strip()


def classify_grade(judge_text: str) -> str:
    """Map a judge response to CORRECT / INCORRECT / NOT_ATTEMPTED.

    Accepts a leading letter (A/B/C) or the grade words. Defaults to
    NOT_ATTEMPTED on anything unrecognized (the conservative non-credit verdict).
    """
    t = _norm(judge_text)
    if not t:
        return "NOT_ATTEMPTED"
    # Word forms win if explicitly present.
    if "not_attempted" in t or "not attempted" in t:
        return "NOT_ATTEMPTED"
    if "incorrect" in t:
        return "INCORRECT"
    if "correct" in t:
        return "CORRECT"
    # Otherwise the first A/B/C token.
    m = re.search(r"\b([abc])\b", t)
    if m:
        return {"a": "CORRECT", "b": "INCORRECT", "c": "NOT_ATTEMPTED"}[m.group(1)]
    head = t[0]
    return {"a": "CORRECT", "b": "INCORRECT", "c": "NOT_ATTEMPTED"}.get(head, "NOT_ATTEMPTED")


def key_point_recall(answer: str, key_points: List[str]) -> float:
    """Fraction of ``key_points`` present in ``answer`` (normalized substring)."""
    if not key_points:
        return 1.0
    a = _norm(answer)
    hits = sum(1 for kp in key_points if _norm(kp) in a)
    return round(hits / len(key_points), 4)


def grade_answer(
    case: Dict[str, Any],
    answer: str,
    judge_fn: Optional[Callable[[str], str]] = None,
) -> Dict[str, Any]:
    """Grade one answer: SimpleQA verdict (via judge_fn) + key-point recall."""
    recall = key_point_recall(answer, case.get("key_points", []))
    grade = "NOT_ATTEMPTED"
    if not (answer or "").strip():
        grade = "NOT_ATTEMPTED"
    elif judge_fn is not None:
        prompt = SIMPLEQA_JUDGE_TEMPLATE.format(
            question=case.get("question", ""),
            target=case.get("answer", ""),
            answer=answer,
        )
        try:
            grade = classify_grade(judge_fn(prompt))
        except Exception:
            grade = "NOT_ATTEMPTED"
    else:
        # No judge available: fall back to recall as a coarse proxy so the
        # harness still produces a verdict offline.
        grade = "CORRECT" if recall >= 0.999 else ("NOT_ATTEMPTED" if recall == 0 else "INCORRECT")
    return {
        "id": case.get("id"),
        "tier": case.get("tier"),
        "grade": grade,
        "key_point_recall": recall,
        "answer": answer,
    }


def aggregate(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Roll up graded records into headline metrics."""
    n = len(records)
    if n == 0:
        return {"n": 0}
    counts = {g: 0 for g in GRADES}
    for r in records:
        counts[r.get("grade", "NOT_ATTEMPTED")] = counts.get(r.get("grade", "NOT_ATTEMPTED"), 0) + 1
    attempted = counts["CORRECT"] + counts["INCORRECT"]
    mean_recall = round(sum(r.get("key_point_recall", 0.0) for r in records) / n, 4)
    return {
        "n": n,
        "correct": counts["CORRECT"],
        "incorrect": counts["INCORRECT"],
        "not_attempted": counts["NOT_ATTEMPTED"],
        # Accuracy over ALL cases (a NOT_ATTEMPTED counts against you).
        "accuracy": round(counts["CORRECT"] / n, 4),
        # Accuracy over only attempted cases (penalizes confident wrong answers).
        "attempted_accuracy": round(counts["CORRECT"] / attempted, 4) if attempted else 0.0,
        "not_attempted_rate": round(counts["NOT_ATTEMPTED"] / n, 4),
        "mean_key_point_recall": mean_recall,
    }


def _load_done_ids(out_path: Path) -> set:
    """IDs already graded in a resumable JSONL output."""
    done = set()
    if out_path.exists():
        for line in out_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                done.add(json.loads(line).get("id"))
            except ValueError:
                continue
    return done


def run_eval(
    cases: List[Dict[str, Any]],
    answer_fn: Callable[[Dict[str, Any]], str],
    judge_fn: Optional[Callable[[str], str]],
    out_path: Path,
) -> List[Dict[str, Any]]:
    """Grade each case, streaming to a resumable JSONL. Returns all records."""
    done = _load_done_ids(out_path)
    records: List[Dict[str, Any]] = []
    # Re-read any prior records so the aggregate covers the full set.
    if out_path.exists():
        for line in out_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except ValueError:
                    pass
    with out_path.open("a", encoding="utf-8") as fh:
        for case in cases:
            if case.get("id") in done:
                continue
            answer = ""
            try:
                answer = answer_fn(case) or ""
            except Exception as exc:  # noqa: BLE001
                answer = ""
                print(f"  ! answer generation failed for {case.get('id')}: {exc}")
            rec = grade_answer(case, answer, judge_fn)
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fh.flush()
            records.append(rec)
            print(f"  {rec['id']}: {rec['grade']}  recall={rec['key_point_recall']}")
    return records


def _load_cases(path: Path) -> List[Dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("cases", data) if isinstance(data, dict) else data


def _default_judge() -> Optional[Callable[[str], str]]:
    """Build a judge backed by the auxiliary model, or None if unavailable."""
    try:
        from agent.auxiliary_client import async_call_llm, extract_content_or_reasoning
        from tools.web_tools import _resolve_web_extract_auxiliary
        import asyncio

        client, model, extra = _resolve_web_extract_auxiliary()
        if client is None or not model:
            return None

        def _judge(prompt: str) -> str:
            async def _go():
                kwargs = {
                    "task": "research_eval",
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.0,
                    "max_tokens": 8,
                }
                if extra:
                    kwargs["extra_body"] = extra
                return extract_content_or_reasoning(await async_call_llm(**kwargs))

            return asyncio.run(_go()) or ""

        return _judge
    except Exception:
        return None


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Research-quality eval harness")
    here = Path(__file__).resolve().parent
    ap.add_argument("cases", nargs="?", default=str(here / "research_cases.json"))
    ap.add_argument("--out", default=str(here / "research_results.jsonl"))
    ap.add_argument("--answers", default="", help="JSON {id: answer} to grade offline")
    args = ap.parse_args(argv)

    cases = _load_cases(Path(args.cases))
    out_path = Path(args.out)

    if args.answers:
        precomputed = json.loads(Path(args.answers).read_text(encoding="utf-8"))
        def answer_fn(case):  # noqa: E306
            return precomputed.get(case.get("id"), "")
    else:
        print("No --answers provided and no agent runner wired in this entry "
              "point; grading empty answers. Provide --answers {id: text} to "
              "grade real outputs, or import run_eval() with your own answer_fn.")
        def answer_fn(case):  # noqa: E306
            return ""

    judge_fn = _default_judge()
    print(f"Judge: {'auxiliary model' if judge_fn else 'NONE (recall-proxy grading)'}")
    records = run_eval(cases, answer_fn, judge_fn, out_path)
    summary = aggregate(records)
    print("\n=== Research eval summary ===")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
