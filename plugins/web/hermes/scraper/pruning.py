"""Heuristic content extraction — the deterministic denoiser.

Two phases, mirroring how Readability / trafilatura / Firecrawl isolate article
text (and adapting crawl4ai's ``PruningContentFilter`` scoring):

  1. **Locate the main-content root.** Score every leaf content block (``p``,
     headings, ``li``, ``pre`` …) by length/quality, propagate those scores up
     to ancestors with distance decay, apply semantic (``<main>``/``role=main``/
     ``<article>``) and class/id multipliers, and pick the highest-scoring
     container. This avoids the classic failure where a content *wrapper* is
     deleted because surrounding chrome makes its aggregate text-density look
     low (e.g. Wikipedia's nested nav).

  2. **Prune boilerplate within that root.** Walk the chosen subtree and drop
     residual chrome blocks (related-link boxes, share widgets, inline nav,
     comment threads) via a weighted composite score — content-heavy wrappers
     are protected so we never amputate real text.

Both phases are cheap, language-agnostic and LLM-free.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, Set

from lxml import etree
from lxml import html as _lxml_html

_TAG_WEIGHTS: Dict[str, float] = {
    "article": 1.5, "main": 1.4, "section": 1.0, "p": 1.0,
    "h1": 1.2, "h2": 1.15, "h3": 1.1, "h4": 1.0, "h5": 0.9, "h6": 0.8,
    "blockquote": 1.0, "pre": 1.1, "figure": 0.9, "figcaption": 0.8,
    "table": 1.0, "ul": 0.7, "ol": 0.7, "li": 0.6, "dl": 0.7,
    "div": 0.5, "details": 0.6, "summary": 0.5,
    "header": 0.2, "footer": 0.05, "nav": 0.0, "aside": 0.1, "form": 0.0,
}
_MAX_TAG_WEIGHT = 1.5

_NEGATIVE_RE = re.compile(
    r"(?:^|[-_ ])(?:nav|navbar|menu|sidebar|side-bar|footer|header|masthead|"
    r"banner|breadcrumb|crumb|pager|pagination|share|social|sharing|"
    r"comment|disqus|related|recommend|promo|sponsor|advert|advertis|"
    r"ads?|adslot|cookie|consent|gdpr|popup|modal|overlay|newsletter|"
    r"subscribe|signup|sign-up|widget|toolbar|metanav|hidden|skip-link|"
    r"site-header|site-footer|global-nav|utility|legal|copyright|infobox-)",
    re.IGNORECASE,
)
_POSITIVE_RE = re.compile(
    r"(?:^|[-_ ])(?:article|content|main|post|entry|story|body|prose|markdown|"
    r"text|blog|page-content|rich-text|mw-parser-output|answer|question|"
    r"hentry|h-entry|story-body|articlebody)",
    re.IGNORECASE,
)

_LEAF_KEEP: Set[str] = {
    "p", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "code",
    "figcaption", "caption", "td", "th", "tr", "thead", "tbody", "tfoot",
    "a", "img", "br", "hr", "em", "strong", "b", "i", "u", "s", "sub", "sup",
    "mark", "small", "abbr", "time", "kbd", "var", "cite", "q", "dt", "dd",
    "picture", "figure", "span", "label",
}

# Block containers that phase-2 scores & may prune. (span intentionally absent —
# it's inline and pruning it independently shreds content.)
_BLOCK_CANDIDATES: Set[str] = {
    "div", "section", "article", "aside", "nav", "header", "footer", "main",
    "ul", "ol", "dl", "table", "form", "details", "address", "fieldset",
}

# Block-level tags that break inline flow — used to compute an element's
# "own" (direct) text for phase-1 scoring.
_BLOCK_LEVEL: Set[str] = {
    "address", "article", "aside", "blockquote", "details", "dialog", "dd",
    "div", "dl", "dt", "fieldset", "figcaption", "figure", "footer", "form",
    "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li", "main", "nav",
    "ol", "p", "pre", "section", "table", "thead", "tbody", "tfoot", "tr",
    "td", "th", "ul", "video", "summary",
}
_HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}

_BAD_ROLES = {"navigation", "banner", "contentinfo", "complementary", "search", "menu", "menubar", "tablist", "dialog"}
_GOOD_ROLES = {"main", "article", "document"}

# Tags eligible to be *selected* as content leaves in collection mode. Excludes
# structural roots (body/html/table/tr/td/section/article/main) so a wrapper
# can never drag the whole page in.
_COLLECT_TAGS = {
    "p", "li", "blockquote", "pre", "h1", "h2", "h3", "h4", "h5", "h6",
    "dd", "dt", "figcaption", "span", "div", "caption",
}

# Headings that introduce boilerplate sections — NOT protected from pruning, so
# their (typically link-list) bodies and the orphan heading are removed.
_BOILERPLATE_HEADING_RE = re.compile(
    r"^\s*(?:see also|related(?: articles| posts| topics)?|references|external links|"
    r"further reading|navigation|footnotes?|citations?|sources|bibliography|"
    r"bibliographic|submission history|citation tools|access paper|"
    r"sidebar|advertisement|sponsored|newsletter|subscribe|comments?|"
    r"share this|more from|recommended|trending|popular|tags?)\b",
    re.IGNORECASE,
)


def _has_content_heading(el) -> bool:
    """True if ``el`` contains a non-boilerplate heading with text."""
    for h in el.iter("h1", "h2", "h3", "h4"):
        t = (h.text_content() or "").strip()
        if t and not _BOILERPLATE_HEADING_RE.match(t):
            return True
    return False


@dataclass
class PruneConfig:
    threshold: float = 0.48
    dynamic: bool = True
    min_word_count: int = 3
    protect_word_count: int = 50      # content-heavy wrappers are never removed
    protect_link_density: float = 0.40
    weights: Dict[str, float] = field(default_factory=lambda: {
        "text_density": 0.40,
        "link_density": 0.20,
        "tag_prior": 0.20,
        "class_id_prior": 0.10,
        "text_length_prior": 0.10,
    })
    text_length_saturation: int = 400


_WS_RE = re.compile(r"\s+")


def _text(el) -> str:
    return _WS_RE.sub(" ", (el.text_content() or "")).strip()


def _link_text_len(el) -> int:
    return sum(len(_WS_RE.sub(" ", (a.text_content() or "")).strip()) for a in el.iter("a"))


def _link_density(el) -> float:
    t = len(_text(el))
    if t == 0:
        return 1.0
    return min(1.0, _link_text_len(el) / t)


def _class_blob(el) -> str:
    return " ".join(filter(None, [el.get("class", ""), el.get("id", "")]))


def _class_id_prior(el) -> float:
    blob = _class_blob(el)
    role = (el.get("role") or "").lower()
    score = 0.0
    if role in _BAD_ROLES:
        score -= 1.0
    if role in _GOOD_ROLES:
        score += 1.0
    if blob:
        if _NEGATIVE_RE.search(blob):
            score -= 1.0
        if _POSITIVE_RE.search(blob):
            score += 1.0
    return max(0.0, min(1.0, (score + 1.0) / 2.0))


# ───────────────────────── phase 1: main-content root ─────────────────────────

def _container_multiplier(el) -> float:
    tag = el.tag
    role = (el.get("role") or "").lower()
    blob = _class_blob(el)
    m = 1.0
    if tag in ("article", "main") or role in _GOOD_ROLES:
        m *= 3.0
    elif tag == "section":
        m *= 1.25
    elif tag in ("nav", "footer", "header", "aside", "form"):
        m *= 0.25
    if _POSITIVE_RE.search(blob):
        m *= 1.6
    if _NEGATIVE_RE.search(blob) or role in _BAD_ROLES:
        m *= 0.2
    return m


def _own_text(el) -> str:
    """Text that belongs DIRECTLY to ``el`` — its own text nodes plus the text of
    inline descendants, but NOT text inside nested block-level children.

    This makes a ``<span class="text">…quote…</span>`` or a bare ``<div>…</div>``
    a first-class content block (not just ``<p>``), while a wrapper ``<div>`` that
    only contains nested ``<p>``/``<div>`` blocks has little own-text and is scored
    as the container it is.
    """
    parts = [el.text or ""]
    for c in el:
        if not isinstance(c.tag, str):
            parts.append(c.tail or "")
            continue
        if c.tag in _BLOCK_LEVEL:
            # skip the block child's inner text; keep the tail (text after it)
            parts.append(c.tail or "")
        else:
            parts.append(c.text_content() or "")
            parts.append(c.tail or "")
    return _WS_RE.sub(" ", "".join(parts)).strip()


def _score_blocks(body):
    """Return ``(scores, own_scores)``.

    ``own_scores[block]`` is a block's intrinsic content contribution (from its
    own direct text). ``scores[el]`` adds distance-decayed propagation to
    ancestors, so containers accumulate their descendants' content — used to
    locate the main-content root.
    """
    scores: Dict[Any, float] = defaultdict(float)
    own_scores: Dict[Any, float] = {}

    for block in body.iter():
        if not isinstance(block.tag, str):
            continue
        text = _own_text(block)
        wc = len(text.split())
        if wc < 3 and block.tag not in _HEADINGS:
            continue
        own_link = sum(len(_WS_RE.sub(" ", (a.text_content() or "")).strip())
                       for a in block.iter("a")
                       if a.getparent() is not None)
        ld = min(1.0, own_link / max(len(text), 1))
        base = 1.0 + min(text.count(",") + text.count("，"), 3) + min(wc / 25.0, 4.0)
        if block.tag in _HEADINGS:
            base += 1.0
        contribution = base * (1.0 - ld)
        own_scores[block] = own_scores.get(block, 0.0) + contribution
        scores[block] += contribution
        parent = block.getparent()
        depth = 0
        while parent is not None and isinstance(parent.tag, str) and depth < 5:
            if parent.tag in ("html", "body"):
                break
            divisor = 2.0 if depth == 0 else (3.0 if depth == 1 else (depth + 1) * 2.0)
            scores[parent] += contribution / divisor
            parent = parent.getparent()
            depth += 1
    return scores, own_scores


def _pick_and_climb(scores):
    best = None
    best_final = -1.0
    for el, raw in scores.items():
        final = raw * _container_multiplier(el) * (1.0 - min(_link_density(el), 0.9))
        if final > best_final:
            best_final, best = final, el
    if best is None:
        return None
    sib_thresh = 0.15 * scores.get(best, 1.0)
    cur = best
    while True:
        parent = cur.getparent()
        if parent is None or not isinstance(parent.tag, str) or parent.tag in ("html", "body"):
            break
        if _link_density(parent) >= 0.5:
            break
        elem_children = [c for c in parent if isinstance(c.tag, str)]
        content_children = [c for c in elem_children if scores.get(c, 0.0) >= sib_thresh]
        if len(content_children) >= 2:
            cur = parent
        elif len(elem_children) == 1:
            cur = parent
        else:
            break
    return cur


def extract_main_content(body, cfg: "PruneConfig | None" = None):
    """Return the single best main-content subtree (or ``body``)."""
    scores, _ = _score_blocks(body)
    if not scores:
        return body
    picked = _pick_and_climb(scores)
    return picked if picked is not None else body


def _coverage(root, own_scores: Dict[Any, float]) -> float:
    total = sum(own_scores.values())
    if total <= 0:
        return 1.0
    captured = sum(s for b, s in own_scores.items() if root is b or root in b.iterancestors())
    return captured / total


def collect_content_blocks(body, own_scores: Dict[Any, float], cfg: PruneConfig):
    """Build a clean tree from the page's leaf content blocks, in document order.

    For pages where content is scattered across sibling blocks separated by
    link-dense chrome (StackOverflow Q + N answers; Hacker News comment trees),
    subtree extraction can't capture it all. We select the *leaf* content blocks
    (high own-score, low link density, not big wrapper containers), drop any that
    nest inside another selected block, and reassemble clones in document order —
    so nav/footer/vote-widgets never come along for the ride.
    """
    import copy

    if not own_scores:
        return None
    order = {el: i for i, el in enumerate(body.iter())}
    mx = max(own_scores.values())
    thr = max(1.0, mx * 0.12)

    selected = []
    for b, s in own_scores.items():
        if b.tag not in _COLLECT_TAGS:        # never structural roots (body/table/tr/section)
            continue
        if s < thr or _link_density(b) >= 0.6:
            continue
        if not (b.text_content() or "").strip():
            continue
        total = len(_text(b))
        own = len(_own_text(b))
        # skip big wrapper containers — their content children get selected
        if total > 400 and total > 0 and own / total < 0.5:
            continue
        selected.append(b)
    if not selected:
        return None

    sel_set = set(selected)
    selected = [b for b in selected if not any(a in sel_set for a in b.iterancestors())]
    selected.sort(key=lambda b: order.get(b, 0))

    container = _lxml_html.Element("div")
    for b in selected:
        container.append(copy.deepcopy(b))
    return container


def _drop_link_clusters(el):
    """Remove pure navigation/menu blocks: ≥4 links, >85% link text, <30 words.

    Catches "new | past | comments | ask | show | jobs" nav bars and
    "Guidelines | FAQ | Lists | API" footers wherever they survive, while
    sparing single-link bylines and link-rich prose (which is >30 words or has
    substantial non-link text).
    """
    for node in list(el.iter()):
        if node is el or not isinstance(node.tag, str) or node.tag in ("a", "li"):
            continue
        links = node.findall(".//a")
        if len(links) < 4:
            continue
        txt = _text(node)
        wc = len(txt.split())
        # Comma-separated link lists (author bylines, "see X, Y, Z") are content,
        # not nav — menus use "|" / "·" separators. Keep the former.
        if txt.count(",") >= len(links) - 2:
            continue
        # Menus dilute link-density with " | " / "·" separators, so 0.6 (not
        # 0.85) catches them; real prose with 4 links in <25 words sits lower.
        if wc < 25 and _link_density(node) > 0.6:
            p = node.getparent()
            if p is not None:
                p.remove(node)
    return el


def locate_content(body, cfg: PruneConfig | None = None):
    """Locate + clean the main content, then strip any surviving nav clusters."""
    return _drop_link_clusters(_locate_impl(body, cfg))


def _locate_impl(body, cfg: PruneConfig | None = None):
    """Locate + clean the main content, returning the element to render.

    Strategy: locate the best subtree; if pruning it keeps most of its text use
    that (article pages). If the page is scattered (low coverage) or subtree
    pruning over-nukes (link-dense comment/Q&A layouts), fall back to leaf-block
    collection. A final guard guarantees we never return near-empty.
    """
    import copy

    cfg = cfg or PruneConfig()
    body_text_len = total_text_len(body)
    scores, own_scores = _score_blocks(body)
    if not scores:
        prune(body, cfg)
        return body

    picked = _pick_and_climb(scores)
    root = picked if picked is not None else body
    coverage = _coverage(root, own_scores)

    def subtree_fit():
        root_len = total_text_len(root)
        fit = copy.deepcopy(root)
        prune(fit, cfg)
        fit_len = total_text_len(fit)
        # over-pruned? (link-dense layout the scorer couldn't keep)
        if root_len > 200 and fit_len < max(0.2 * root_len, 150):
            return None
        return fit

    if coverage >= 0.6:
        fit = subtree_fit()
        if fit is not None:
            return fit

    collected = collect_content_blocks(body, own_scores, cfg)
    if collected is not None and total_text_len(collected) >= max(150, 0.05 * body_text_len):
        # Don't re-prune: blocks were already selected as leaf content; pruning
        # would re-nuke short comments and small answer snippets.
        return collected

    # last resort: relaxed subtree prune, then unpruned root, then whole body
    fit = copy.deepcopy(root)
    prune(fit, PruneConfig(threshold=0.30))
    if total_text_len(fit) >= 120:
        return fit
    return root if total_text_len(root) >= 120 else body


# ───────────────────────── phase 2: boilerplate pruning ───────────────────────

def composite_score(el, cfg: PruneConfig) -> float:
    text = _text(el)
    text_len = len(text)
    word_count = len(text.split())

    if word_count < cfg.min_word_count:
        # Don't force-remove short blocks that still carry a content signal:
        # a section heading, an image, or a table (table cell text concatenates
        # without spaces, so word_count badly undercounts real data tables).
        has_heading = any(True for _ in el.iter("h1", "h2", "h3", "h4", "h5", "h6"))
        has_table = el.tag == "table" or el.find(".//table") is not None
        has_img = el.tag == "img" or el.find(".//img") is not None
        if not has_table and not has_img and not has_heading:
            return -1.0

    try:
        html_len = len(etree.tostring(el, encoding="unicode"))
    except Exception:  # noqa: BLE001
        html_len = max(text_len, 1)
    text_density = text_len / max(html_len, 1)
    link_density = _link_density(el)
    tag_prior = _TAG_WEIGHTS.get(el.tag, 0.5) / _MAX_TAG_WEIGHT
    class_id = _class_id_prior(el)
    text_len_prior = min(1.0, math.log(text_len + 1) / math.log(cfg.text_length_saturation + 1))

    w = cfg.weights
    return (
        w["text_density"] * min(1.0, text_density)
        + w["link_density"] * (1.0 - link_density)
        + w["tag_prior"] * tag_prior
        + w["class_id_prior"] * class_id
        + w["text_length_prior"] * text_len_prior
    )


def _threshold_for(tag: str, cfg: PruneConfig) -> float:
    if not cfg.dynamic:
        return cfg.threshold
    base = cfg.threshold
    if tag in ("article", "main"):
        return base * 0.5
    if tag in ("section", "div", "details"):
        return base * 0.9
    if tag in ("nav", "footer", "form", "aside", "header"):
        return base * 1.25
    return base


def _is_empty(el) -> bool:
    if el.tag in ("img", "br", "hr", "td", "th"):
        return False
    if _text(el):
        return False
    if el.find(".//img") is not None or el.find(".//table") is not None:
        return False
    return True


def prune(root, cfg: PruneConfig | None = None):
    """Remove residual boilerplate blocks from ``root`` in place; returns it."""
    cfg = cfg or PruneConfig()

    def visit(el) -> None:
        for child in list(el):
            if not isinstance(child.tag, str):
                el.remove(child)
                continue
            tag = child.tag
            if tag in _BLOCK_CANDIDATES:
                # Semantic data tables (with <th>/<caption>) are content — keep
                # them intact even though linked cells make them look link-dense.
                if tag == "table" and (
                    child.find(".//th") is not None or child.find(".//caption") is not None
                ):
                    continue
                # Protect content-heavy wrappers regardless of aggregate score.
                txt = _text(child)
                if len(txt.split()) >= cfg.protect_word_count and _link_density(child) < cfg.protect_link_density:
                    visit(child)
                    continue
                score = composite_score(child, cfg)
                if score < _threshold_for(tag, cfg):
                    # A block whose content is a real (non-boilerplate) section
                    # heading is a signpost — keep it and clean inside rather
                    # than amputate. "See also"/"Related"/etc. are NOT protected.
                    if _has_content_heading(child):
                        visit(child)
                        continue
                    el.remove(child)
                    continue
                visit(child)
            elif tag in _LEAF_KEEP:
                continue
            elif len(child):
                visit(child)

    visit(root)

    for el in list(root.iter()):
        if el is root or not isinstance(el.tag, str):
            continue
        if _is_empty(el):
            p = el.getparent()
            if p is not None:
                p.remove(el)
    return root


def total_text_len(el) -> int:
    return len(_text(el))


# Strict chrome class/id patterns safe to remove pre-emptively (link-dense).
_STRICT_CHROME_RE = re.compile(
    r"(?:^|[-_ ])(?:site-header|site-footer|global-nav|masthead|breadcrumb|"
    r"cookie|consent|gdpr|newsletter|skip-link|navbar|topnav|main-nav)",
    re.IGNORECASE,
)


def strip_obvious_chrome(body):
    """Remove unambiguous site chrome before content detection.

    Conservative on purpose — only ``<nav>``/``<footer>`` tags, ARIA
    navigation/banner/contentinfo/search landmarks, and link-dense blocks with a
    strict chrome class/id. ``<header>``/``<aside>`` are left for the scorer
    (they sometimes carry the headline or a pull-quote).
    """
    for node in list(body.iter()):
        if node is body or not isinstance(node.tag, str):
            continue
        tag = node.tag
        role = (node.get("role") or "").lower()
        remove = False
        if tag in ("nav", "footer"):
            remove = True
        elif role in ("navigation", "banner", "contentinfo", "search"):
            remove = True
        elif _STRICT_CHROME_RE.search(_class_blob(node)) and _link_density(node) > 0.3:
            remove = True
        if remove:
            p = node.getparent()
            if p is not None:
                p.remove(node)
    return body
