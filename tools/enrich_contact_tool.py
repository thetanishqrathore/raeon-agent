#!/usr/bin/env python3
"""enrich_contact tool (E2) — free-first contact enrichment for lead-gen.

Given a company domain + a target person/role + the page text the model already
extracted (contact / about / team pages), returns a contact record with an
HONEST confidence level (Verified / Likely / Inferred / Contact form only /
Not found) — filling the lead-gen sheet's people/email columns. Never fabricates
a Verified email. Deterministic core in :mod:`agent.lead_enrichment`; the only
network calls are an MX check (DoH) and an optional Hunter lookup (gated on a key).
"""

import json
from typing import Any, List, Optional, Tuple
from urllib.parse import urlsplit


def _to_domain(domain_or_url: str) -> str:
    s = (domain_or_url or "").strip()
    if not s:
        return ""
    if "://" in s or s.startswith("www.") or "/" in s:
        netloc = urlsplit(s if "://" in s else "https://" + s).netloc or s
        s = netloc
    s = s.lower().lstrip("@")
    if s.startswith("www."):
        s = s[4:]
    return s.split("/")[0].split(":")[0]


def _coerce_pairs(known_pairs: Any) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    if isinstance(known_pairs, list):
        for p in known_pairs:
            if isinstance(p, dict) and p.get("name") and p.get("email"):
                out.append((str(p["name"]), str(p["email"])))
            elif isinstance(p, (list, tuple)) and len(p) == 2:
                out.append((str(p[0]), str(p[1])))
    return out


def enrich_contact(
    domain: str,
    person_name: str = "",
    person_title: str = "",
    page_text: str = "",
    contact_url: str = "",
    known_pairs: Any = None,
) -> str:
    dom = _to_domain(domain)
    if not dom:
        return tool_error("Provide the company `domain` (or a company URL).")
    try:
        from agent.lead_enrichment import enrich

        rec = enrich(
            dom,
            person_name=person_name or "",
            person_title=person_title or "",
            page_text=page_text or "",
            known_pairs=_coerce_pairs(known_pairs),
            contact_url=contact_url or "",
        )
    except Exception as exc:  # noqa: BLE001
        return tool_error(f"enrichment failed: {exc}")
    out = rec.to_dict()
    out["domain"] = dom
    return json.dumps(out, ensure_ascii=False, indent=2)


def check_enrich_contact_requirements() -> bool:
    """No external requirements — free path always available (Hunter optional)."""
    return True


ENRICH_CONTACT_SCHEMA = {
    "name": "enrich_contact",
    "description": (
        "Build a professional contact record for a lead with an HONEST "
        "confidence level. FIRST web_extract the company's contact / about / "
        "team / leadership pages, THEN call this with that text as `page_text` "
        "plus the company `domain` and the target person's name/title.\n\n"
        "It harvests sample emails from the page, infers the org's email "
        "pattern (e.g. first.last@), constructs the target's email, checks the "
        "domain's MX records, and returns: person, email, email_confidence "
        "(Verified / Likely / Inferred / Contact form only / Not found), the "
        "source/pattern used, and a backup contact (generic inbox / contact "
        "form). It NEVER invents a Verified email — without a verification "
        "provider, expect Likely/Inferred, which you must record as such. Use "
        "the returned confidence + source verbatim in the sheet."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "domain": {"type": "string", "description": "Company domain or URL (e.g. acme.aero)."},
            "person_name": {"type": "string", "description": "Target decision-maker's full name (if known)."},
            "person_title": {"type": "string", "description": "Their title/role (if known)."},
            "page_text": {"type": "string", "description": "Text you extracted from the company's contact/about/team pages (the source of sample emails + the pattern)."},
            "contact_url": {"type": "string", "description": "Optional contact-form URL, used as a backup contact."},
            "known_pairs": {
                "type": "array",
                "items": {"type": "object"},
                "description": "Optional known name→email pairs ([{name,email}]) to anchor the pattern.",
            },
        },
        "required": ["domain"],
    },
}


# --- Registry ---
from tools.registry import registry, tool_error

registry.register(
    name="enrich_contact",
    toolset="web",
    schema=ENRICH_CONTACT_SCHEMA,
    handler=lambda args, **kw: enrich_contact(
        domain=args.get("domain", ""),
        person_name=args.get("person_name", "") or "",
        person_title=args.get("person_title", "") or "",
        page_text=args.get("page_text", "") or "",
        contact_url=args.get("contact_url", "") or "",
        known_pairs=args.get("known_pairs"),
    ),
    check_fn=check_enrich_contact_requirements,
    emoji="📇",
    max_result_size_chars=20_000,
)
