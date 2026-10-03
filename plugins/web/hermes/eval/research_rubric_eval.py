#!/usr/bin/env python3
"""Rubric-graded research eval (A4).

Companion to ``research_eval.py``: that harness grades short factual answers
against references (SimpleQA-style); THIS one grades open-ended research
output — market questions, lead-qualification briefs, competitive analyses,
freshness checks — where no single reference answer exists. An LLM judge
scores each answer 0-1 on five dimensions:

  factual_accuracy   claims correct + internally consistent, no invented specifics
  citation_accuracy  claims tied to specific, plausible cited sources
  completeness       covers the case's expected aspects (``guidance``)
  source_quality     authoritative, diverse, recent-enough sources
  tool_efficiency    effort proportionate to the ask (needs ``tool_stats``; null otherwise)

The judge model rides the existing auxiliary routing (web_extract aux) and
falls back to OpenRouter when a key is on disk — there is no separate judge
config. With neither configured the runner SKIPS gracefully (clear message,
exit 0) so cron/CI never breaks on a missing key.

This runner NEVER launches live research. Answers come from a bundles file
produced by whatever driver ran the agent:

  {"case-id": "answer text", ...}                          # minimal
  {"case-id": {"answer": "...", "sources": ["url", ...],   # full bundle
               "tool_stats": {"searches": 4, "fetches": 9, "seconds": 210}}}

Usage:
  venv/bin/python -m plugins.web.hermes.eval.research_rubric_eval --dry-run
  venv/bin/python -m plugins.web.hermes.eval.research_rubric_eval \\
      --answers answers.json [--out rubric_results.jsonl] [cases.json]

Results stream to a resumable JSONL (re-running skips graded cases), same as
research_eval.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

DEFAULT_CASES_PATH = Path(__file__).resolve().parent / "research_rubric_cases.json"

RUBRIC_DIMENSIONS = (
    "factual_accuracy",
    "citation_accuracy",
    "completeness",
    "source_quality",
    "tool_efficiency",
)

# tool_efficiency is judgeable only when the driver recorded tool stats.
_REQUIRED_DIMENSIONS = RUBRIC_DIMENSIONS[:4]

# Fallback judge model when only an OpenRouter key is configured (mirrors the
# lead-gen batch's synthesis default; override via env).
_OPENROUTER_JUDGE_MODEL_ENV = "RESEARCH_RUBRIC_JUDGE_MODEL"
_OPENROUTER_JUDGE_MODEL_DEFAULT = "deepseek/deepseek-v4-pro"

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)

JUDGE_PROMPT_TEMPLATE = """You are grading a research assistant's answer for an operator who depends on accurate, well-sourced intelligence. Score STRICTLY. Each dimension is a float from 0.0 (unusable) to 1.0 (excellent):

- factual_accuracy: are the specific claims correct and internally consistent? Penalize invented specifics (names, numbers, dates) and contradictions.
- citation_accuracy: are claims tied to the cited sources; are citations specific (deep links, not bare homepages) and plausible for what they're cited for?
- completeness: does the answer cover the EXPECTED ASPECTS below?
- source_quality: authoritative/primary, diverse, and recent enough for the question? (Freshness-sensitive questions demand dated, current sources.)
- tool_efficiency: given TOOL STATS (searches/fetches/seconds), was effort proportionate to the ask — neither a single lazy search nor unbounded thrashing? If TOOL STATS is "not recorded", set this to null.

QUESTION:
{question}

EXPECTED ASPECTS (completeness checklist):
{guidance}

FRESHNESS-SENSITIVE: {freshness}
EXPECTED SOURCE TYPES: {source_types}

ANSWER UNDER REVIEW:
{answer}

CITED SOURCES:
{sources}

TOOL STATS: {tool_stats}

Reply with ONLY a JSON object, no prose, no code fences:
{{"factual_accuracy": 0.0, "citation_accuracy": 0.0, "completeness": 0.0, "source_quality": 0.0, "tool_efficiency": null, "rationale": "<= 60 words"}}"""


# ── pure helpers ─────────────────────────────────────────────────────────────

def load_cases(path: Optional[Path] = None) -> List[Dict[str, Any]]:
    data = json.loads((path or DEFAULT_CASES_PATH).read_text(encoding="utf-8"))
    return data.get("cases", data) if isinstance(data, dict) else data


def _clamp01(value: Any) -> Optional[float]:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return None


def parse_rubric_scores(text: Optional[str]) -> Optional[Dict[str, Any]]:
    """Parse a judge reply into ``{dimension: float|None, "rationale": str}``.

    Fence-tolerant, clamps to [0,1], keeps ``tool_efficiency`` as ``None`` when
    null/absent. Returns ``None`` when fewer than three required dimensions
    parse — an unusable verdict must not silently score as zeros.
    """
    if not text:
        return None
    s = text.strip()
    m = _JSON_FENCE_RE.search(s)
    if m:
        s = m.group(1).strip()
    start = s.find("{")
    if start > 0:
        s = s[start:]
    obj = None
    for end in (len(s), s.rfind("}") + 1):
        if end <= 0:
            continue
        try:
            obj = json.loads(s[:end])
            break
        except (json.JSONDecodeError, ValueError):
            continue
    if not isinstance(obj, dict):
        return None
    out: Dict[str, Any] = {}
    for dim in RUBRIC_DIMENSIONS:
        out[dim] = _clamp01(obj.get(dim))
    present = sum(1 for dim in _REQUIRED_DIMENSIONS if out[dim] is not None)
    if present < 3:
        return None
    out["rationale"] = str(obj.get("rationale") or "")[:500]
    return out


def normalize_bundle(bundle: Any) -> Dict[str, Any]:
    """Accept a bare answer string or a full bundle dict."""
    if isinstance(bundle, str):
        return {"answer": bundle, "sources": [], "tool_stats": None}
    if isinstance(bundle, dict):
        return {
            "answer": str(bundle.get("answer") or ""),
            "sources": [str(u) for u in (bundle.get("sources") or [])],
            "tool_stats": bundle.get("tool_stats"),
        }
    return {"answer": "", "sources": [], "tool_stats": None}


def build_judge_prompt(case: Dict[str, Any], bundle: Dict[str, Any]) -> str:
    sources = list(bundle.get("sources") or [])
    if not sources:
        # Fall back to URLs cited inline in the answer (same URL grammar the
        # citation validator uses) so citation_accuracy stays judgeable.
        try:
            from agent.research_citations import extract_urls

            sources = extract_urls(bundle.get("answer") or "")
        except Exception:
            sources = []
    stats = bundle.get("tool_stats")
    return JUDGE_PROMPT_TEMPLATE.format(
        question=case.get("question", ""),
        guidance="\n".join(f"- {g}" for g in case.get("guidance", [])) or "- (none listed)",
        freshness="yes" if case.get("freshness_sensitive") else "no",
        source_types=", ".join(case.get("expected_source_types", [])) or "(unspecified)",
        answer=(bundle.get("answer") or "(empty answer)")[:24000],
        sources="\n".join(f"- {u}" for u in sources[:40]) or "(none)",
        tool_stats=json.dumps(stats) if stats else "not recorded",
    )


def grade_case(
    case: Dict[str, Any],
    bundle: Any,
    judge_fn: Callable[[str], str],
) -> Dict[str, Any]:
    """Grade one case; judge failures degrade to an ungraded record."""
    b = normalize_bundle(bundle)
    record: Dict[str, Any] = {
        "id": case.get("id"),
        "category": case.get("category"),
        "tier": case.get("tier"),
        "scores": None,
        "overall": None,
        "rationale": "",
        "error": None,
    }
    if not b["answer"].strip():
        record["error"] = "empty_answer"
        return record
    try:
        raw = judge_fn(build_judge_prompt(case, b))
    except Exception as exc:  # noqa: BLE001 — a judge hiccup must not kill the run
        record["error"] = f"judge_error: {str(exc)[:200]}"
        return record
    parsed = parse_rubric_scores(raw)
    if parsed is None:
        record["error"] = "unparseable_judge_reply"
        return record
    record["rationale"] = parsed.pop("rationale", "")
    record["scores"] = parsed
    graded = [v for v in parsed.values() if v is not None]
    record["overall"] = round(sum(graded) / len(graded), 4) if graded else None
    return record


def aggregate_rubric(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Per-dimension means over graded records (None scores excluded)."""
    graded = [r for r in records if r.get("scores")]
    out: Dict[str, Any] = {
        "n": len(records),
        "graded": len(graded),
        "errors": sum(1 for r in records if r.get("error")),
    }
    for dim in RUBRIC_DIMENSIONS:
        vals = [r["scores"][dim] for r in graded if r["scores"].get(dim) is not None]
        out[dim] = round(sum(vals) / len(vals), 4) if vals else None
    overalls = [r["overall"] for r in graded if r.get("overall") is not None]
    out["overall"] = round(sum(overalls) / len(overalls), 4) if overalls else None
    return out


# ── judge resolution (existing aux / openrouter config only) ────────────────

def _openrouter_key() -> str:
    env = os.getenv("OPENROUTER_API_KEY", "").strip()
    if env:
        return env
    kf = Path.home() / ".hermes" / ".openrouter_key"
    try:
        return kf.read_text().strip() if kf.exists() else ""
    except OSError:
        return ""


def resolve_judge() -> Tuple[Optional[Callable[[str], str]], str]:
    """Build the judge callable from existing config, or ``(None, why)``.

    Order: the web_extract auxiliary route (same resolution the scraper and
    the citation-support pass use), then plain OpenRouter with the key on
    disk. No new config surface.
    """
    try:
        import asyncio

        from agent.auxiliary_client import async_call_llm, extract_content_or_reasoning

        def _mk(call_kwargs_base: Dict[str, Any], label: str):
            def _judge(prompt: str) -> str:
                async def _go():
                    kwargs = dict(call_kwargs_base)
                    kwargs["messages"] = [{"role": "user", "content": prompt}]
                    return extract_content_or_reasoning(await async_call_llm(**kwargs))

                return asyncio.run(_go()) or ""

            return _judge, label

        try:
            from tools.web_tools import _resolve_web_extract_auxiliary

            client, model, extra = _resolve_web_extract_auxiliary()
        except Exception:
            client, model, extra = None, None, {}
        if client is not None and model:
            base: Dict[str, Any] = {
                "task": "research_rubric_eval",
                "model": model,
                "temperature": 0.0,
                "max_tokens": 700,
            }
            if extra:
                base["extra_body"] = extra
            return _mk(base, f"auxiliary model ({model})")

        key = _openrouter_key()
        if key:
            model = os.getenv(_OPENROUTER_JUDGE_MODEL_ENV, "").strip() or _OPENROUTER_JUDGE_MODEL_DEFAULT
            return _mk(
                {
                    "task": "research_rubric_eval",
                    "provider": "openrouter",
                    "model": model,
                    "api_key": key,
                    "temperature": 0.0,
                    "max_tokens": 700,
                },
                f"openrouter ({model})",
            )
        return None, "no auxiliary model configured and no OpenRouter key found"
    except Exception as exc:  # noqa: BLE001 — resolution must never crash the runner
        return None, f"judge resolution failed: {str(exc)[:200]}"


# ── runner ───────────────────────────────────────────────────────────────────

def _load_done_ids(out_path: Path) -> set:
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


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Rubric-graded research eval (A4)")
    here = Path(__file__).resolve().parent
    ap.add_argument("cases", nargs="?", default=str(DEFAULT_CASES_PATH))
    ap.add_argument("--answers", default="", help="JSON {id: answer|bundle} to grade")
    ap.add_argument("--out", default=str(here / "rubric_results.jsonl"))
    ap.add_argument("--dry-run", action="store_true",
                    help="list cases and exit; no judge, no network")
    args = ap.parse_args(argv)

    cases = load_cases(Path(args.cases))
    if args.dry_run:
        print(f"{len(cases)} rubric eval case(s) in {args.cases}:")
        for c in cases:
            q = " ".join(str(c.get("question", "")).split())
            print(f"  {c.get('id'):32} [{c.get('category')}/{c.get('tier')}] "
                  f"{q[:90]}{'…' if len(q) > 90 else ''}")
        print("\nDimensions:", ", ".join(RUBRIC_DIMENSIONS))
        print("Grade real output with: --answers answers.json "
              "({id: answer-or-bundle}; this runner never launches live research).")
        return 0

    if not args.answers:
        print("No --answers file provided — nothing to grade. This runner never "
              "launches live research; produce answers with your driver, then "
              "re-run with --answers {id: answer|bundle}. (--dry-run lists cases.)")
        return 0

    judge_fn, judge_desc = resolve_judge()
    if judge_fn is None:
        print(f"SKIP: no judge model available — {judge_desc}.")
        print("Configure the auxiliary model (hermes setup) or place an OpenRouter "
              "key in $OPENROUTER_API_KEY / ~/.hermes/.openrouter_key, then re-run.")
        return 0
    print(f"Judge: {judge_desc}")

    bundles = json.loads(Path(args.answers).read_text(encoding="utf-8"))
    out_path = Path(args.out)
    done = _load_done_ids(out_path)
    records: List[Dict[str, Any]] = []
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
            cid = case.get("id")
            if cid in done:
                continue
            if cid not in bundles:
                print(f"  {cid}: no answer in bundle — skipped")
                continue
            rec = grade_case(case, bundles[cid], judge_fn)
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fh.flush()
            records.append(rec)
            if rec.get("scores"):
                dims = " ".join(
                    f"{d.split('_')[0]}={rec['scores'][d] if rec['scores'][d] is not None else '-'}"
                    for d in RUBRIC_DIMENSIONS
                )
                print(f"  {cid}: overall={rec['overall']}  {dims}")
            else:
                print(f"  {cid}: ERROR {rec.get('error')}")

    print("\n=== Rubric eval summary ===")
    print(json.dumps(aggregate_rubric(records), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
