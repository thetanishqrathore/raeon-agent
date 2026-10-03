"""Markdown generation from a cleaned/pruned lxml tree.

A focused HTML→Markdown serialiser (no html2text dependency) so we control
exactly how links, code, tables and citations render. Produces:

  * ``to_markdown(body)`` — clean GitHub-flavoured markdown with inline links.
  * ``convert_links_to_citations(md)`` — crawl4ai-style transform that replaces
    inline URLs with numbered ``⟨n⟩`` markers and appends a References block,
    keeping prose readable and token-cheap for an LLM.
"""

from __future__ import annotations

import re
from typing import List, Tuple

_BLOCK_TAGS = {
    "p", "div", "section", "article", "main", "aside", "header", "footer",
    "nav", "ul", "ol", "li", "dl", "dt", "dd", "table", "thead", "tbody",
    "tfoot", "tr", "blockquote", "pre", "figure", "figcaption", "hr",
    "h1", "h2", "h3", "h4", "h5", "h6", "details", "summary", "address",
}

_WS = re.compile(r"[ \t]{2,}")
_NL = re.compile(r"\n{3,}")
_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")


def _clean_inline(s: str) -> str:
    if not s:
        return ""
    s = s.replace("\xa0", " ").replace("​", "")
    s = re.sub(r"[ \t]*\n[ \t]*", "\n", s)
    s = _WS.sub(" ", s)
    s = _NL.sub("\n\n", s)
    return s.strip()


def _inline_content(el) -> str:
    parts: List[str] = [el.text or ""]
    for child in el:
        if isinstance(child.tag, str):
            parts.append(_inline_node(child))
        parts.append(child.tail or "")
    return "".join(parts)


def _inline_node(el) -> str:
    tag = el.tag
    if tag in ("strong", "b"):
        inner = _inline_content(el).strip()
        return f"**{inner}**" if inner else ""
    if tag in ("em", "i"):
        inner = _inline_content(el).strip()
        return f"*{inner}*" if inner else ""
    if tag in ("code", "kbd", "samp", "var", "tt"):
        inner = (el.text_content() or "").strip()
        return f"`{inner}`" if inner else ""
    if tag == "a":
        text = _clean_inline(_inline_content(el)).replace("\n", " ").strip()
        href = (el.get("href") or "").strip()
        if href and text and href.startswith(("http://", "https://")):
            return f"[{text}]({href})"
        return text
    if tag == "img":
        src = (el.get("src") or "").strip()
        alt = (el.get("alt") or "").strip()
        if src and src.startswith(("http://", "https://")):
            return f"![{alt}]({src})"
        return ""
    if tag == "br":
        return "\n"
    if tag == "sup":
        return f"^{_inline_content(el).strip()}"
    if tag == "sub":
        return f"~{_inline_content(el).strip()}"
    if tag in ("del", "s", "strike"):
        return f"~~{_inline_content(el).strip()}~~"
    # span / mark / small / abbr / time / cite / q / u / etc → pass through.
    return _inline_content(el)


def _detect_lang(pre_el) -> str:
    code_el = pre_el.find(".//code")
    cls = (code_el.get("class") if code_el is not None else pre_el.get("class")) or ""
    m = re.search(r"(?:language|lang|highlight)-([\w+#.-]+)", cls)
    return m.group(1) if m else ""


def _render_table(el) -> List[str]:
    rows: List[List[str]] = []
    for tr in el.iter("tr"):
        cells: List[str] = []
        for cell in tr:
            if isinstance(cell.tag, str) and cell.tag in ("td", "th"):
                txt = _clean_inline(_inline_content(cell)).replace("\n", " ").replace("|", "\\|")
                cells.append(txt)
        if cells:
            rows.append(cells)
    if not rows:
        return []
    ncol = max(len(r) for r in rows)
    rows = [r + [""] * (ncol - len(r)) for r in rows]
    header = rows[0]
    out = ["| " + " | ".join(header) + " |", "| " + " | ".join(["---"] * ncol) + " |"]
    for r in rows[1:]:
        out.append("| " + " | ".join(r) + " |")
    return ["\n".join(out)]


def _render_list(el, ordered: bool, depth: int = 0) -> List[str]:
    lines: List[str] = []
    idx = 1
    for li in el:
        if not isinstance(li.tag, str) or li.tag != "li":
            continue
        nested: List = []
        inline_parts: List[str] = [li.text or ""]
        for c in li:
            if isinstance(c.tag, str) and c.tag in ("ul", "ol"):
                nested.append(c)
            elif isinstance(c.tag, str):
                if c.tag in _BLOCK_TAGS:
                    inline_parts.append(" " + _clean_inline(_inline_content(c)))
                else:
                    inline_parts.append(_inline_node(c))
            if c.tail:
                inline_parts.append(c.tail)
        text = _clean_inline("".join(inline_parts)).replace("\n", " ").strip()
        marker = f"{idx}." if ordered else "-"
        indent = "  " * depth
        if text:
            lines.append(f"{indent}{marker} {text}")
        for n in nested:
            lines.extend(_render_list(n, n.tag == "ol", depth + 1))
        idx += 1
    return ["\n".join(lines)] if lines else []


def _render_block_el(el) -> List[str]:
    tag = el.tag
    if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
        level = int(tag[1])
        text = _clean_inline(_inline_content(el)).replace("\n", " ").strip()
        return [f"{'#' * level} {text}"] if text else []
    if tag == "p":
        return [_clean_inline(_inline_content(el))] or []
    if tag == "hr":
        return ["---"]
    if tag == "pre":
        code = (el.text_content() or "").rstrip("\n")
        if not code.strip():
            return []
        return [f"```{_detect_lang(el)}\n{code}\n```"]
    if tag == "blockquote":
        inner = _render_blocks(el)
        quoted = []
        for blk in inner:
            quoted.append("\n".join("> " + ln for ln in blk.split("\n")))
        return ["\n>\n".join(quoted)] if quoted else []
    if tag == "ul":
        return _render_list(el, ordered=False)
    if tag == "ol":
        return _render_list(el, ordered=True)
    if tag == "table":
        return _render_table(el)
    if tag == "img":
        node = _inline_node(el)
        return [node] if node else []
    if tag in ("figure", "figcaption", "details", "summary", "dl", "dt", "dd", "address"):
        return _render_blocks(el)
    # generic container
    return _render_blocks(el)


def _render_blocks(el) -> List[str]:
    out: List[str] = []
    inline_buf: List[str] = []

    def flush() -> None:
        t = _clean_inline("".join(inline_buf))
        inline_buf.clear()
        if t:
            out.append(t)

    if el.text:
        inline_buf.append(el.text)
    for child in el:
        if not isinstance(child.tag, str):
            if child.tail:
                inline_buf.append(child.tail)
            continue
        if child.tag in _BLOCK_TAGS:
            flush()
            out.extend(_render_block_el(child))
        else:
            inline_buf.append(_inline_node(child))
        if child.tail:
            inline_buf.append(child.tail)
    flush()
    return out


def to_markdown(body) -> str:
    """Serialise a cleaned/pruned ``<body>`` element to markdown."""
    if body is None:
        return ""
    blocks = _render_blocks(body)
    md = "\n\n".join(b for b in blocks if b and b.strip())
    md = _NL.sub("\n\n", md)
    # Tidy stray spaces before newlines.
    md = re.sub(r"[ \t]+\n", "\n", md)
    return md.strip()


def convert_links_to_citations(markdown: str) -> Tuple[str, str]:
    """Replace inline ``[text](url)`` with ``text ⟨n⟩`` + a References block.

    Returns ``(body_with_citations, references_markdown)``.
    """
    url_to_num: dict[str, int] = {}
    order: List[str] = []

    def repl(m: re.Match) -> str:
        text, url = m.group(1), m.group(2)
        if url not in url_to_num:
            url_to_num[url] = len(order) + 1
            order.append(url)
        return f"{text} ⟨{url_to_num[url]}⟩"

    body = _LINK_RE.sub(repl, markdown)
    if not order:
        return markdown, ""
    refs = "\n".join(f"⟨{i + 1}⟩ {url}" for i, url in enumerate(order))
    return body, refs
