# Crawl4AI extract backend

Self-hosted, browser-rendered `web_extract` backend. Pages are fetched in
headless Chromium **inside your own Crawl4AI service** (no third party, no
API cost) and returned as clean fit-markdown. Pairs with SearXNG (search)
for a fully free, fully self-hosted web stack.

## Setup

1. Run the service — see `docker/crawl4ai/README.md` (container
   `hermes-crawl4ai`, image `unclecode/crawl4ai:0.9.0`, bound to
   `127.0.0.1:11235`).
2. Point Hermes at it (`~/.hermes/.env`):

   ```
   CRAWL4AI_URL=http://127.0.0.1:11235
   CRAWL4AI_API_TOKEN=<your token>   # required by crawl4ai >= 0.9.0
   CRAWL4AI_TIMEOUT=60               # optional, seconds per URL
   ```

3. Select it (`~/.hermes/config.yaml`):

   ```yaml
   web:
     extract_backend: crawl4ai
     extract_fallback: true   # default: retry engine failures via the
                              # native hermes scraper automatically
   ```

## Behavior

* **Markdown**: fit-markdown (Pruning content filter, nav/footer/aside
  stripped); falls back to raw markdown when the filter over-prunes.
  `format: "html"` returns cleaned HTML.
* **query param**: switches the content filter to BM25 so the markdown is
  biased toward passages relevant to the query. `schema` degrades
  gracefully — structured extraction runs at the tool layer over the
  returned markdown, and direct callers get an `extraction_note`.
* **Reliability**: one `POST /crawl` per URL (isolated failures),
  concurrency-capped, typed errors (`Crawl4AIAuthError`,
  `Crawl4AITimeoutError`, `Crawl4AIUnavailableError`, `Crawl4AICrawlError`).
  On any engine failure the native hermes scraper transparently takes over
  for the affected URLs (unless `web.extract_fallback: false`); every result
  carries `metadata.engine` and fallback results carry an `extraction_note`
  naming the engine that produced the content.
* **Safety**: the same SSRF/private-IP guard (`tools/url_safety.py`) and
  website-policy gate as the other extract backends run **before** any URL
  is sent to the service — the crawl executes server-side, so pre-flight
  blocking is the only thing standing between a malicious prompt and your
  loopback services. Post-redirect URLs are re-checked. Policy/SSRF blocks
  are never retried through the fallback.

## Health

`hermes search-status` shows service reachability + version, next to the
search router state. Manual probe: `curl http://127.0.0.1:11235/health`.
