"""LLM extraction layer — engaged only when the caller asks a question
(``query``) or wants structured output (``schema``).

Two strategies, both built on Hermes' auxiliary text client:

  * :func:`extract_answer` — map-reduce over BM25-pre-filtered chunks: extract
    from each relevant chunk in parallel, then synthesise one answer.
  * :func:`extract_structured` — schema-constrained JSON extraction with a parse
    + (optional) jsonschema-validate + one retry.

These keep the deterministic markdown path free of any model call; the model is
a precision tool we reach for, not the default.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Dict, List, Optional

from plugins.web.hermes.scraper import bm25, chunking

logger = logging.getLogger(__name__)

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


async def _aux_call(
    system: str, user: str, *, model: Optional[str], max_tokens: int = 4000,
    temperature: float = 0.1,
) -> Optional[str]:
    """One auxiliary-LLM round-trip, reusing the web_extract aux resolution."""
    from agent.auxiliary_client import async_call_llm, extract_content_or_reasoning
    from tools.web_tools import _resolve_web_extract_auxiliary

    client, effective_model, extra_body = _resolve_web_extract_auxiliary(model)
    if client is None or not effective_model:
        return None
    kwargs: Dict[str, Any] = {
        "task": "web_extract",
        "model": effective_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if extra_body:
        kwargs["extra_body"] = extra_body
    resp = await async_call_llm(**kwargs)
    return extract_content_or_reasoning(resp)


def aux_available() -> bool:
    try:
        from tools.web_tools import check_auxiliary_model

        return bool(check_auxiliary_model())
    except Exception:  # noqa: BLE001
        return False


# ───────────────────────────── query (map-reduce) ─────────────────────────────

_MAP_SYS = (
    "You extract information from ONE section of a web page to answer a user's "
    "request. Output only the facts, quotes, figures and details from THIS "
    "section that are relevant to the request. No preamble, no conclusions. If "
    "this section has nothing relevant, reply exactly with: NONE."
)
_REDUCE_SYS = (
    "You synthesise per-section extracts into one cohesive, accurate answer to "
    "the user's request. Preserve specific facts, numbers, quotes and code "
    "verbatim. Use clean markdown. Do not invent anything not in the extracts."
)


async def extract_answer(
    content: str, query: str, *, title: str = "", model: Optional[str] = None,
    max_input_tokens: int = 9000,
) -> str:
    """Answer ``query`` over ``content`` via BM25-filtered map-reduce."""
    chunks = chunking.chunk_text(content, max_tokens=1800, overlap_tokens=150)
    if not chunks:
        return ""
    if len(chunks) == 1:
        out = await _aux_call(
            _REDUCE_SYS,
            f"Request: {query}\n\nPage title: {title}\n\nCONTENT:\n{chunks[0]}",
            model=model, max_tokens=4000,
        )
        return (out or "").strip()

    # Pre-filter chunks to the relevance budget before spending map calls.
    relevant = bm25.rank_blocks(
        chunks, query, max_tokens=max_input_tokens, token_counter=chunking.estimate_tokens,
    )
    if not relevant:
        relevant = chunks[:4]

    async def _map(ch: str) -> str:
        out = await _aux_call(
            _MAP_SYS, f"Request: {query}\n\nSECTION:\n{ch}", model=model, max_tokens=1500,
        )
        return (out or "").strip()

    extracts = await asyncio.gather(*[_map(c) for c in relevant], return_exceptions=True)
    good = [e for e in extracts if isinstance(e, str) and e and e.strip().upper() != "NONE"]
    if not good:
        return ""
    if len(good) == 1:
        return good[0]
    merged = "\n\n---\n\n".join(f"[section {i+1}]\n{e}" for i, e in enumerate(good))
    out = await _aux_call(
        _REDUCE_SYS, f"Request: {query}\n\nPER-SECTION EXTRACTS:\n{merged}",
        model=model, max_tokens=4000,
    )
    return (out or merged).strip()


# ───────────────────────────── structured (schema) ────────────────────────────

def _parse_json(text: str) -> Optional[Any]:
    if not text:
        return None
    text = text.strip()
    m = _JSON_FENCE_RE.search(text)
    if m:
        text = m.group(1).strip()
    # find first { or [ … last } or ]
    start = min([i for i in (text.find("{"), text.find("[")) if i >= 0], default=-1)
    if start > 0:
        text = text[start:]
    for end in (len(text), text.rfind("}") + 1, text.rfind("]") + 1):
        if end <= 0:
            continue
        try:
            return json.loads(text[:end])
        except (json.JSONDecodeError, ValueError):
            continue
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


def _validate(obj: Any, schema: Dict[str, Any]) -> Optional[str]:
    try:
        import jsonschema  # noqa: WPS433 — optional

        jsonschema.validate(obj, schema)
        return None
    except ImportError:
        return None  # can't validate; accept
    except Exception as exc:  # noqa: BLE001 — jsonschema.ValidationError
        return str(exc).split("\n")[0]


async def extract_structured(
    content: str, schema: Dict[str, Any], *, instruction: str = "",
    title: str = "", model: Optional[str] = None, max_input_tokens: int = 9000,
) -> Dict[str, Any]:
    """Extract a schema-constrained JSON object from ``content``.

    Returns ``{"data": <obj>}`` on success or ``{"error": str, "raw": str}``.
    """
    chunks = chunking.chunk_text(content, max_tokens=2200, overlap_tokens=120)
    if not chunks:
        return {"error": "no content"}
    query = instruction or json.dumps(schema.get("properties", schema))[:400]
    if len(chunks) > 1:
        relevant = bm25.rank_blocks(
            chunks, query, max_tokens=max_input_tokens, token_counter=chunking.estimate_tokens,
        )
        body = "\n\n".join(relevant or chunks[:3])
    else:
        body = chunks[0]

    schema_str = json.dumps(schema, indent=2)
    system = (
        "You are a precise web data extractor. Extract the requested fields from "
        "the page content and return ONLY a single JSON value that conforms to the "
        "provided JSON Schema. No markdown, no commentary, no code fences. Use null "
        "for fields genuinely not present. Copy values verbatim from the page."
    )
    base_user = (
        f"{('Task: ' + instruction) if instruction else ''}\n\n"
        f"JSON Schema:\n{schema_str}\n\n"
        f"Page title: {title}\n\nPAGE CONTENT:\n{body}\n\n"
        "Return only the JSON value."
    )

    last_raw = ""
    for attempt in range(2):
        user = base_user if attempt == 0 else (
            base_user + f"\n\nYour previous output did not parse/validate as JSON "
                        f"matching the schema. Return ONLY valid JSON.\nPrevious: {last_raw[:600]}"
        )
        raw = await _aux_call(system, user, model=model, max_tokens=3000, temperature=0.0)
        last_raw = raw or ""
        obj = _parse_json(last_raw)
        if obj is not None:
            err = _validate(obj, schema)
            if err is None:
                return {"data": obj}
            if attempt == 1:
                return {"data": obj, "schema_warning": err}
    return {"error": "could not parse structured output", "raw": last_raw[:1000]}
