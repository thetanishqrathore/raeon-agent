"""Tiered fetching: cheap HTTP first, escalate to a real browser on demand.

Rung 1 — ``httpx`` GET with realistic browser headers, redirect following,
charset sniffing and retry/backoff. Good for the ~80% of pages that are
server-rendered.

Rung 2 — a real browser, used only when rung 1 returns a bot-challenge or an
empty JS-shell. We reuse Hermes' existing Camoufox stealth backend
(:mod:`tools.browser_camofox`) when it's configured, otherwise we signal
``needs_browser`` so the caller can advise the agent to use ``browser_navigate``.

This module never raises for network errors — it returns a :class:`FetchResult`
with ``error`` set, which the pipeline turns into a per-URL error entry.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Optional

import httpx

_DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_DEFAULT_HEADERS = {
    "User-Agent": _DEFAULT_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
              "image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    # NOTE: intentionally NOT setting Accept-Encoding. httpx advertises only the
    # codings it can actually decode (gzip/deflate, plus br/zstd iff the optional
    # packages are installed). Forcing "br" here makes servers send Brotli we
    # then can't decode → garbled bytes. Let httpx negotiate.
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
}

# Markers that a CDN bot-wall served a challenge instead of the page.
_BOT_WALL_RE = re.compile(
    r"(?:just a moment|checking your browser|cf-browser-verification|"
    r"cf-challenge|attention required|access denied|enable javascript and cookies|"
    r"verify you are (?:a )?human|px-captcha|are you a robot|ddos protection by)",
    re.IGNORECASE,
)
_CHARSET_RE = re.compile(rb'<meta[^>]+charset=["\']?\s*([\w\-]+)', re.IGNORECASE)
_SCRIPT_RE = re.compile(r"<script\b", re.IGNORECASE)


@dataclass
class FetchResult:
    url: str
    final_url: str = ""
    status_code: int = 0
    content_type: str = ""
    html: str = ""
    raw_bytes: bytes = b""
    fetched_via: str = "http"        # http | browser
    is_pdf: bool = False
    needs_browser: bool = False
    bot_blocked: bool = False
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and bool(self.html)


def _sniff_charset(data: bytes, header_charset: Optional[str]) -> str:
    if header_charset:
        return header_charset
    m = _CHARSET_RE.search(data[:4096])
    if m:
        try:
            return m.group(1).decode("ascii", "ignore")
        except Exception:  # noqa: BLE001
            pass
    return "utf-8"


def _looks_like_js_shell(html: str) -> bool:
    """Heuristic: a near-empty SPA shell whose body has scripts but ~no text."""
    if len(html) > 200_000:
        return False
    # crude visible-text estimate
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    visible = re.sub(r"\s+", " ", text).strip()
    scripts = len(_SCRIPT_RE.findall(html))
    has_app_root = re.search(r'id=["\'](?:root|app|__next|__nuxt)["\']', html) is not None
    return len(visible) < 220 and (scripts >= 1 or has_app_root)


async def _browser_fetch(url: str, timeout: float) -> Optional[FetchResult]:
    """Best-effort fetch via Hermes' Camoufox backend, if configured."""
    try:
        import os

        if not os.getenv("CAMOFOX_URL"):
            return None
        from tools import browser_camofox as cam  # type: ignore
    except Exception:  # noqa: BLE001
        return None

    # The camofox client API surface varies; probe for a usable html getter.
    try:
        for navigate, get_html in (
            (getattr(cam, "camofox_navigate", None), getattr(cam, "camofox_get_html", None)),
            (getattr(cam, "navigate", None), getattr(cam, "get_html", None)),
        ):
            if callable(navigate) and callable(get_html):
                if asyncio.iscoroutinefunction(navigate):
                    await navigate(url)
                else:
                    await asyncio.to_thread(navigate, url)
                html = (
                    await get_html() if asyncio.iscoroutinefunction(get_html)
                    else await asyncio.to_thread(get_html)
                )
                if html:
                    return FetchResult(url=url, final_url=url, status_code=200,
                                       content_type="text/html", html=html,
                                       fetched_via="browser")
    except Exception:  # noqa: BLE001
        return None
    return None


async def fetch(
    url: str,
    *,
    timeout: float = 30.0,
    max_retries: int = 2,
    allow_browser: bool = True,
    headers: Optional[dict] = None,
) -> FetchResult:
    """Fetch ``url`` with HTTP, escalating to a browser on bot-wall / JS-shell."""
    hdrs = dict(_DEFAULT_HEADERS)
    if headers:
        hdrs.update(headers)

    last_err = ""
    backoff = 0.6
    async with httpx.AsyncClient(
        follow_redirects=True, timeout=timeout, headers=hdrs, http2=False,
    ) as client:
        for attempt in range(max_retries + 1):
            try:
                resp = await client.get(url)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_err = f"{type(exc).__name__}: {exc}"
                if attempt < max_retries:
                    await asyncio.sleep(backoff)
                    backoff *= 2
                    continue
                return FetchResult(url=url, error=last_err)

            ctype = resp.headers.get("content-type", "").lower()
            status = resp.status_code

            # Retry transient throttling / server errors.
            if status in (429, 500, 502, 503, 504) and attempt < max_retries:
                retry_after = resp.headers.get("retry-after")
                delay = float(retry_after) if (retry_after or "").isdigit() else backoff
                await asyncio.sleep(min(delay, 10.0))
                backoff *= 2
                continue

            if "application/pdf" in ctype or url.lower().split("?")[0].endswith(".pdf"):
                return FetchResult(
                    url=url, final_url=str(resp.url), status_code=status,
                    content_type=ctype, raw_bytes=resp.content, is_pdf=True,
                )

            charset = resp.charset_encoding
            data = resp.content
            try:
                html = data.decode(_sniff_charset(data, charset), errors="replace")
            except (LookupError, UnicodeDecodeError):
                html = data.decode("utf-8", errors="replace")

            bot_blocked = (
                status in (401, 403, 429, 503)
                and bool(_BOT_WALL_RE.search(html[:6000]))
            ) or (status == 200 and len(html) < 4000 and bool(_BOT_WALL_RE.search(html)))
            js_shell = status == 200 and _looks_like_js_shell(html)

            if (bot_blocked or js_shell) and allow_browser:
                br = await _browser_fetch(url, timeout)
                if br is not None and br.ok:
                    return br

            result = FetchResult(
                url=url, final_url=str(resp.url), status_code=status,
                content_type=ctype, html=html, fetched_via="http",
                bot_blocked=bot_blocked,
                needs_browser=(bot_blocked or js_shell),
            )
            if status >= 400 and not html.strip():
                result.error = f"HTTP {status}"
            return result

    return FetchResult(url=url, error=last_err or "fetch failed")
