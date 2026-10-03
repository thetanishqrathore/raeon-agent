"""Pipeline orchestrator: URL/HTML → ScrapeResult.

Ties the stages together:

    fetch (tiered)  →  parse+clean  →  locate main content  →  prune boilerplate
                    →  markdown (raw + fit, optional citations)
                    →  [optional] LLM answer / structured extraction

The deterministic path (everything up to "fit markdown") never calls a model.
A safety net guarantees we never hand back near-empty content when the page
actually had text — pruning failures fall back to relaxed pruning, then to the
unpruned cleaned tree.
"""

from __future__ import annotations

import copy
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from plugins.web.hermes.scraper import mdgen
from plugins.web.hermes.scraper.htmlclean import parse_and_clean
from plugins.web.hermes.scraper.pruning import (
    PruneConfig,
    locate_content,
    strip_obvious_chrome,
)


@dataclass
class ScrapeOptions:
    format: str = "markdown"            # markdown | html | text
    citations: bool = False
    query: Optional[str] = None         # → LLM map-reduce answer
    schema: Optional[Dict[str, Any]] = None  # → LLM structured JSON
    instruction: str = ""               # natural-language hint for schema extraction
    use_llm: bool = True                # allow the LLM layer when query/schema set
    include_raw: bool = True            # populate raw_markdown
    fetch_timeout: float = 30.0
    allow_browser: bool = True
    prune_config: PruneConfig = field(default_factory=PruneConfig)


@dataclass
class ScrapeResult:
    url: str
    final_url: str = ""
    status_code: int = 0
    title: str = ""
    markdown: str = ""                  # fit markdown
    raw_markdown: str = ""
    references: str = ""
    text: str = ""
    answer: str = ""                    # query result
    structured: Optional[Dict[str, Any]] = None  # schema result
    metadata: Dict[str, Any] = field(default_factory=dict)
    links: List[str] = field(default_factory=list)
    images: List[str] = field(default_factory=list)
    word_count: int = 0
    fetched_via: str = "http"
    elapsed_ms: int = 0
    needs_browser: bool = False
    error: str = ""

    def to_provider_dict(self) -> Dict[str, Any]:
        """Shape expected by ``WebSearchProvider.extract`` consumers."""
        if self.error:
            return {"url": self.url, "title": "", "content": "", "error": self.error}
        if self.structured is not None:
            import json

            content = json.dumps(self.structured.get("data", self.structured), ensure_ascii=False, indent=2)
        elif self.answer:
            content = self.answer
        else:
            content = self.markdown
        meta = dict(self.metadata)
        meta.update({
            "sourceURL": self.final_url or self.url,
            "statusCode": self.status_code,
            "fetchedVia": self.fetched_via,
            "wordCount": self.word_count,
            "title": self.title,
        })
        if self.references:
            meta["references"] = self.references
        if self.answer or self.structured is not None:
            meta["fitMarkdown"] = self.markdown[:20000]
        return {
            "url": self.final_url or self.url,
            "title": self.title,
            "content": content,
            "raw_content": self.markdown,
            "metadata": meta,
        }


_WS = re.compile(r"\s+")


def _collect_links_images(root) -> tuple[List[str], List[str]]:
    links, images = [], []
    seen_l, seen_i = set(), set()
    for a in root.iter("a"):
        href = (a.get("href") or "").strip()
        if href.startswith(("http://", "https://")) and href not in seen_l:
            seen_l.add(href)
            links.append(href)
    for img in root.iter("img"):
        src = (img.get("src") or "").strip()
        if src.startswith(("http://", "https://")) and src not in seen_i:
            seen_i.add(src)
            images.append(src)
    return links[:200], images[:100]


def _headings(root) -> List[str]:
    out = []
    for h in root.iter("h1", "h2", "h3"):
        t = _WS.sub(" ", (h.text_content() or "")).strip()
        if t:
            out.append(t)
    return out[:12]


def scrape_html(raw_html: str, url: str, opts: Optional[ScrapeOptions] = None) -> ScrapeResult:
    """Deterministic HTML → markdown (no network, no LLM). The testable core."""
    opts = opts or ScrapeOptions()
    t0 = time.time()
    doc = parse_and_clean(raw_html, url)
    body = doc.body
    # raw_markdown reflects the truly-raw cleaned page (before chrome stripping).
    raw_md = mdgen.to_markdown(body) if opts.include_raw else ""
    # Remove unambiguous chrome, then locate + prune content (subtree extraction
    # for articles, block collection for scattered Q&A/comment pages).
    strip_obvious_chrome(body)
    fit_root = locate_content(body, opts.prune_config)
    fit_md = mdgen.to_markdown(fit_root)
    references = ""
    if opts.citations:
        fit_md, references = mdgen.convert_links_to_citations(fit_md)

    links, images = _collect_links_images(fit_root)
    meta = doc.meta
    result = ScrapeResult(
        url=url,
        final_url=url,
        title=meta.title,
        markdown=fit_md,
        raw_markdown=raw_md,
        references=references,
        text=_WS.sub(" ", total_text_str(fit_root)).strip() if opts.format == "text" else "",
        metadata={k: v for k, v in meta.as_dict().items() if v},
        links=links,
        images=images,
        word_count=len(mdgen._clean_inline(fit_md).split()),
        elapsed_ms=int((time.time() - t0) * 1000),
    )
    result.metadata["headings"] = _headings(fit_root)
    return result


def total_text_str(el) -> str:
    return el.text_content() or ""


async def scrape(url: str, opts: Optional[ScrapeOptions] = None) -> ScrapeResult:
    """Full async scrape: fetch → deterministic core → optional LLM extraction."""
    opts = opts or ScrapeOptions()
    from plugins.web.hermes.scraper import fetch as _fetch

    fr = await _fetch.fetch(
        url, timeout=opts.fetch_timeout, allow_browser=opts.allow_browser,
    )
    if fr.is_pdf:
        return _scrape_pdf(fr, url, opts)
    if not fr.ok:
        return ScrapeResult(
            url=url, final_url=fr.final_url or url, status_code=fr.status_code,
            fetched_via=fr.fetched_via, needs_browser=fr.needs_browser,
            error=fr.error or (
                "Page returned no usable HTML (bot wall or JS-only). "
                "Try browser_navigate." if fr.needs_browser else "empty response"
            ),
        )

    res = scrape_html(fr.html, fr.final_url or url, opts)
    res.final_url = fr.final_url or url
    res.status_code = fr.status_code
    res.fetched_via = fr.fetched_via
    res.needs_browser = fr.needs_browser

    # ── optional LLM layer ──
    if opts.use_llm and (opts.query or opts.schema) and res.markdown:
        try:
            from plugins.web.hermes.scraper import llm_extract

            if not llm_extract.aux_available():
                res.metadata["llmNote"] = "no auxiliary model configured; returned deterministic markdown"
            elif opts.schema:
                res.structured = await llm_extract.extract_structured(
                    res.markdown, opts.schema, instruction=opts.instruction, title=res.title,
                )
            elif opts.query:
                res.answer = await llm_extract.extract_answer(
                    res.markdown, opts.query, title=res.title,
                )
        except Exception as exc:  # noqa: BLE001 — degrade to markdown
            res.metadata["llmError"] = str(exc)[:200]
    res.elapsed_ms = res.elapsed_ms
    return res


def _scrape_pdf(fr, url: str, opts: ScrapeOptions) -> ScrapeResult:
    """Best-effort PDF → text using pypdf if importable; else a clear note."""
    try:
        import io

        from pypdf import PdfReader  # type: ignore  # noqa: WPS433

        reader = PdfReader(io.BytesIO(fr.raw_bytes))
        pages = [(p.extract_text() or "") for p in reader.pages]
        text = "\n\n".join(t.strip() for t in pages if t.strip())
        md = re.sub(r"\n{3,}", "\n\n", text).strip()
        return ScrapeResult(
            url=url, final_url=fr.final_url or url, status_code=fr.status_code,
            title=(md.splitlines()[0][:120] if md else "PDF document"),
            markdown=md, raw_markdown=md, word_count=len(md.split()),
            fetched_via=fr.fetched_via, metadata={"contentType": "application/pdf"},
        )
    except ImportError:
        return ScrapeResult(
            url=url, final_url=fr.final_url or url, status_code=fr.status_code,
            error="PDF detected. Install pypdf (lazy dep) to extract, or use another backend.",
            metadata={"contentType": "application/pdf"}, fetched_via=fr.fetched_via,
        )
    except Exception as exc:  # noqa: BLE001
        return ScrapeResult(url=url, error=f"PDF parse failed: {exc}")
