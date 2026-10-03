#!/usr/bin/env python3
"""Machine-checked citation / provenance validation for research answers.

Hermes' verification today is the model self-grading: a prose instruction to
"cite >=2 sources." Nothing in code checks that a cited URL was *actually
retrieved*. This module closes that gap with a small, dependency-free,
deterministic checker — the difference between "looks cited" and "is grounded."

Design principles (from the deep-research review — efficient-deep-research's
hash-id + format-validator + regenerate loop, onyx's ``collapse_citations``
global renumber, btahir's ``usedSources``):

* **Never trust the model to renumber.** Renumbering is a pure code transform
  (:func:`renumber_citations`).
* **Catch fabrication, not style.** The load-bearing check is "cited URL was
  seen in some tool result" — a URL the model invented and never retrieved is
  the real correctness failure. We compute softer signals too, but only
  fabrication drives a revise request.
* **Warning-then-revise, never a hard block.** The loop hook
  (:func:`build_citation_revise_nudge`) asks the model to fix fabricated
  citations; it never deletes the user's answer or fails the run.

Pure functions only — no agent/loop imports — so this is trivially testable and
independently revertible.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode


# A URL ends at whitespace or a delimiter that commonly closes it in prose /
# markdown: closing paren/bracket/brace, quotes, angle brackets, backtick.
_URL_RE = re.compile(r"https?://[^\s)\]}>\"'`]+", re.IGNORECASE)

# Trailing punctuation that is almost always prose, not part of the URL.
_TRAILING_PUNCT = ".,;:!?"

# Numeric citation markers like [1], [12], [3, 4], [3][4].
_NUM_CITATION_RE = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")

# A bibliography line: "[3] https://…" (optionally "[3]: https://…") — these are
# source listings, not claims, so the claim/citation pairer skips them and the
# sources-map parser consumes them.
_SOURCES_LINE_RE = re.compile(r"^\s*\[(\d+)\]:?\s+<?(https?://\S+?)>?\s*$")

# Markdown links [text](url) — the URL is a citation for the surrounding claim.
_MD_LINK_URL_RE = re.compile(r"\]\((https?://[^)\s]+)\)")

# Words this short carry no signal for excerpt selection.
_WORD_RE = re.compile(r"[a-z0-9][a-z0-9\-']{3,}")

# Tracking params we strip so the same link cited two ways still matches.
_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "fbclid", "gclid", "gclsrc", "dclid", "msclkid", "mc_cid",
    "mc_eid", "ref", "ref_src", "ref_url", "spm", "igshid",
}

# Tool names whose results count as "the model saw / retrieved this URL".
_RETRIEVAL_TOOLS = {"web_extract"}
_DISCOVERY_TOOLS = {"web_search"}
# Sub-researchers (spawned via delegate_task with the research contract) retrieve
# pages in their own isolated context and return a cited report; the URLs in that
# report are legitimate provenance for the parent, so they count as retrieved
# (otherwise the parent citing a sub-report's source would be falsely flagged as
# fabricated).
_DELEGATION_TOOLS = {"delegate_task"}
_ALL_WEB_TOOLS = _RETRIEVAL_TOOLS | _DISCOVERY_TOOLS | _DELEGATION_TOOLS

# Keys in tool JSON whose values are source URLs.
_URL_KEYS = {"url", "sourceurl", "source_url", "link", "href", "finalurl", "final_url"}


def canonicalize_url(url: str) -> str:
    """Normalize a URL so two spellings of the same source compare equal.

    Lowercases scheme + host, drops the fragment, removes tracking params,
    sorts the remaining query, and strips a trailing slash from the path. Best
    effort: anything unparseable is returned stripped/lowercased.
    """
    if not url:
        return ""
    url = url.strip().rstrip(_TRAILING_PUNCT).strip()
    try:
        parts = urlsplit(url)
        if not parts.scheme or not parts.netloc:
            return url.lower()
        scheme = parts.scheme.lower()
        netloc = parts.netloc.lower()
        # Drop a default port.
        if netloc.endswith(":80") and scheme == "http":
            netloc = netloc[:-3]
        elif netloc.endswith(":443") and scheme == "https":
            netloc = netloc[:-4]
        # Normalize trailing slashes so "/path/" == "/path" and a lone "/"
        # (root) == "" — the same source cited with or without a slash matches.
        path = (parts.path or "").rstrip("/")
        query_pairs = [
            (k, v) for (k, v) in parse_qsl(parts.query, keep_blank_values=True)
            if k.lower() not in _TRACKING_PARAMS
        ]
        query = urlencode(sorted(query_pairs))
        return urlunsplit((scheme, netloc, path, query, ""))
    except Exception:  # noqa: BLE001 — never raise from a normalizer
        return url.lower()


def extract_urls(text: str) -> List[str]:
    """Return all raw URLs found in *text*, de-duplicated, order preserved."""
    if not text:
        return []
    seen: Set[str] = set()
    out: List[str] = []
    for m in _URL_RE.finditer(text):
        raw = m.group(0)
        # The regex stops at ')', which truncates URLs that legitimately
        # contain parens (e.g. wikipedia .../Foo_(bar)). Re-attach closing
        # parens while the URL has unmatched '(' — a markdown link's closing
        # ')' stays excluded because such URLs are already balanced.
        end = m.end()
        while end < len(text) and text[end] == ")" and raw.count("(") > raw.count(")"):
            raw += ")"
            end += 1
        raw = raw.rstrip(_TRAILING_PUNCT)
        if raw and raw not in seen:
            seen.add(raw)
            out.append(raw)
    return out


def _walk_for_urls(obj: Any, out: List[str]) -> None:
    """Collect URL-valued strings from a parsed-JSON structure."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, str) and (
                k.lower() in _URL_KEYS or v.startswith(("http://", "https://"))
            ):
                if v.startswith(("http://", "https://")):
                    out.append(v)
            else:
                _walk_for_urls(v, out)
    elif isinstance(obj, list):
        for item in obj:
            _walk_for_urls(item, out)
    elif isinstance(obj, str) and obj.startswith(("http://", "https://")):
        out.append(obj)


def _urls_from_tool_content(content: str) -> List[str]:
    """Pull URLs out of one tool result's content (JSON-aware, regex fallback)."""
    if not content:
        return []
    urls: List[str] = []
    try:
        parsed = json.loads(content)
        _walk_for_urls(parsed, urls)
    except (ValueError, TypeError):
        pass
    # Always also regex the raw text — covers non-JSON results and URLs that
    # only appear inside content bodies. Being permissive here only makes the
    # fabrication check *safer* (fewer false positives).
    urls.extend(extract_urls(content))
    return urls


def _iter_tool_messages(messages: Sequence[Dict[str, Any]]) -> Iterable[Tuple[str, str]]:
    """Yield (tool_name, content_str) for every tool-result message."""
    for msg in messages or []:
        if not isinstance(msg, dict) or msg.get("role") != "tool":
            continue
        name = (msg.get("name") or "").strip()
        content = msg.get("content")
        if isinstance(content, list):
            # Some providers wrap tool content as a list of parts.
            content = " ".join(
                part.get("text", "") if isinstance(part, dict) else str(part)
                for part in content
            )
        if not isinstance(content, str):
            content = "" if content is None else str(content)
        yield name, content


def collect_known_urls(
    messages: Sequence[Dict[str, Any]],
) -> Tuple[Set[str], Set[str]]:
    """Return ``(known_urls, extracted_urls)`` (canonicalized) from tool results.

    * ``known_urls`` — every URL that appeared in ANY web tool result
      (search or extract). A cited URL outside this set was never seen by the
      model → fabricated.
    * ``extracted_urls`` — URLs surfaced specifically by ``web_extract`` (i.e.
      pages actually read). A cited URL in ``known`` but not ``extracted`` was
      seen in search but never read → uncorroborated (soft signal).
    """
    known: Set[str] = set()
    extracted: Set[str] = set()
    for name, content in _iter_tool_messages(messages):
        if name not in _ALL_WEB_TOOLS:
            continue
        urls = {canonicalize_url(u) for u in _urls_from_tool_content(content)}
        urls.discard("")
        known |= urls
        if name in _RETRIEVAL_TOOLS or name in _DELEGATION_TOOLS:
            extracted |= urls
    return known, extracted


def web_research_occurred(messages: Sequence[Dict[str, Any]]) -> bool:
    """True if any web search/extract tool result is present in the history."""
    for name, _ in _iter_tool_messages(messages):
        if name in _ALL_WEB_TOOLS:
            return True
    return False


@dataclass
class CitationReport:
    """Outcome of validating an answer's citations against retrieved sources."""

    cited_urls: List[str] = field(default_factory=list)        # canonical, as cited
    known_urls: Set[str] = field(default_factory=set)          # seen via any web tool
    extracted_urls: Set[str] = field(default_factory=set)      # read via web_extract
    fabricated: List[str] = field(default_factory=list)        # cited, never seen (HARD)
    uncorroborated: List[str] = field(default_factory=list)    # cited, seen-not-read (soft)
    numeric_markers: List[int] = field(default_factory=list)   # [n] markers found

    @property
    def has_citations(self) -> bool:
        return bool(self.cited_urls) or bool(self.numeric_markers)

    @property
    def ok(self) -> bool:
        """A report is OK unless it cites a URL that was never retrieved."""
        return not self.fabricated

    def to_dict(self) -> Dict[str, Any]:
        return {
            "cited_count": len(self.cited_urls),
            "known_count": len(self.known_urls),
            "extracted_count": len(self.extracted_urls),
            "fabricated": list(self.fabricated),
            "uncorroborated": list(self.uncorroborated),
            "ok": self.ok,
        }


def validate_answer_citations(
    answer_text: str,
    messages: Sequence[Dict[str, Any]],
) -> CitationReport:
    """Validate that every URL cited in *answer_text* was actually retrieved.

    This is the core check. It never raises and never mutates anything.
    """
    known, extracted = collect_known_urls(messages)
    cited_raw = extract_urls(answer_text)
    cited = []
    seen: Set[str] = set()
    for u in cited_raw:
        c = canonicalize_url(u)
        if c and c not in seen:
            seen.add(c)
            cited.append(c)

    numeric = []
    for m in _NUM_CITATION_RE.finditer(answer_text or ""):
        for piece in m.group(1).split(","):
            piece = piece.strip()
            if piece.isdigit():
                numeric.append(int(piece))

    fabricated = [u for u in cited if u not in known]
    uncorroborated = [u for u in cited if u in known and u not in extracted]

    return CitationReport(
        cited_urls=cited,
        known_urls=known,
        extracted_urls=extracted,
        fabricated=fabricated,
        uncorroborated=uncorroborated,
        numeric_markers=sorted(set(numeric)),
    )


# ---------------------------------------------------------------------------
# Claim ↔ citation pairing + source-content collection (A1 semantic pass).
#
# Pure helpers only: they turn a final report + the session's tool results into
# (claim, cited-URL, source-excerpt) material for the auxiliary-LLM support
# check that lives in agent/verification_stop.py. No model calls here.
# ---------------------------------------------------------------------------


def parse_sources_map(text: str) -> Dict[int, str]:
    """Map numeric source ids to URLs from ``[n] url`` bibliography lines."""
    out: Dict[int, str] = {}
    for line in (text or "").splitlines():
        m = _SOURCES_LINE_RE.match(line)
        if m:
            out.setdefault(int(m.group(1)), m.group(2).rstrip(_TRAILING_PUNCT))
    return out


def _iter_claim_blocks(text: str) -> Iterable[str]:
    """Yield claim-sized text blocks: one per bullet line, one per paragraph.

    Headings, bibliography (``[n] url``) lines, table rules, and code fences
    are skipped — they aren't claims.
    """
    para: List[str] = []
    in_fence = False
    for raw in (text or "").splitlines():
        line = raw.strip()
        if line.startswith("```"):
            in_fence = not in_fence
            line = ""
        if in_fence:
            line = ""
        is_bullet = bool(re.match(r"^([-*+]|\d+[.)])\s+", line))
        skip = (
            not line
            or line.startswith("#")
            or _SOURCES_LINE_RE.match(line)
            or set(line) <= set("-|: =")  # table rules / separators
        )
        if skip or is_bullet:
            if para:
                yield " ".join(para)
                para = []
            if is_bullet and not skip:
                yield line
            continue
        para.append(line)
    if para:
        yield " ".join(para)


def _clean_claim(block: str, max_chars: int = 500) -> str:
    """Strip citation clutter so the judge sees prose, not markup."""
    s = re.sub(r"^([-*+]|\d+[.)])\s+", "", block)          # bullet prefix
    s = re.sub(r"\[([^\]]*)\]\(https?://[^)\s]+\)", r"\1", s)  # md link → text
    s = _URL_RE.sub("", s)                                   # raw URLs
    s = _NUM_CITATION_RE.sub("", s)                          # [n] markers
    s = re.sub(r"\s+", " ", s).strip(" \t-–—:;,")
    return s[:max_chars].strip()


def extract_claim_citation_pairs(
    answer_text: str,
    *,
    min_claim_chars: int = 30,
    max_pairs: int = 64,
) -> List[Tuple[str, str]]:
    """Return ``(claim_text, cited_url)`` pairs from a final research answer.

    A claim is a bullet or paragraph; its citations are inline URLs, markdown
    links, and ``[n]`` markers resolved through the answer's ``[n] url``
    bibliography. Pairs are deduped on (claim, canonical url), order preserved.
    """
    sources = parse_sources_map(answer_text)
    pairs: List[Tuple[str, str]] = []
    seen: Set[Tuple[str, str]] = set()
    for block in _iter_claim_blocks(answer_text):
        cited: List[str] = list(extract_urls(block))
        for m in _NUM_CITATION_RE.finditer(block):
            for piece in m.group(1).split(","):
                piece = piece.strip()
                if piece.isdigit() and int(piece) in sources:
                    cited.append(sources[int(piece)])
        if not cited:
            continue
        claim = _clean_claim(block)
        if len(claim) < min_claim_chars:
            continue
        for url in cited:
            key = (claim, canonicalize_url(url))
            if key[1] and key not in seen:
                seen.add(key)
                pairs.append((claim, url))
                if len(pairs) >= max_pairs:
                    return pairs
    return pairs


def interleave_pairs_by_url(
    pairs: Sequence[Tuple[str, str]],
) -> List[Tuple[str, str]]:
    """Round-robin pairs across distinct URLs so a capped check budget spreads
    over sources instead of burning entirely on the first-cited URL."""
    buckets: Dict[str, List[Tuple[str, str]]] = {}
    order: List[str] = []
    for claim, url in pairs:
        c = canonicalize_url(url)
        if c not in buckets:
            buckets[c] = []
            order.append(c)
        buckets[c].append((claim, url))
    out: List[Tuple[str, str]] = []
    while len(out) < len(pairs):
        for c in order:
            if buckets[c]:
                out.append(buckets[c].pop(0))
    return out


# Keys whose string values are page bodies in web tool results.
_CONTENT_KEYS = ("content", "raw_content", "markdown", "text")
_URL_VALUE_KEYS = ("url", "final_url", "finalurl", "source_url", "sourceurl", "link")


def _walk_for_contents(obj: Any, out: Dict[str, str]) -> None:
    if isinstance(obj, dict):
        url = None
        for k in _URL_VALUE_KEYS:
            v = obj.get(k)
            if isinstance(v, str) and v.startswith(("http://", "https://")):
                url = v
                break
        if url:
            body = "\n\n".join(
                obj[k] for k in _CONTENT_KEYS
                if isinstance(obj.get(k), str) and obj[k].strip()
            )
            c = canonicalize_url(url)
            if c and body and len(body) > len(out.get(c, "")):
                out[c] = body
        for v in obj.values():
            _walk_for_contents(v, out)
    elif isinstance(obj, list):
        for item in obj:
            _walk_for_contents(item, out)


def collect_source_contents(messages: Sequence[Dict[str, Any]]) -> Dict[str, str]:
    """Map canonical URL → retrieved page content from web_extract results.

    Only retrieval tools count — a search snippet is not source content. When
    the same URL was extracted twice, the longest body wins.
    """
    out: Dict[str, str] = {}
    for name, content in _iter_tool_messages(messages):
        if name not in _RETRIEVAL_TOOLS:
            continue
        try:
            parsed = json.loads(content)
        except (ValueError, TypeError):
            continue
        _walk_for_contents(parsed, out)
    return out


def select_support_excerpt(content: str, claim: str, max_chars: int = 4000) -> str:
    """Pick the slice of *content* most likely to bear on *claim*.

    Deterministic word-overlap scoring over paragraphs, window expanded around
    the best hit; falls back to the head when nothing overlaps. Keeps the
    per-check aux prompt bounded no matter how large the page was.
    """
    content = (content or "").strip()
    if len(content) <= max_chars:
        return content
    claim_words = set(_WORD_RE.findall((claim or "").lower()))
    paras = [p.strip() for p in re.split(r"\n\s*\n", content) if p.strip()]
    if not claim_words or not paras:
        return content[:max_chars]
    scores = [
        len(claim_words & set(_WORD_RE.findall(p.lower()))) for p in paras
    ]
    best = max(range(len(paras)), key=lambda i: scores[i])
    if scores[best] == 0:
        return content[:max_chars]
    # Grow a window around the best paragraph until the budget is spent.
    picked = [paras[best]]
    lo, hi = best - 1, best + 1
    size = len(paras[best])
    while size < max_chars and (lo >= 0 or hi < len(paras)):
        grew = False
        if hi < len(paras) and size + len(paras[hi]) + 2 <= max_chars:
            picked.append(paras[hi])
            size += len(paras[hi]) + 2
            hi += 1
            grew = True
        if lo >= 0 and size + len(paras[lo]) + 2 <= max_chars:
            picked.insert(0, paras[lo])
            size += len(paras[lo]) + 2
            lo -= 1
            grew = True
        if not grew:
            break
    return "\n\n".join(picked)[:max_chars]


def assign_source_ids(urls: Sequence[str]) -> Dict[str, int]:
    """Assign stable 1-based ids to URLs in first-seen order (canonicalized)."""
    mapping: Dict[str, int] = {}
    n = 0
    for u in urls:
        c = canonicalize_url(u)
        if c and c not in mapping:
            n += 1
            mapping[c] = n
    return mapping


def renumber_citations(
    text: str,
    ordered_urls: Sequence[str],
) -> Tuple[str, List[Tuple[int, str]]]:
    """Collapse numeric citation markers to a dense 1..k in order of appearance.

    Pure, deterministic — the renumber is NEVER delegated to the model. Returns
    the rewritten text and the ``[(new_id, url)]`` source list. ``ordered_urls``
    maps the ORIGINAL marker numbers (1-based index → url) so we can remap.

    Markers that reference an unknown original number are left as-is (we do not
    invent a source for them). Only markers seen in the text get new ids, so the
    final list has no gaps.
    """
    if not text:
        return text, []
    orig_to_url = {i + 1: u for i, u in enumerate(ordered_urls)}
    remap: Dict[int, int] = {}
    next_id = 1

    def _first_pass(match: re.Match) -> str:
        nonlocal next_id
        for piece in match.group(1).split(","):
            piece = piece.strip()
            if piece.isdigit():
                old = int(piece)
                if old in orig_to_url and old not in remap:
                    remap[old] = next_id
                    next_id += 1
        return match.group(0)

    # First pass only populates remap in appearance order.
    _NUM_CITATION_RE.sub(_first_pass, text)

    def _second_pass(match: re.Match) -> str:
        nums = []
        for piece in match.group(1).split(","):
            piece = piece.strip()
            if piece.isdigit() and int(piece) in remap:
                nums.append(str(remap[int(piece)]))
        if not nums:
            return match.group(0)
        return "".join(f"[{n}]" for n in nums)

    new_text = _NUM_CITATION_RE.sub(_second_pass, text)
    source_list = sorted(
        ((new_id, orig_to_url[old]) for old, new_id in remap.items()),
        key=lambda t: t[0],
    )
    return new_text, source_list


def build_citation_revise_nudge(
    answer_text: str,
    messages: Sequence[Dict[str, Any]],
    *,
    attempts: int = 0,
    max_attempts: int = 1,
) -> Optional[str]:
    """Return a revise instruction when the answer cites URLs never retrieved.

    Warning-then-revise: returns ``None`` (no nudge) when the answer is clean,
    when no web research happened, when there are no citations, or when the
    attempt budget is spent — so a valid final answer is never blocked and the
    loop can't spin. Only *fabricated* citations trigger a nudge; the
    uncorroborated (seen-in-search-not-read) set is surfaced as a softer note
    inside the same nudge when a nudge is already warranted.
    """
    if attempts >= max_attempts:
        return None
    if not web_research_occurred(messages):
        return None
    report = validate_answer_citations(answer_text, messages)
    if report.ok:
        return None

    fabricated = report.fabricated[:8]
    lines = [
        "[System: citation check — some URLs in your answer were never "
        "retrieved by web_search or web_extract in this session, so they are "
        "unverified and may be fabricated. Ground your answer only in sources "
        "you actually retrieved.",
        "",
        "Cited but NOT retrieved:",
    ]
    lines += [f"  - {u}" for u in fabricated]
    if len(report.fabricated) > len(fabricated):
        lines.append(f"  - … and {len(report.fabricated) - len(fabricated)} more")
    lines += [
        "",
        "Fix this now: for each, either (a) retrieve the page with web_extract "
        "and keep the citation only if it supports the claim, or (b) replace it "
        "with a source you did retrieve, or (c) remove the claim/citation. Do "
        "not invent or guess URLs. Then re-emit the corrected answer.]",
    ]
    return "\n".join(lines)
