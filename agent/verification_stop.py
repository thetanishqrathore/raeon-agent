"""Turn-end verification guard for coding edits.

This module is intentionally policy-only. It never runs checks itself; it turns
the passive verification ledger into a bounded follow-up when the model tries to
finish immediately after editing code without fresh evidence.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

logger = logging.getLogger(__name__)


_MAX_CHANGED_PATHS_IN_NUDGE = 8

# Non-code file extensions whose edits carry no verifiable runtime behavior:
# documentation, prose, and data/markup that no test/build exercises. When a
# turn touches ONLY these, verify-on-stop has nothing to check, so the nudge is
# suppressed (this is fix "C" for the doc/markdown/skill false-positive — a
# SKILL.md or README edit must never demand a /tmp verification script). A turn
# that edits any non-listed path (a real source/code/config file) still nudges.
_NON_CODE_VERIFY_EXTENSIONS = frozenset(
    {
        ".md",
        ".markdown",
        ".mdx",
        ".rst",
        ".txt",
        ".text",
        ".adoc",
        ".asciidoc",
        ".org",
        ".log",
        ".csv",
        ".tsv",
    }
)

# Filenames (case-insensitive, extension-less or otherwise) that are pure prose
# even without a recognized doc extension.
_NON_CODE_VERIFY_FILENAMES = frozenset(
    {
        "license",
        "licence",
        "notice",
        "authors",
        "contributors",
        "changelog",
        "codeowners",
    }
)


def _is_non_code_path(raw: str) -> bool:
    """Return True when a changed path is documentation/prose with nothing to verify."""
    try:
        p = Path(str(raw))
    except Exception:
        return False
    suffix = p.suffix.lower()
    if suffix in _NON_CODE_VERIFY_EXTENSIONS:
        return True
    if not suffix and p.name.lower() in _NON_CODE_VERIFY_FILENAMES:
        return True
    return False


def _filter_verifiable_paths(paths: Iterable[str]) -> list[str]:
    """Drop documentation/prose paths; keep paths that could have verifiable behavior."""
    return [p for p in paths if p and not _is_non_code_path(p)]


# Session identities (platform or source) that are NOT human conversational
# messaging surfaces: interactive coding surfaces (CLI, TUI, desktop, codex,
# local, gateway) and programmatic callers (API server, webhooks, tools).
# Verify-on-stop stays ON by default for these. Any other resolved gateway
# platform is a conversational messaging surface (Telegram, Discord, WhatsApp,
# Signal, Slack, etc.) where the verification narrative would reach a human as
# chat noise, so it defaults OFF. Mirrors LOCAL_SESSION_SOURCE_IDS in
# apps/desktop/src/lib/session-source.ts; keep roughly in sync when adding a
# local or programmatic surface. Default-deny by design: an unrecognized
# identity is treated as messaging (OFF) so a new chat platform never leaks the
# verification receipt before this set is updated.
_NON_MESSAGING_SESSION_SURFACES = frozenset(
    {
        "",
        "cli",
        "codex",
        "desktop",
        "gateway",
        "local",
        "tui",
        "tool",
        "api_server",
        "webhook",
        "msgraph_webhook",
    }
)


def _session_is_messaging_surface() -> bool:
    """Return whether this turn is delivered over a human messaging channel.

    The gateway binds the platform value (e.g. ``telegram``) to
    ``HERMES_SESSION_PLATFORM``; the CLI and TUI set ``HERMES_SESSION_SOURCE``
    (e.g. ``cli``, ``tui``) instead. Both are consulted via the session-context
    helper (with an ``os.environ`` fallback), alongside the ``HERMES_PLATFORM``
    override, matching the sibling platform resolution in
    ``agent/skill_commands.py`` and ``agent/prompt_builder.py``. A turn is a
    messaging surface when a resolved identity is present and is not a known
    non-messaging surface.
    """
    try:
        from gateway.session_context import get_session_env

        platform = (
            os.getenv("HERMES_PLATFORM")
            or get_session_env("HERMES_SESSION_PLATFORM", "")
        )
        source = get_session_env("HERMES_SESSION_SOURCE", "")
    except Exception:
        platform = os.getenv("HERMES_PLATFORM", "") or os.environ.get(
            "HERMES_SESSION_PLATFORM", ""
        )
        source = os.environ.get("HERMES_SESSION_SOURCE", "")
    for identity in (platform, source):
        identity = str(identity or "").strip().lower()
        if identity and identity not in _NON_MESSAGING_SESSION_SURFACES:
            return True
    return False


def verify_on_stop_enabled(config: dict[str, Any] | None = None) -> bool:
    """Return whether edit -> verify-before-finish behavior is enabled.

    Precedence: an explicit ``HERMES_VERIFY_ON_STOP`` env var wins, then an
    explicit ``agent.verify_on_stop`` config value. The config default is
    ``False`` (see ``DEFAULT_CONFIG``) — verify-on-stop is OFF unless the user
    opts in. The legacy ``"auto"`` sentinel is still honored for anyone who
    sets it explicitly: it resolves to ON for interactive coding surfaces
    (CLI, TUI, desktop) and programmatic callers, and OFF for conversational
    messaging surfaces (Telegram, Discord, etc.). A missing/unknown value
    falls back to OFF.
    """
    env = os.environ.get("HERMES_VERIFY_ON_STOP")
    if env is not None:
        return env.strip().lower() not in {"0", "false", "no", "off"}
    if config is None:
        try:
            from hermes_cli.config import load_config

            config = load_config()
        except Exception:
            config = {}
    agent_cfg = (config or {}).get("agent") if isinstance(config, dict) else None
    cfg_val = agent_cfg.get("verify_on_stop") if isinstance(agent_cfg, dict) else None
    if isinstance(cfg_val, bool):
        return cfg_val
    if isinstance(cfg_val, str):
        token = cfg_val.strip().lower()
        if token in {"1", "true", "yes", "on"}:
            return True
        if token in {"0", "false", "no", "off"}:
            return False
        if token == "auto":
            # Explicit opt-in to the legacy surface-aware behavior.
            return not _session_is_messaging_surface()
    # Missing or unknown value -> OFF (the new default).
    return False


def _candidate_cwds(paths: Iterable[str]) -> list[Path]:
    candidates: list[Path] = []
    seen: set[str] = set()
    for raw in paths:
        if not raw:
            continue
        try:
            path = Path(raw).expanduser()
            candidate = path if path.is_dir() else path.parent
            resolved = str(candidate.resolve())
        except Exception:
            continue
        if resolved not in seen:
            seen.add(resolved)
            candidates.append(Path(resolved))
    return candidates


def _verification_snapshot(
    *,
    session_id: str | None,
    changed_paths: list[str],
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Return ``(status, facts)`` for the first edited workspace needing proof."""
    try:
        from agent.coding_context import project_facts_for
        from agent.verification_evidence import verification_status
    except Exception:
        return None

    first_snapshot: tuple[dict[str, Any], dict[str, Any]] | None = None
    for cwd in _candidate_cwds(changed_paths):
        facts = project_facts_for(cwd)
        if not facts:
            continue
        status = verification_status(session_id=session_id, cwd=cwd)
        snapshot = (status, facts)
        if first_snapshot is None:
            first_snapshot = snapshot
        if str(status.get("status") or "unverified") != "passed":
            return snapshot
    return first_snapshot


def _format_changed_paths(paths: list[str]) -> str:
    shown = paths[:_MAX_CHANGED_PATHS_IN_NUDGE]
    lines = [f"- `{path}`" for path in shown]
    remaining = len(paths) - len(shown)
    if remaining > 0:
        lines.append(f"- ... and {remaining} more")
    return "\n".join(lines)


def _status_detail(status: dict[str, Any]) -> str:
    state = str(status.get("status") or "unverified")
    evidence = status.get("evidence") if isinstance(status.get("evidence"), dict) else None
    if not evidence:
        return state

    command = evidence.get("canonical_command") or evidence.get("command")
    summary = str(evidence.get("output_summary") or "").strip()
    parts = [state]
    if command:
        parts.append(f"last command `{command}`")
    if summary:
        max_summary = 1200
        if len(summary) > max_summary:
            summary = summary[:max_summary].rstrip() + "\n... [truncated]"
        parts.append(f"last output:\n{summary}")
    return "\n".join(parts)


def build_verify_on_stop_nudge(
    *,
    session_id: str | None,
    changed_paths: Iterable[str],
    attempts: int = 0,
    max_attempts: int = 2,
) -> str | None:
    """Return a synthetic follow-up when edited code lacks fresh verification."""
    # Drop documentation/prose paths (markdown, skills, README, LICENSE, ...) —
    # they carry no verifiable behavior, so a turn that touched only those has
    # nothing to verify and must not nudge.
    paths = sorted({str(p) for p in _filter_verifiable_paths(changed_paths)})
    if not paths or attempts >= max_attempts:
        return None

    snapshot = _verification_snapshot(session_id=session_id, changed_paths=paths)
    if snapshot is None:
        return None
    status, facts = snapshot

    verify_commands = [
        str(cmd).strip()
        for cmd in (facts.get("verifyCommands") or [])
        if str(cmd).strip()
    ]

    state = str(status.get("status") or "unverified")
    if state == "passed":
        return None

    if verify_commands:
        command_instruction = (
            "Run the relevant verification command now ("
            + ", ".join(f"`{cmd}`" for cmd in verify_commands[:3])
            + (", ..." if len(verify_commands) > 3 else "")
            + "), read any failure, repair the code, and summarize what passed."
        )
    else:
        temp_dir = tempfile.gettempdir()
        command_instruction = (
            "No canonical test/lint/build command was detected. Create a focused "
            f"temporary verification script under `{temp_dir}` using an OS-safe "
            "`tempfile` path with a `hermes-verify-` filename prefix, run it "
            "against the changed behavior, clean it up when possible, and "
            "summarize it explicitly as ad-hoc verification rather than suite "
            "green."
        )

    return (
        "[System: You edited code in this turn, but the workspace does not have "
        "fresh passing verification evidence yet.\n\n"
        f"Verification status: {_status_detail(status)}\n\n"
        f"Changed paths:\n{_format_changed_paths(paths)}\n\n"
        f"{command_instruction} If verification is not possible, explain the "
        "concrete blocker instead of claiming the work is fully verified.]"
    )


# ---------------------------------------------------------------------------
# Research citation / provenance check (P0.3)
#
# A sibling of verify-on-stop, scoped to *research answers* instead of code
# edits: when the model finalizes an answer whose cited URLs were never actually
# retrieved this session, it gets one warning-then-revise turn. The heavy
# lifting (URL parsing, retrieved-set collection, fabrication detection) lives
# in agent/research_citations.py; this is the policy/gating layer, matching the
# shape of build_verify_on_stop_nudge above.
# ---------------------------------------------------------------------------


def research_citation_check_enabled(config: dict[str, Any] | None = None) -> bool:
    """Return whether the research citation/provenance check runs on stop.

    Precedence: ``HERMES_VERIFY_CITATIONS`` env var, then
    ``research.verify_citations`` config (default ON). The check is additionally
    self-gating — it only emits a nudge when web research actually happened AND
    the answer cites a URL never retrieved — so leaving it ON is inert for
    non-research sessions.
    """
    env = os.environ.get("HERMES_VERIFY_CITATIONS")
    if env is not None:
        return env.strip().lower() not in {"0", "false", "no", "off"}
    if config is None:
        try:
            from hermes_cli.config import load_config

            config = load_config()
        except Exception:
            config = {}
    research_cfg = (config or {}).get("research") if isinstance(config, dict) else None
    cfg_val = research_cfg.get("verify_citations") if isinstance(research_cfg, dict) else None
    if isinstance(cfg_val, bool):
        return cfg_val
    if isinstance(cfg_val, str):
        token = cfg_val.strip().lower()
        if token in {"1", "true", "yes", "on"}:
            return True
        if token in {"0", "false", "no", "off"}:
            return False
    return True  # default ON; self-gating keeps it dormant outside research


def _final_text_from_message(final_msg: Any) -> str:
    """Best-effort extraction of the assistant's final answer text."""
    if not isinstance(final_msg, dict):
        return ""
    content = final_msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                parts.append(part.get("text") or part.get("content") or "")
            else:
                parts.append(str(part))
        return " ".join(p for p in parts if p)
    return "" if content is None else str(content)


def build_research_citation_nudge(
    *,
    final_msg: Any,
    messages: list[Any],
    attempts: int = 0,
    max_attempts: int = 1,
    config: dict[str, Any] | None = None,
) -> str | None:
    """Return a revise nudge when a research answer cites unretrieved URLs.

    Self-gating warning-then-revise: returns ``None`` when the check is
    disabled, no web research happened, the citations are clean, the answer has
    no text, or the attempt budget is exhausted. Never raises (a checker bug
    must never block a final answer).

    When the URL-membership check comes back clean, the semantic
    citation-SUPPORT pass (A1, below) runs on the same stop path — also
    warning-then-revise, also fail-open, sharing the same attempt budget.
    """
    if not research_citation_check_enabled(config):
        return None
    try:
        from agent.research_citations import build_citation_revise_nudge

        final_text = _final_text_from_message(final_msg)
        if not final_text.strip():
            return None
        nudge = build_citation_revise_nudge(
            final_text, messages, attempts=attempts, max_attempts=max_attempts
        )
        if nudge:
            return nudge
    except Exception:
        return None
    # URL membership is clean → semantic support pass (A1). Its own try so a
    # support-check bug can never suppress a legitimate final answer.
    try:
        return _build_citation_support_nudge(
            final_text,
            messages,
            attempts=attempts,
            max_attempts=max_attempts,
            config=config,
        )
    except Exception:
        logger.debug("citation support check failed open", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Citation SUPPORT verification (A1) — semantic sibling of the URL-membership
# check above. The membership check proves a cited URL was *retrieved*; this
# pass asks the auxiliary model whether the retrieved content actually
# *supports* the claim citing it (Anthropic CitationAgent pattern).
#
# Non-negotiables, mirroring the membership validator:
#   * fail-open — aux missing/erroring/timing out skips with a structured
#     note; it NEVER blocks or hard-fails a report;
#   * bounded — ≤ research.citation_support_max_checks checks per report, a
#     hard per-check timeout, and one shared warning-then-revise attempt;
#   * self-gating — inert unless web research happened AND the answer pairs
#     claims with retrieved sources.
# The (claim, URL, excerpt) plumbing is pure code in research_citations.py;
# only the verdict comes from the model.
# ---------------------------------------------------------------------------

_SUPPORT_PER_CHECK_TIMEOUT_S = 20.0   # hard timeout for one aux verdict
_SUPPORT_TOTAL_TIMEOUT_S = 120.0      # hard ceiling for the whole pass
_SUPPORT_CONCURRENCY = 4
_SUPPORT_EXCERPT_CHARS = 4000
# CONTRADICTED is the hard signal. A single NOT_SUPPORTED is tolerated as
# excerpt-selection noise; two or more indicate decorative citing.
_SUPPORT_MIN_UNSUPPORTED_FOR_NUDGE = 2
_SUPPORT_MAX_CHECKS_DEFAULT = 12

_SUPPORT_SYSTEM_PROMPT = (
    "You verify whether a cited source supports a claim from a research "
    "report. Judge ONLY from the provided source excerpt; do not use outside "
    "knowledge. Reply with exactly one word: SUPPORTED (the excerpt states or "
    "directly implies the claim), PARTIAL (it supports part of the claim), "
    "NOT_SUPPORTED (it contains nothing about the claim), or CONTRADICTED "
    "(it states the opposite of the claim)."
)

_SUPPORT_USER_TEMPLATE = (
    "CLAIM:\n{claim}\n\n"
    "CITED SOURCE: {url}\n"
    "SOURCE EXCERPT:\n{excerpt}\n\n"
    "One-word verdict:"
)


def citation_support_enabled(config: dict[str, Any] | None = None) -> bool:
    """Gate for the semantic support pass.

    Precedence: ``HERMES_VERIFY_CITATION_SUPPORT`` env var, then
    ``research.verify_citation_support`` config (default ON). Runs only on the
    stop path already gated by ``research.verify_citations``.
    """
    env = os.environ.get("HERMES_VERIFY_CITATION_SUPPORT")
    if env is not None:
        return env.strip().lower() not in {"0", "false", "no", "off"}
    if config is None:
        try:
            from hermes_cli.config import load_config

            config = load_config()
        except Exception:
            config = {}
    research_cfg = (config or {}).get("research") if isinstance(config, dict) else None
    cfg_val = (
        research_cfg.get("verify_citation_support")
        if isinstance(research_cfg, dict)
        else None
    )
    if isinstance(cfg_val, bool):
        return cfg_val
    if isinstance(cfg_val, str):
        token = cfg_val.strip().lower()
        if token in {"1", "true", "yes", "on"}:
            return True
        if token in {"0", "false", "no", "off"}:
            return False
    return True


def citation_support_max_checks(config: dict[str, Any] | None = None) -> int:
    """``research.citation_support_max_checks`` (default 12, never negative)."""
    if config is None:
        try:
            from hermes_cli.config import load_config

            config = load_config()
        except Exception:
            config = {}
    research_cfg = (config or {}).get("research") if isinstance(config, dict) else None
    raw = (
        research_cfg.get("citation_support_max_checks")
        if isinstance(research_cfg, dict)
        else None
    )
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return _SUPPORT_MAX_CHECKS_DEFAULT


def classify_support_verdict(text: str | None) -> str:
    """Map a judge reply onto a verdict; anything unparseable is UNKNOWN.

    Order matters: NOT_SUPPORTED contains 'SUPPORTED'.
    """
    t = (text or "").strip().upper()
    if not t:
        return "UNKNOWN"
    if "CONTRADICT" in t:
        return "CONTRADICTED"
    if "NOT_SUPPORTED" in t or "NOT SUPPORTED" in t or "UNSUPPORTED" in t:
        return "NOT_SUPPORTED"
    if "PARTIAL" in t:
        return "PARTIAL"
    if "SUPPORT" in t:
        return "SUPPORTED"
    return "UNKNOWN"


def _build_support_aux_call() -> Optional[Callable[..., Any]]:
    """Resolve the web-extract auxiliary route into a ``(system, user) -> text``
    coroutine function, or ``None`` when no auxiliary model is configured.

    Reuses the exact resolution the scraper's LLM-extract layer uses
    (``tools.web_tools._resolve_web_extract_auxiliary``) so citation checks
    ride the same cheap model the extract degradation path already routes to.
    """
    try:
        from agent.auxiliary_client import async_call_llm, extract_content_or_reasoning
        from tools.web_tools import _resolve_web_extract_auxiliary

        client, model, extra_body = _resolve_web_extract_auxiliary()
        if client is None or not model:
            return None

        async def _call(system: str, user: str) -> Optional[str]:
            kwargs: dict[str, Any] = {
                "task": "citation_support",
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": 0.0,
                "max_tokens": 64,
            }
            if extra_body:
                kwargs["extra_body"] = extra_body
            return extract_content_or_reasoning(await async_call_llm(**kwargs))

        return _call
    except Exception:
        return None


def _run_coro_bounded(coro: Any, timeout: float) -> Any:
    """Drive a coroutine from sync code with a hard wall-clock bound.

    Works with or without an event loop already running in this thread (a
    running loop gets a dedicated worker thread with its own loop).
    """

    async def _bounded() -> Any:
        return await asyncio.wait_for(coro, timeout)

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_bounded())
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, _bounded()).result(timeout + 10)


def run_citation_support_check(
    final_text: str,
    messages: list[Any],
    *,
    config: dict[str, Any] | None = None,
    aux_call: Optional[Callable[..., Any]] = None,
) -> dict[str, Any]:
    """Check whether cited sources semantically support the claims citing them.

    Returns a structured report; never raises:
      ``status``       'checked' | 'skipped'
      ``reason``       skip reason ('aux_unavailable', 'no_claim_citation_pairs', …)
      ``checked``      number of (claim, URL) pairs judged
      ``verdicts``     [{claim, url, verdict}, …]
      ``contradicted`` / ``unsupported``  the flagged subsets
      ``should_revise``  whether the outcome warrants a revise nudge
      ``notes``        structured skip/degradation notes (fail-open receipts)
    """
    report: dict[str, Any] = {
        "status": "skipped",
        "reason": "",
        "checked": 0,
        "verdicts": [],
        "contradicted": [],
        "unsupported": [],
        "should_revise": False,
        "notes": [],
    }
    try:
        from agent.research_citations import (
            canonicalize_url,
            collect_source_contents,
            extract_claim_citation_pairs,
            interleave_pairs_by_url,
            select_support_excerpt,
        )

        max_checks = citation_support_max_checks(config)
        if max_checks <= 0:
            report["reason"] = "max_checks_zero"
            return report
        pairs = extract_claim_citation_pairs(final_text)
        if not pairs:
            report["reason"] = "no_claim_citation_pairs"
            return report
        contents = collect_source_contents(messages)
        checkable = [
            (claim, url) for claim, url in pairs
            if contents.get(canonicalize_url(url))
        ]
        uncheckable = len(pairs) - len(checkable)
        if uncheckable:
            report["notes"].append(
                f"{uncheckable} pair(s) skipped: cited URL has no extracted "
                "content in this session"
            )
        if not checkable:
            report["reason"] = "no_source_content"
            return report
        checkable = interleave_pairs_by_url(checkable)[:max_checks]

        if aux_call is None:
            aux_call = _build_support_aux_call()
        if aux_call is None:
            report["reason"] = "aux_unavailable"
            report["notes"].append(
                "auxiliary model unavailable — semantic citation check "
                "skipped (fail-open)"
            )
            return report

        async def _one(claim: str, url: str) -> dict[str, Any]:
            excerpt = select_support_excerpt(
                contents[canonicalize_url(url)], claim, _SUPPORT_EXCERPT_CHARS
            )
            user = _SUPPORT_USER_TEMPLATE.format(claim=claim, url=url, excerpt=excerpt)
            try:
                raw = await asyncio.wait_for(
                    aux_call(_SUPPORT_SYSTEM_PROMPT, user),
                    _SUPPORT_PER_CHECK_TIMEOUT_S,
                )
            except Exception:
                return {"claim": claim, "url": url, "verdict": "UNKNOWN"}
            return {"claim": claim, "url": url, "verdict": classify_support_verdict(raw)}

        async def _run_all() -> list[dict[str, Any]]:
            sem = asyncio.Semaphore(_SUPPORT_CONCURRENCY)

            async def _guarded(claim: str, url: str) -> dict[str, Any]:
                async with sem:
                    return await _one(claim, url)

            return await asyncio.gather(
                *[_guarded(claim, url) for claim, url in checkable]
            )

        try:
            verdicts = _run_coro_bounded(_run_all(), _SUPPORT_TOTAL_TIMEOUT_S)
        except Exception:
            report["reason"] = "check_error_or_timeout"
            report["notes"].append(
                "semantic citation check errored or timed out — skipped (fail-open)"
            )
            return report

        report["status"] = "checked"
        report["checked"] = len(verdicts)
        report["verdicts"] = verdicts
        report["contradicted"] = [v for v in verdicts if v["verdict"] == "CONTRADICTED"]
        report["unsupported"] = [v for v in verdicts if v["verdict"] == "NOT_SUPPORTED"]
        unknown = sum(1 for v in verdicts if v["verdict"] == "UNKNOWN")
        if unknown:
            report["notes"].append(
                f"{unknown} check(s) inconclusive (timeout/aux error) — not "
                "counted against the report"
            )
        report["should_revise"] = bool(report["contradicted"]) or (
            len(report["unsupported"]) >= _SUPPORT_MIN_UNSUPPORTED_FOR_NUDGE
        )
        return report
    except Exception:
        logger.debug("run_citation_support_check failed open", exc_info=True)
        report["reason"] = report["reason"] or "internal_error"
        return report


def _clip(text: str, n: int = 140) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def render_citation_support_nudge(report: dict[str, Any]) -> str | None:
    """Format a checked support report into a revise instruction (pure)."""
    if not report or report.get("status") != "checked" or not report.get("should_revise"):
        return None
    lines = [
        "[System: citation support check — an auxiliary model compared each "
        "claim in your answer against the content of the source it cites. "
        "Some citations do not hold up:",
        "",
    ]
    if report.get("contradicted"):
        lines.append("CONTRADICTED by the cited source:")
        for v in report["contradicted"][:6]:
            lines.append(f'  - "{_clip(v["claim"])}" — {v["url"]}')
        lines.append("")
    if report.get("unsupported"):
        lines.append("Cited source does NOT contain the claim:")
        for v in report["unsupported"][:6]:
            lines.append(f'  - "{_clip(v["claim"])}" — {v["url"]}')
        lines.append("")
    lines.append(
        "Fix this now: for each item, re-read the source with web_extract and "
        "either correct the claim to what the source actually says, cite a "
        "retrieved source that does support it, or remove the claim. Do not "
        "pad citations. Then re-emit the corrected answer.]"
    )
    return "\n".join(lines)


def _build_citation_support_nudge(
    final_text: str,
    messages: list[Any],
    *,
    attempts: int,
    max_attempts: int,
    config: dict[str, Any] | None,
) -> str | None:
    """Gate + run + render the support pass for the stop path."""
    if attempts >= max_attempts:
        return None
    if not citation_support_enabled(config):
        return None
    from agent.research_citations import web_research_occurred

    if not web_research_occurred(messages):
        return None
    report = run_citation_support_check(final_text, messages, config=config)
    if report.get("status") != "checked":
        if report.get("reason") not in ("no_claim_citation_pairs", ""):
            logger.info(
                "citation support check skipped (%s): %s",
                report.get("reason"),
                "; ".join(report.get("notes") or []) or "no notes",
            )
        return None
    return render_citation_support_nudge(report)


__all__ = [
    "build_verify_on_stop_nudge",
    "verify_on_stop_enabled",
    "build_research_citation_nudge",
    "research_citation_check_enabled",
    "citation_support_enabled",
    "citation_support_max_checks",
    "classify_support_verdict",
    "run_citation_support_check",
    "render_citation_support_nudge",
]
