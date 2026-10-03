#!/usr/bin/env python3
"""
Think Tool Module — forced standalone reflection turn.

A deliberately side-effect-free tool. Calling it does nothing observable except
echo the reflection back as an acknowledgement. Its entire value is procedural:
it gives the model a sanctioned way to spend ONE turn consolidating what it has
learned, naming the gaps that remain, and deciding the next move — instead of
reflexively firing another search.

This is the single highest-ROI / lowest-effort pattern from the deep-research
review (open_deep_research, onyx, deepagents all ship a `think_tool`). It is a
cheap substitute for a separate critic agent: structured self-reflection on its
own turn measurably improves stop decisions, so the agent neither under-researches
(answering from thin snippets) nor over-researches (looping past sufficiency).

CONTRACT (enforced by prompt guidance, not code): the model must call `think`
ALONE — never paired with other tool calls in the same turn — between major
search/read sets and before finalizing. Pairing it with a search defeats the
purpose (the reflection wouldn't see that search's results).

Zero external dependencies; always available; no I/O; no state mutation.
"""

import json
from typing import Optional


# A reflection shorter than this is almost certainly not a real consolidation
# pass (e.g. "ok", "continue"). We don't hard-fail it — that would punish the
# model mid-loop — but we nudge for the structured form.
_MIN_USEFUL_REFLECTION_CHARS = 12


def think_tool(reflection: str, next_step: Optional[str] = None) -> str:
    """Record a standalone reflection turn.

    Args:
        reflection: The model's structured reasoning — what was learned so far,
            which parts of the brief are now answered, what gaps/contradictions
            remain, and how confident each finding is. This is for the model's
            own benefit; it is not shown to the user.
        next_step: Optional one-line statement of the concrete next action
            (e.g. "search for X's 2026 pricing" or "sufficient — synthesize").

    Returns:
        JSON acknowledgement. No side effects.
    """
    if not reflection or not reflection.strip():
        return tool_error(
            "think requires a non-empty `reflection`: summarize findings so far, "
            "name the remaining gaps, and state your next step."
        )

    reflection = reflection.strip()
    payload = {
        "acknowledged": True,
        "reflection": reflection,
    }
    if next_step and next_step.strip():
        payload["next_step"] = next_step.strip()

    if len(reflection) < _MIN_USEFUL_REFLECTION_CHARS:
        # Soft nudge, not an error — keep the loop moving.
        payload["note"] = (
            "Reflection was very short. A useful reflection names what you now "
            "know, what is still missing, and the single next action."
        )

    return json.dumps(payload, ensure_ascii=False)


def check_think_requirements() -> bool:
    """Think tool has no external requirements -- always available."""
    return True


# =============================================================================
# OpenAI Function-Calling Schema
# =============================================================================

THINK_SCHEMA = {
    "name": "think",
    "description": (
        "Take a standalone reasoning turn to consolidate findings and plan the "
        "next move. This tool does nothing except record your reflection — its "
        "value is forcing you to STOP and think between research steps instead "
        "of reflexively searching again.\n\n"
        "Use it during multi-step research: after a set of searches/reads, "
        "before deciding whether to dig deeper or finalize, and whenever results "
        "are surprising, contradictory, or thin.\n\n"
        "CRITICAL: call `think` ALONE — never in the same turn as web_search, "
        "web_extract, delegate_task, or any other tool. Pairing it with a search "
        "defeats the purpose, because the reflection would not yet see that "
        "search's results. Reflect, then act on the next turn.\n\n"
        "In `reflection`, briefly cover: (1) what you've learned that answers "
        "the brief, (2) what's still missing or unverified, (3) any "
        "contradictions or single-sourced claims, (4) whether the evidence is "
        "now sufficient. Then put the single concrete next action in `next_step`."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "reflection": {
                "type": "string",
                "description": (
                    "Your structured reasoning: findings so far, gaps that "
                    "remain, contradictions/uncertainty, and a sufficiency "
                    "judgment. For your own use — not shown to the user."
                ),
            },
            "next_step": {
                "type": "string",
                "description": (
                    "Optional. One line naming the single next action — e.g. a "
                    "specific search to run, a page to read, a claim to verify, "
                    "or 'sufficient — synthesize the answer'."
                ),
            },
        },
        "required": ["reflection"],
    },
}


# --- Registry ---
from tools.registry import registry, tool_error

registry.register(
    name="think",
    toolset="web",
    schema=THINK_SCHEMA,
    handler=lambda args, **kw: think_tool(
        reflection=args.get("reflection", ""),
        next_step=args.get("next_step"),
    ),
    check_fn=check_think_requirements,
    emoji="🤔",
)
