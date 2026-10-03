# Crawl4AI — self-hosted extraction service

[Crawl4AI](https://github.com/unclecode/crawl4ai) is the browser-based
web-extraction engine behind Hermes' `crawl4ai` extract backend
(`web.extract_backend: crawl4ai`). It renders pages in headless Chromium
(Playwright) server-side and returns clean fit-markdown, which makes it far
more reliable than plain HTTP fetching on JS-heavy/SPA pages.

Free, no API key, runs entirely on this machine — same posture as the
SearXNG container (`hermes-searxng`) it sits next to.

## Deployed service

| | |
|---|---|
| Container | `hermes-crawl4ai` |
| Image | `unclecode/crawl4ai:0.9.0` (pin the tag; `latest` moves) |
| Bind | `127.0.0.1:11235` (loopback only — never expose publicly) |
| Memory | capped at 4 GB (`--memory=4g`), `--shm-size=1g` for Chromium |
| Restart | `unless-stopped` |
| Auth | `CRAWL4AI_API_TOKEN` bearer token (see below) |

### Why the token is REQUIRED (v0.9.0+)

Crawl4AI 0.9.0 is *secure by default*: without `CRAWL4AI_API_TOKEN` the
server binds loopback **inside the container**, so a `-p` port mapping
cannot reach it at all. Setting the token makes gunicorn bind all container
interfaces (we then map it to host loopback only) and requires
`Authorization: Bearer <token>` on every data endpoint (`/health` stays
open). Generate a random one; it is shared only between this container and
Hermes via `~/.hermes/.env` — **never commit it**.

## Exact run command

```bash
# 1. Generate a token once and keep it in ~/.hermes/.env:
#      CRAWL4AI_URL=http://127.0.0.1:11235
#      CRAWL4AI_API_TOKEN=<output of the command below>
python3 -c 'import secrets; print(secrets.token_hex(32))'

# 2. Run the service (reads the token back out of ~/.hermes/.env):
TOKEN=$(grep '^CRAWL4AI_API_TOKEN=' ~/.hermes/.env | cut -d= -f2)
docker run -d \
  --name hermes-crawl4ai \
  --restart unless-stopped \
  -p 127.0.0.1:11235:11235 \
  --shm-size=1g \
  --memory=4g --memory-swap=4g \
  -e CRAWL4AI_API_TOKEN="$TOKEN" \
  unclecode/crawl4ai:0.9.0
```

### Or with docker compose

`docker-compose.yml` in this directory is the same thing declaratively:

```bash
cd docker/crawl4ai
CRAWL4AI_API_TOKEN=$(grep '^CRAWL4AI_API_TOKEN=' ~/.hermes/.env | cut -d= -f2) docker compose up -d
```

## Verify

```bash
curl -s http://127.0.0.1:11235/health
# → {"status":"ok", ..., "version":"0.9.0"}

TOKEN=$(grep '^CRAWL4AI_API_TOKEN=' ~/.hermes/.env | cut -d= -f2)
curl -s -X POST http://127.0.0.1:11235/md \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com","f":"fit"}'
# → {"url":..., "markdown":"# Example Domain\n...", "success":true}
```

## Manage

```bash
docker logs -f hermes-crawl4ai        # tail logs
docker restart hermes-crawl4ai        # restart (config.yaml untouched)
docker stats hermes-crawl4ai          # live memory/cpu (capped at 4g)
docker rm -f hermes-crawl4ai          # remove (re-run command above to recreate)
```

Upgrade: bump the image tag here and in `docker-compose.yml`, then
`docker pull unclecode/crawl4ai:<new>` and recreate the container. Re-run the
smoke test in `tests/plugins/web/test_crawl4ai_provider.py` (integration
tests auto-skip when the service is down).

## API surface Hermes uses

* `POST /crawl` — `{"urls": [...], "browser_config": {...}, "crawler_config": {...}}`
  → `{"success": true, "results": [{url, status_code, metadata.title,
  markdown: {raw_markdown, fit_markdown, ...}, redirected_url, error_message, ...}]}`.
  One request per URL (a hard-blocked page 500s the whole request, so
  batching would let one bad URL poison the batch).
* `GET /health` — unauthenticated liveness + version probe (used by
  `hermes search-status`).

SSRF note: Hermes checks every URL against the private-IP/metadata-endpoint
blocklist (`tools/url_safety.py`) **before** sending it to this container —
the crawl runs server-side, so the container would otherwise happily fetch
`http://169.254.169.254/...` from inside the host network. Do not disable
that guard; the container's own egress pinning is a second layer, not a
replacement.
