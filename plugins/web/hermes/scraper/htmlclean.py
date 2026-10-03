"""HTML parsing, sanitisation and metadata extraction.

Stage 1 of the pipeline. Takes raw HTML bytes/str → a cleaned lxml tree plus a
:class:`PageMeta` record. Responsibilities (drawn from ScrapeGraphAI's
``cleanup_html`` and crawl4ai's scraping stage):

  * Parse defensively (lxml's ``html`` parser tolerates real-world breakage).
  * Resolve ``<base href>`` and rewrite all links/images to absolute URLs.
  * HARD-remove non-content nodes (script/style/svg/forms/comments/hidden) but
    PRESERVE ``application/ld+json`` payloads first — they often hold the
    cleanest structured data on the page.
  * Extract page metadata (title, description, lang, canonical, author, date,
    OpenGraph) and the parsed JSON-LD blocks.

Structural *chrome* (nav/header/footer/aside) is deliberately NOT removed here —
that judgement is left to the scoring pruner in :mod:`.pruning`, which can keep a
content-bearing ``<aside>`` while dropping a navigational one.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

from lxml import etree, html

# Tags that never carry readable content. Removed outright (after JSON-LD is
# harvested). ``noscript`` is dropped because its body is fallback markup that
# duplicates/obscures the real DOM.
_HARD_REMOVE_TAGS = (
    "script", "style", "noscript", "template", "svg", "canvas", "math",
    "object", "embed", "applet", "iframe", "frame", "frameset",
    "form", "input", "button", "select", "option", "textarea", "label",
    "fieldset", "legend", "datalist", "output", "progress", "meter",
    "audio", "video", "track", "source", "map", "area", "param",
    "link", "meta", "base", "title", "head",
    "dialog", "menu", "menuitem",
)

# Attributes stripped from every surviving element to shrink the tree and remove
# tracking noise. ``href``/``src``/``alt``/``title``/``datetime`` are kept; we
# also keep ``class``/``id`` until pruning has run (the pruner reads them).
_KEEP_ATTRS = {"href", "src", "alt", "title", "datetime", "class", "id", "role", "lang", "colspan", "rowspan"}


@dataclass
class PageMeta:
    """Lightweight metadata harvested from the document head / OpenGraph."""

    title: str = ""
    description: str = ""
    language: str = ""
    canonical_url: str = ""
    author: str = ""
    published: str = ""
    site_name: str = ""
    favicon: str = ""
    json_ld: List[Any] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "title": self.title,
            "description": self.description,
            "language": self.language,
            "canonicalUrl": self.canonical_url,
            "author": self.author,
            "published": self.published,
            "siteName": self.site_name,
            "favicon": self.favicon,
            "jsonLd": self.json_ld,
        }


def _meta_content(root, *, name: str = "", prop: str = "") -> str:
    if name:
        el = root.find(f'.//meta[@name="{name}"]')
        if el is not None and el.get("content"):
            return el.get("content").strip()
    if prop:
        el = root.find(f'.//meta[@property="{prop}"]')
        if el is not None and el.get("content"):
            return el.get("content").strip()
    return ""


def _harvest_json_ld(root) -> List[Any]:
    blocks: List[Any] = []
    for node in root.xpath('//script[@type="application/ld+json"]'):
        raw = (node.text_content() or "").strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            # Some sites concatenate multiple JSON objects or include trailing
            # commas; try a couple of cheap repairs before giving up.
            try:
                data = json.loads(raw.replace("\n", " "))
            except Exception:  # noqa: BLE001
                continue
        blocks.append(data)
    return blocks


def _extract_meta(root, base_url: str) -> PageMeta:
    meta = PageMeta()
    meta.json_ld = _harvest_json_ld(root)

    # Title: <title> → og:title → first <h1>
    title_el = root.find(".//title")
    if title_el is not None and title_el.text:
        meta.title = title_el.text.strip()
    if not meta.title:
        meta.title = _meta_content(root, prop="og:title")
    if not meta.title:
        h1 = root.find(".//h1")
        if h1 is not None:
            meta.title = (h1.text_content() or "").strip()

    meta.description = (
        _meta_content(root, name="description")
        or _meta_content(root, prop="og:description")
    )
    meta.site_name = _meta_content(root, prop="og:site_name")
    meta.author = (
        _meta_content(root, name="author")
        or _meta_content(root, prop="article:author")
    )
    meta.published = (
        _meta_content(root, prop="article:published_time")
        or _meta_content(root, name="date")
        or _meta_content(root, name="pubdate")
    )
    if not meta.published:
        t = root.find(".//time[@datetime]")
        if t is not None:
            meta.published = (t.get("datetime") or "").strip()

    html_el = root if root.tag == "html" else root.find(".//html")
    if html_el is not None and html_el.get("lang"):
        meta.language = html_el.get("lang").strip()

    canon = root.find('.//link[@rel="canonical"]')
    if canon is not None and canon.get("href"):
        meta.canonical_url = urljoin(base_url, canon.get("href"))

    icon = root.find('.//link[@rel="icon"]')
    if icon is None:
        icon = root.find('.//link[@rel="shortcut icon"]')
    if icon is not None and icon.get("href"):
        meta.favicon = urljoin(base_url, icon.get("href"))

    # JSON-LD often has author/date when meta tags don't.
    for block in meta.json_ld:
        objs = block if isinstance(block, list) else [block]
        for obj in objs:
            if not isinstance(obj, dict):
                continue
            if not meta.author and obj.get("author"):
                a = obj["author"]
                if isinstance(a, dict):
                    meta.author = str(a.get("name", "")).strip()
                elif isinstance(a, list) and a and isinstance(a[0], dict):
                    meta.author = str(a[0].get("name", "")).strip()
                elif isinstance(a, str):
                    meta.author = a.strip()
            if not meta.published and obj.get("datePublished"):
                meta.published = str(obj["datePublished"]).strip()
    return meta


def _is_hidden(el) -> bool:
    if el.get("hidden") is not None:
        return True
    if (el.get("aria-hidden") or "").lower() == "true":
        return True
    style = (el.get("style") or "").lower().replace(" ", "")
    if "display:none" in style or "visibility:hidden" in style:
        return True
    return False


def _absolutize(root, base_url: str) -> None:
    base_el = root.find(".//base[@href]")
    effective_base = urljoin(base_url, base_el.get("href")) if base_el is not None else base_url
    try:
        root.make_links_absolute(effective_base, resolve_base_href=True)
    except (ValueError, TypeError):
        # make_links_absolute can choke on malformed/javascript: hrefs — fall
        # back to a manual pass over the common attributes.
        for el in root.iter():
            for attr in ("href", "src"):
                v = el.get(attr)
                if v and not v.startswith(("javascript:", "data:", "mailto:", "#")):
                    try:
                        el.set(attr, urljoin(effective_base, v))
                    except ValueError:
                        pass


def parse_and_clean(raw_html: str, base_url: str) -> "CleanedDoc":
    """Parse ``raw_html`` and return a :class:`CleanedDoc` (tree + metadata).

    The returned tree has scripts/styles/forms/hidden nodes removed and links
    made absolute, but retains structural chrome for the pruner.
    """
    if not raw_html or not raw_html.strip():
        return CleanedDoc(body=html.Element("body"), meta=PageMeta(), base_url=base_url)

    root = html.fromstring(raw_html)

    # 1. Metadata + JSON-LD BEFORE we strip <head>/<script>.
    meta = _extract_meta(root, base_url)

    # 2. Absolute links (uses <base href> then strips it via resolve_base_href).
    _absolutize(root, base_url)

    # 3. Drop HTML comments and hidden nodes.
    for comment in root.xpath("//comment()"):
        parent = comment.getparent()
        if parent is not None:
            parent.remove(comment)
    for el in list(root.iter()):
        if isinstance(el.tag, str) and _is_hidden(el):
            p = el.getparent()
            if p is not None:
                p.remove(el)

    # 4. HARD-remove non-content tags (scripts/styles/forms/embeds/head). We do
    #    this explicitly rather than via lxml's Cleaner (which moved to a
    #    separate ``lxml_html_clean`` package in lxml 5.x) — one fewer dep and
    #    full control. ``strip_elements`` drops the elements AND their tails'
    #    owning subtree cleanly.
    try:
        etree.strip_elements(root, *[t for t in _HARD_REMOVE_TAGS], with_tail=False)
    except Exception:  # noqa: BLE001 — never let cleaning crash extraction
        for tag in _HARD_REMOVE_TAGS:
            for el in root.xpath(f"//{tag}"):
                p = el.getparent()
                if p is not None:
                    p.remove(el)
    # Processing instructions / leftover PIs.
    for pi in root.xpath("//processing-instruction()"):
        p = pi.getparent()
        if p is not None:
            p.remove(pi)

    # 5. Strip noise attributes (keep the few the pruner / markdown need).
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        for attr in list(el.attrib):
            if attr not in _KEEP_ATTRS:
                del el.attrib[attr]

    body = root.find(".//body")
    if body is None:
        body = root if root.tag == "body" else root
    return CleanedDoc(body=body, meta=meta, base_url=base_url)


@dataclass
class CleanedDoc:
    body: Any  # lxml element (the <body> subtree)
    meta: PageMeta
    base_url: str
