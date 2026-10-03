"""Unit tests for the Hermes native scraper (network-free, synthetic HTML).

Covers the deterministic core (clean → locate content → prune → markdown),
the never-empty safety net, chunking, BM25 ranking, and provider wiring.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

pytest.importorskip("lxml")

from plugins.web.hermes.scraper import bm25, chunking, mdgen  # noqa: E402
from plugins.web.hermes.scraper.htmlclean import parse_and_clean  # noqa: E402
from plugins.web.hermes.scraper.pipeline import ScrapeOptions, scrape_html  # noqa: E402
from plugins.web.hermes.scraper.pruning import (  # noqa: E402
    PruneConfig,
    extract_main_content,
    prune,
    strip_obvious_chrome,
)

ARTICLE_HTML = """
<!doctype html><html lang="en"><head>
  <title>Test Article — My Site</title>
  <meta name="description" content="A test article.">
  <meta property="article:published_time" content="2026-01-02">
  <script type="application/ld+json">{"@type":"Article","author":{"name":"Jane Doe"}}</script>
  <script>var x = 1;</script><style>.a{color:red}</style>
</head><body>
  <header class="site-header"><nav><a href="/">Home</a> <a href="/about">About</a></nav></header>
  <div id="content" role="main">
    <article class="post">
      <h1>The Main Headline</h1>
      <p>This is the first <strong>important</strong> paragraph with a
         <a href="https://example.com/x">link</a> in it.</p>
      <h2>A Section</h2>
      <ul><li>First item</li><li>Second item with <em>emphasis</em></li></ul>
      <pre><code class="language-python">print("hello")</code></pre>
      <table><tr><th>Key</th><th>Value</th></tr><tr><td>UPC</td><td>abc123</td></tr></table>
      <blockquote>A wise quote.</blockquote>
    </article>
    <aside class="related"><h3>Related</h3><ul>
      <li><a href="/a">Spammy link one</a></li><li><a href="/b">Spammy link two</a></li>
      <li><a href="/c">Spammy link three</a></li></ul></aside>
  </div>
  <footer class="site-footer"><p>Copyright 2026. Privacy Policy. Terms of Service.</p></footer>
</body></html>
"""


@pytest.fixture
def doc():
    return parse_and_clean(ARTICLE_HTML, "https://my.site/post")


def test_metadata_and_jsonld(doc):
    assert doc.meta.title == "Test Article — My Site"
    assert doc.meta.description == "A test article."
    assert doc.meta.language == "en"
    assert doc.meta.published == "2026-01-02"
    assert doc.meta.author == "Jane Doe"          # harvested from JSON-LD
    assert doc.meta.json_ld and doc.meta.json_ld[0]["@type"] == "Article"


def test_scripts_and_styles_removed(doc):
    text = doc.body.text_content()
    assert "var x = 1" not in text
    assert "color:red" not in text


def test_main_content_located_and_pruned(doc):
    strip_obvious_chrome(doc.body)
    root = extract_main_content(doc.body)
    prune(root)
    md = mdgen.to_markdown(root)
    # kept: headline, body, list, code, table, blockquote
    assert "# The Main Headline" in md
    assert "important" in md and "link" in md
    assert "First item" in md and "Second item" in md
    assert "print(\"hello\")" in md
    assert "| Key | Value |" in md and "abc123" in md
    assert "> A wise quote." in md
    # removed: nav, footer, related-links aside
    assert "Privacy Policy" not in md
    assert "Spammy link" not in md
    assert "About" not in md


def test_links_become_absolute(doc):
    # relative <a href="/about"> should have been absolutized
    hrefs = [a.get("href") for a in doc.body.iter("a")]
    assert any(h and h.startswith("https://my.site/") for h in hrefs)


def test_code_language_fence(doc):
    strip_obvious_chrome(doc.body)
    md = mdgen.to_markdown(extract_main_content(doc.body))
    assert "```python" in md


def test_citations():
    md = "See [the docs](https://example.com/docs) and [more](https://example.com/more)."
    body, refs = mdgen.convert_links_to_citations(md)
    assert "⟨1⟩" in body and "⟨2⟩" in body
    assert "https://example.com/docs" in refs
    assert "[the docs]" not in body  # inline link replaced by marker


def test_scrape_html_result():
    res = scrape_html(ARTICLE_HTML, "https://my.site/post", ScrapeOptions())
    assert res.title == "Test Article — My Site"
    assert "# The Main Headline" in res.markdown
    assert res.word_count > 5
    assert "https://example.com/x" in res.links


def test_safety_net_never_empty():
    # A page that is ALL navigation/links — pruning would normally nuke it.
    nav_only = "<html><body>" + "".join(
        f'<div class="menu"><a href="/{i}">link {i}</a></div>' for i in range(40)
    ) + "<div><p>" + "real sentence here. " * 30 + "</p></div></body></html>"
    res = scrape_html(nav_only, "https://x.y/", ScrapeOptions())
    assert "real sentence here" in res.markdown  # content survives the prune


def test_empty_html_is_safe():
    res = scrape_html("", "https://x.y/", ScrapeOptions())
    assert res.error == "" and res.markdown == ""


def test_chunking_respects_budget():
    text = "\n\n".join(f"Paragraph number {i} with some words." for i in range(200))
    chunks = chunking.chunk_text(text, max_tokens=100, overlap_tokens=10)
    assert len(chunks) > 1
    assert all(chunking.estimate_tokens(c) <= 130 for c in chunks)  # ~budget + slack


def test_bm25_ranks_relevant_first():
    blocks = [
        "The cat sat on the mat in the sunshine.",
        "Quarterly revenue grew twelve percent year over year.",
        "Photosynthesis converts light into chemical energy.",
    ]
    kept = bm25.rank_blocks(blocks, "company revenue growth earnings", top_k=1)
    assert kept == ["Quarterly revenue grew twelve percent year over year."]


def test_provider_registration():
    from plugins.web.hermes.provider import HermesNativeWebSearchProvider

    p = HermesNativeWebSearchProvider()
    assert p.name == "hermes"
    assert p.is_available() is True
    assert p.supports_extract() is True
    assert p.supports_search() is False
