#!/usr/bin/env python3
"""research_ledger tool (P1.3) — durable research scratchpad.

Persists the research brief + running state (learnings, visited URLs, claims) in
SQLite, OUTSIDE the message list, so it survives context compaction. The model
writes to it as it researches and reads it back (especially after a long run or
a compaction) so it never forgets the brief or re-treads visited sources.

Session-scoped via the ``session_id`` the dispatcher passes to every tool
handler — so it works through the normal registry dispatch path (no special
wiring, no access to the agent object needed). Storage lives in
:mod:`agent.research_state`.
"""

import json
from typing import Any, Optional


_VALID_ACTIONS = {"set_brief", "add_learning", "add_visited", "add_claim", "get", "clear"}


def research_ledger(args: dict, session_id: Optional[str] = None) -> str:
    """Dispatch a ledger action. Returns JSON; never raises."""
    action = str(args.get("action") or "").strip().lower()
    if action not in _VALID_ACTIONS:
        return tool_error(
            f"Unknown action '{action}'. Use one of: {sorted(_VALID_ACTIONS)}."
        )
    try:
        from agent.research_state import ResearchLedger

        ledger = ResearchLedger(session_id)
    except Exception as exc:  # noqa: BLE001
        return tool_error(f"could not open research ledger: {exc}")

    try:
        if action == "set_brief":
            brief = str(args.get("brief") or "").strip()
            if not brief:
                return tool_error("set_brief requires `brief`.")
            ledger.set_brief(brief)
            return json.dumps({"ok": True, "action": action})

        if action == "add_learning":
            claim = str(args.get("claim") or "").strip()
            if not claim:
                return tool_error("add_learning requires `claim`.")
            ledger.add_learning(
                claim,
                source_url=str(args.get("source_url") or ""),
                date=str(args.get("date") or ""),
                confidence=str(args.get("confidence") or ""),
            )
            return json.dumps({"ok": True, "action": action})

        if action == "add_visited":
            url = str(args.get("url") or "").strip()
            if not url:
                return tool_error("add_visited requires `url`.")
            new = ledger.add_visited_url(url)
            return json.dumps({"ok": True, "action": action, "new": new})

        if action == "add_claim":
            claim = str(args.get("claim") or "").strip()
            if not claim:
                return tool_error("add_claim requires `claim`.")
            ledger.add_claim(
                claim,
                status=str(args.get("status") or ""),
                evidence_url=str(args.get("evidence_url") or ""),
            )
            return json.dumps({"ok": True, "action": action})

        if action == "clear":
            ledger.clear()
            return json.dumps({"ok": True, "action": action})

        # action == "get"
        return ledger.render_digest()
    except Exception as exc:  # noqa: BLE001
        return tool_error(f"ledger {action} failed: {exc}")


def check_research_ledger_requirements() -> bool:
    """No external requirements -- always available."""
    return True


RESEARCH_LEDGER_SCHEMA = {
    "name": "research_ledger",
    "description": (
        "Durable research scratchpad that SURVIVES context compaction (stored "
        "outside the conversation). During a long or multi-round research task, "
        "persist your brief and findings here and read them back so you never "
        "lose the brief or re-read sources you already visited.\n\n"
        "Actions (`action`):\n"
        "- set_brief: save the frozen research brief (`brief`).\n"
        "- add_learning: record a finding (`claim`, optional `source_url`, "
        "`date`, `confidence`) — recording a source also marks it visited.\n"
        "- add_visited: mark a URL read (`url`); returns whether it was new "
        "(use to skip duplicates).\n"
        "- add_claim: record a load-bearing claim and its verification "
        "(`claim`, `status`, `evidence_url`).\n"
        "- get: return a compact digest of the whole ledger (read this after a "
        "long gap or if your context may have been compacted).\n"
        "- clear: wipe this session's ledger.\n\n"
        "Use it for genuinely long investigations; a quick lookup doesn't need it."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": sorted(_VALID_ACTIONS),
                "description": "Which ledger operation to perform.",
            },
            "brief": {"type": "string", "description": "For set_brief."},
            "claim": {"type": "string", "description": "For add_learning / add_claim."},
            "source_url": {"type": "string", "description": "For add_learning."},
            "date": {"type": "string", "description": "For add_learning (publication date)."},
            "confidence": {"type": "string", "description": "For add_learning: high/medium/uncertain."},
            "url": {"type": "string", "description": "For add_visited."},
            "status": {"type": "string", "description": "For add_claim: supported/partial/none."},
            "evidence_url": {"type": "string", "description": "For add_claim."},
        },
        "required": ["action"],
    },
}


# --- Registry ---
from tools.registry import registry, tool_error

registry.register(
    name="research_ledger",
    toolset="web",
    schema=RESEARCH_LEDGER_SCHEMA,
    handler=lambda args, **kw: research_ledger(args, session_id=kw.get("session_id")),
    check_fn=check_research_ledger_requirements,
    emoji="📒",
    max_result_size_chars=60_000,
)
