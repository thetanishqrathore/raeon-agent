# Self-hosted SearXNG for Hermes

The recommended **free, uncapped, Google-grade** primary tier for the resilient
search router. SearXNG is a metasearch aggregator — it queries Google/Bing/
DuckDuckGo/Brave/etc. on your behalf, so you get Google-quality results with no
API key, no per-query cost, and no third-party rate cap (you run it).

## Setup

```bash
cd deploy/searxng
# 1. set a real secret
python -c "import secrets; print(secrets.token_hex(32))"   # paste into settings.yml: server.secret_key
# 2. launch
docker compose up -d
# 3. confirm the JSON API works (this is what Hermes calls)
curl 'http://localhost:8888/search?q=aircraft+technical+records&format=json' | head -c 300
```

## Wire it into Hermes

The SearXNG provider reads **`SEARXNG_URL`**, and it must reach the environment
Hermes actually runs in (same gotcha as the other keys — a shell `export` in
your terminal may not reach a service-launched Hermes):

```bash
export SEARXNG_URL=http://localhost:8888
```

Then turn the router on in `config.yaml`:

```yaml
web:
  search_backend: resilient
```

SearXNG is the first tier, so it serves the bulk; ddgs/Brave/Tavily/Google-CSE
are automatic fallbacks. Verify the whole chain with:

```bash
venv/bin/python -m plugins.web.hermes.eval.search_router_eval
```

## Why these settings matter
- **`formats: [html, json]`** — stock SearXNG disables the JSON API; Hermes calls
  `/search?format=json`, so this is required (it's the #1 reason public instances
  don't work).
- **`limiter: false`** — the agent is the only client; the bot-limiter would
  throttle/block Hermes' automated queries.
- **Keep it private** — bind to `127.0.0.1` on a server; it's an internal tool.
