#!/usr/bin/env python3
"""Evaluation harness for the Hermes native scraper.

Runs the deterministic extraction pipeline (and, when an auxiliary model is
available, the structured-extraction path) over a verified case set and reports
extraction quality vs a naive full-page baseline.

Metrics per case (all 0..1, higher = better unless noted):

  content_recall      fraction of `must_include` (main content) kept in fit md
  boilerplate_removal fraction of `must_exclude` (chrome) ABSENT from fit md
  quality_f1          harmonic mean of the two above — the headline score
  compression         fit_chars / full_page_text_chars        (lower = leaner)
  label_ok            sanity: every must_include is present in the raw page
  field_accuracy      (structured cases) fraction of expected values extracted

Baseline `raw` = full cleaned body markdown (no content-root, no prune) — i.e.
dumb html→markdown. It should show ~1.0 recall but ~0 boilerplate_removal,
quantifying the value pruning adds.

Usage:
  venv/bin/python -m plugins.web.hermes.eval.run_eval [cases.json] [--no-llm]
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from plugins.web.hermes.scraper import fetch as _fetch  # noqa: E402
from plugins.web.hermes.scraper import mdgen  # noqa: E402
from plugins.web.hermes.scraper.htmlclean import parse_and_clean  # noqa: E402
from plugins.web.hermes.scraper.pipeline import ScrapeOptions, scrape_html  # noqa: E402

_WS = re.compile(r"\s+")


def _norm(s: str) -> str:
    return _WS.sub(" ", (s or "").lower()).strip()


def _present(needle: str, haystack_norm: str) -> bool:
    return _norm(needle) in haystack_norm


def _flatten_values(obj: Any, out: List[str]) -> None:
    if isinstance(obj, dict):
        for v in obj.values():
            _flatten_values(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _flatten_values(v, out)
    elif obj is not None:
        out.append(str(obj))


def _f1(a: float, b: float) -> float:
    return 0.0 if (a + b) == 0 else 2 * a * b / (a + b)


async def eval_case(case: Dict[str, Any], use_llm: bool) -> Dict[str, Any]:
    url = case["url"]
    t0 = time.time()
    fr = await _fetch.fetch(url, timeout=30, allow_browser=False)
    fetch_ms = int((time.time() - t0) * 1000)
    if not fr.ok:
        return {"id": case.get("id"), "url": url, "error": fr.error or "fetch failed",
                "status": fr.status_code}

    html = fr.html
    # Deterministic fit extraction (hermes) and the naive raw baseline, same HTML.
    res = scrape_html(html, fr.final_url or url, ScrapeOptions(include_raw=True))
    fit_md = res.markdown
    raw_md = res.raw_markdown
    fit_norm = _norm(fit_md)
    raw_norm = _norm(raw_md)

    # full visible page text for compression + label sanity
    doc = parse_and_clean(html, url)
    page_text = doc.body.text_content() or ""
    page_norm = _norm(page_text)

    inc = case.get("must_include", [])
    exc = case.get("must_exclude", [])

    # label sanity: golden must_include should exist in raw page text
    label_ok = (sum(1 for s in inc if _present(s, page_norm)) / len(inc)) if inc else 1.0

    def recall(text_norm: str) -> float:
        return (sum(1 for s in inc if _present(s, text_norm)) / len(inc)) if inc else 1.0

    def removal(text_norm: str) -> float:
        return (sum(1 for s in exc if not _present(s, text_norm)) / len(exc)) if exc else 1.0

    fit_recall, fit_removal = recall(fit_norm), removal(fit_norm)
    raw_recall, raw_removal = recall(raw_norm), removal(raw_norm)

    compression = (len(fit_md) / max(len(page_text), 1))

    result = {
        "id": case.get("id"), "url": url, "status": fr.status_code,
        "title": res.title[:60], "fetch_ms": fetch_ms, "extract_ms": res.elapsed_ms,
        "page_text_chars": len(page_text), "fit_chars": len(fit_md), "raw_md_chars": len(raw_md),
        "label_ok": round(label_ok, 3),
        "hermes": {
            "content_recall": round(fit_recall, 3),
            "boilerplate_removal": round(fit_removal, 3),
            "quality_f1": round(_f1(fit_recall, fit_removal), 3),
            "compression": round(compression, 4),
        },
        "baseline_raw": {
            "content_recall": round(raw_recall, 3),
            "boilerplate_removal": round(raw_removal, 3),
            "quality_f1": round(_f1(raw_recall, raw_removal), 3),
        },
        "missed_include": [s for s in inc if not _present(s, fit_norm)],
        "leaked_exclude": [s for s in exc if _present(s, fit_norm)],
    }

    # ── structured extraction (LLM) ──
    if use_llm and case.get("structured_enabled"):
        try:
            from plugins.web.hermes.scraper import llm_extract

            if llm_extract.aux_available():
                schema = json.loads(case.get("structured_schema_json") or "{}")
                expected = json.loads(case.get("structured_expected_json") or "{}")
                out = await llm_extract.extract_structured(
                    fit_md, schema, instruction=case.get("structured_prompt", ""), title=res.title,
                )
                data = out.get("data")
                exp_vals: List[str] = []
                _flatten_values(expected, exp_vals)
                got_blob = _norm(json.dumps(data, ensure_ascii=False)) if data is not None else ""
                hits = sum(1 for v in exp_vals if _norm(v) in got_blob)
                result["structured"] = {
                    "field_accuracy": round(hits / len(exp_vals), 3) if exp_vals else None,
                    "parsed": data is not None,
                    "error": out.get("error"),
                    "data": data,
                }
        except Exception as exc:  # noqa: BLE001
            result["structured"] = {"error": str(exc)[:200]}
    return result


def _agg(rows: List[Dict[str, Any]], key: str, sub: str) -> float:
    vals = [r[key][sub] for r in rows if key in r and r[key].get(sub) is not None]
    return round(sum(vals) / len(vals), 3) if vals else 0.0


async def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    use_llm = "--no-llm" not in sys.argv
    here = Path(__file__).resolve().parent
    path = Path(args[0]) if args else (here / "cases.json" if (here / "cases.json").exists() else here / "cases.seed.json")
    data = json.loads(path.read_text())
    cases = data["cases"] if isinstance(data, dict) else data
    print(f"Loaded {len(cases)} cases from {path.name}  (llm={'on' if use_llm else 'off'})\n")

    rows: List[Dict[str, Any]] = []
    for case in cases:
        try:
            r = await eval_case(case, use_llm)
        except Exception as exc:  # noqa: BLE001
            r = {"id": case.get("id"), "url": case.get("url"), "error": str(exc)[:200]}
        rows.append(r)
        if "error" in r:
            print(f"  ✗ {r.get('id'):20} ERROR {r['error']}")
            continue
        h = r["hermes"]
        warn = ""
        if r["label_ok"] < 1.0:
            warn += f"  ⚠label_ok={r['label_ok']}"
        if r["missed_include"]:
            warn += f"  miss={r['missed_include']}"
        if r["leaked_exclude"]:
            warn += f"  leak={r['leaked_exclude']}"
        struct = ""
        if "structured" in r and r["structured"].get("field_accuracy") is not None:
            struct = f"  struct_acc={r['structured']['field_accuracy']}"
        print(f"  {r['id']:20} recall={h['content_recall']:.2f} rm_boiler={h['boilerplate_removal']:.2f} "
              f"f1={h['quality_f1']:.2f} compress={h['compression']:.3f}{struct}{warn}")

    ok = [r for r in rows if "hermes" in r]
    print("\n── AGGREGATE ──")
    print(f"  cases evaluated:        {len(ok)}/{len(rows)}")
    print(f"  HERMES   recall={_agg(ok,'hermes','content_recall'):.3f}  "
          f"rm_boiler={_agg(ok,'hermes','boilerplate_removal'):.3f}  "
          f"f1={_agg(ok,'hermes','quality_f1'):.3f}  compress={_agg(ok,'hermes','compression'):.3f}")
    print(f"  BASELINE recall={_agg(ok,'baseline_raw','content_recall'):.3f}  "
          f"rm_boiler={_agg(ok,'baseline_raw','boilerplate_removal'):.3f}  "
          f"f1={_agg(ok,'baseline_raw','quality_f1'):.3f}")
    structs = [r["structured"]["field_accuracy"] for r in ok
               if "structured" in r and r["structured"].get("field_accuracy") is not None]
    if structs:
        print(f"  STRUCTURED field_accuracy={sum(structs)/len(structs):.3f}  (n={len(structs)})")

    out_path = here / "last_results.json"
    out_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2))
    print(f"\n  wrote {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
