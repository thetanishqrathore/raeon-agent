#!/usr/bin/env python3
"""Free-first contact enrichment for lead-gen (E1).

Turns "company + target role" into a contact record — person, email, and an
HONEST confidence — using only free signals: emails harvested from the
company's own pages, the org's email pattern inferred from name↔email pairs,
and an MX check (via DNS-over-HTTPS, no extra dependency). An optional external
provider (Hunter, gated on a key) is the only paid/verified path; absent it,
the layer degrades to Likely/Inferred and NEVER fabricates a "Verified" email.

Pure + deterministic except for two clearly-isolated network calls (`mx_valid`
DoH lookup, `external_enrich` opt-in) — both fail safe. Mirrors the
research_citations / research_ranking style.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# Confidence ladder (matches the lead-gen prompt's Email Confidence enum).
VERIFIED = "Verified"
LIKELY = "Likely"
INFERRED = "Inferred"
CONTACT_FORM = "Contact form only"
NOT_FOUND = "Not found"

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# A "Firstname Lastname" (each capitalized) — used to pair names with nearby emails.
_NAME_RE = re.compile(r"\b([A-Z][a-z]+)\s+([A-Z][a-z]+)\b")

# Local parts that are role/distribution addresses, not a person.
_GENERIC_LOCALS = frozenset({
    "info", "sales", "contact", "support", "admin", "hello", "careers", "jobs",
    "team", "office", "enquiries", "enquiry", "marketing", "hr", "press", "media",
    "general", "help", "service", "services", "mail", "no-reply", "noreply",
})

# Canonical local-part templates, tried in this order for fallback construction.
_PATTERNS = [
    "{first}.{last}", "{f}{last}", "{first}{last}", "{first}", "{f}.{last}",
    "{first}_{last}", "{first}{l}", "{last}", "{last}.{first}", "{last}{f}",
]


def parse_name(full_name: str) -> Tuple[str, str]:
    """('Jane A. Smith') -> ('jane', 'smith'). Strips titles/initials."""
    toks = [t for t in re.split(r"\s+", (full_name or "").strip()) if t]
    # Drop common honorifics and lone initials.
    drop = {"mr", "mrs", "ms", "dr", "prof", "sir", "mx"}
    clean = [re.sub(r"[^A-Za-z\-]", "", t) for t in toks]
    clean = [t for t in clean if t and t.lower().rstrip(".") not in drop and len(t) > 1]
    if not clean:
        return "", ""
    if len(clean) == 1:
        return clean[0].lower(), ""
    return clean[0].lower(), clean[-1].lower()


def apply_pattern(template: str, first: str, last: str, domain: str) -> str:
    first, last = first.lower(), last.lower()
    try:
        local = template.format(first=first, last=last,
                                f=(first[:1] or ""), l=(last[:1] or ""))
    except (KeyError, IndexError):
        return ""
    local = local.strip(".-_")
    return f"{local}@{domain}" if local else ""


def harvest_emails(text: str, domain: Optional[str] = None) -> Dict[str, List[str]]:
    """Return {'personal': [...], 'generic': [...]} emails found in *text*.

    Restricted to *domain* when given (so we don't pick up partners' addresses).
    """
    out_personal, out_generic, seen = [], [], set()
    for m in _EMAIL_RE.finditer(text or ""):
        email = m.group(0).lower().rstrip(".")
        if email in seen:
            continue
        seen.add(email)
        dom = email.split("@", 1)[1]
        if domain and dom != domain.lower():
            continue
        local = email.split("@", 1)[0]
        if local in _GENERIC_LOCALS or local.split("+")[0] in _GENERIC_LOCALS:
            out_generic.append(email)
        else:
            out_personal.append(email)
    return {"personal": out_personal, "generic": out_generic}


def extract_name_email_pairs(text: str, domain: Optional[str] = None) -> List[Tuple[str, str]]:
    """Heuristically pair a personal email with the nearest preceding name.

    Team/about pages typically render 'Jane Smith … jane.smith@co.com'. For each
    on-domain personal email we look back a short window for a 'First Last'.
    """
    pairs: List[Tuple[str, str]] = []
    text = text or ""
    for m in _EMAIL_RE.finditer(text):
        email = m.group(0).lower().rstrip(".")
        dom = email.split("@", 1)[1]
        if domain and dom != domain.lower():
            continue
        if email.split("@", 1)[0] in _GENERIC_LOCALS:
            continue
        window = text[max(0, m.start() - 80): m.start()]
        names = _NAME_RE.findall(window)
        if names:
            first, last = names[-1]
            pairs.append((f"{first} {last}", email))
    return pairs


def infer_pattern(pairs: List[Tuple[str, str]]) -> Tuple[Optional[str], int]:
    """Infer the org email template from name↔email pairs.

    Returns (template, support) where support = how many pairs agree. None when
    no pair yields a recognized template.
    """
    counts: Dict[str, int] = {}
    for full_name, email in pairs:
        first, last = parse_name(full_name)
        if not first:
            continue
        local = email.split("@", 1)[0]
        domain = email.split("@", 1)[1]
        for tmpl in _PATTERNS:
            # A single-word name renders {last}/{l} empty, making e.g.
            # "{first}.{last}" collapse to "{first}" and falsely win — skip
            # templates that reference a name part we don't have.
            if not last and ("{last}" in tmpl or "{l}" in tmpl):
                continue
            built = apply_pattern(tmpl, first, last, domain).split("@", 1)[0]
            if built and built == local:
                counts[tmpl] = counts.get(tmpl, 0) + 1
                break
    if not counts:
        return None, 0
    best = max(counts.items(), key=lambda kv: kv[1])
    return best[0], best[1]


def mx_valid(domain: str, timeout: float = 5.0) -> Optional[bool]:
    """True/False if the domain has MX records (accepts mail); None if unknown.

    Uses DNS-over-HTTPS (no dnspython dependency). Fails safe to None so a
    network hiccup never penalizes confidence.
    """
    if not domain:
        return None
    try:
        import httpx
        r = httpx.get("https://dns.google/resolve",
                      params={"name": domain, "type": "MX"}, timeout=timeout)
        if r.status_code != 200:
            return None
        data = r.json()
        status = data.get("Status")
        if status == 3:  # NXDOMAIN — the domain genuinely doesn't exist
            return False
        if status not in (0, None):  # SERVFAIL etc. — resolver hiccup, not "no MX"
            return None
        return any(a.get("type") == 15 for a in (data.get("Answer") or []))
    except Exception:
        return None


def external_enrich(domain: str, first: str, last: str) -> Optional[Dict[str, str]]:
    """Optional paid/verified provider seam. Returns a verified record or None.

    Currently supports Hunter's email-finder when ``HUNTER_API_KEY`` is set
    (free tier ~25/mo — reserve for top leads). Absent a key, returns None and
    the caller uses the free pattern-inference path. This is the upgrade slot
    for Apollo/RocketReach/etc. (mirrors the resilient-search provider model).
    """
    key = os.getenv("HUNTER_API_KEY", "").strip()
    if not key or not (first and domain):
        return None
    try:
        import httpx
        r = httpx.get("https://api.hunter.io/v2/email-finder",
                      params={"domain": domain, "first_name": first,
                              "last_name": last, "api_key": key}, timeout=15)
        if r.status_code != 200:
            return None
        d = (r.json() or {}).get("data") or {}
        email = d.get("email")
        if not email:
            return None
        # Hunter returns a deliverability/confidence score; treat a found email
        # with a non-trivial score as Verified, else Likely.
        score = d.get("score") or 0
        return {"email": email,
                "confidence": VERIFIED if score >= 80 else LIKELY,
                "source": f"Hunter email-finder (score {score})"}
    except Exception:
        return None


@dataclass
class ContactRecord:
    person_name: str = ""
    person_title: str = ""
    email: str = ""
    email_confidence: str = NOT_FOUND
    email_source: str = ""
    backup_contact: str = ""
    mx_valid: Optional[bool] = None
    candidates: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, object]:
        return {
            "person_name": self.person_name,
            "person_title": self.person_title,
            "email": self.email,
            "email_confidence": self.email_confidence,
            "email_source": self.email_source,
            "backup_contact": self.backup_contact,
            "mx_valid": self.mx_valid,
            "candidates": self.candidates,
        }


def enrich(
    domain: str,
    person_name: str = "",
    person_title: str = "",
    page_text: str = "",
    known_pairs: Optional[List[Tuple[str, str]]] = None,
    contact_url: str = "",
    check_mx: bool = True,
) -> ContactRecord:
    """Produce a contact record with honest confidence. Never fabricates Verified.

    Inputs are what the model already gathered: the company *domain*, the target
    *person_name*/*person_title*, and the *page_text* it extracted from the
    company's contact/about/team pages (the source of sample emails + patterns).
    """
    domain = (domain or "").strip().lower().lstrip("@")
    rec = ContactRecord(person_name=person_name.strip(), person_title=person_title.strip())
    if not domain:
        rec.email_source = "no domain provided"
        return rec

    harvested = harvest_emails(page_text, domain)
    pairs = list(known_pairs or []) + extract_name_email_pairs(page_text, domain)
    template, support = infer_pattern(pairs)
    first, last = parse_name(person_name)
    mx = mx_valid(domain) if check_mx else None
    rec.mx_valid = mx

    # 1. Verified path — only via an external provider.
    ext = external_enrich(domain, first, last) if first else None
    if ext:
        rec.email = ext["email"]
        rec.email_confidence = ext["confidence"]
        rec.email_source = ext["source"]
    # 2. Free pattern path.
    elif first and template:
        rec.email = apply_pattern(template, first, last, domain)
        rec.candidates = [rec.email]
        if support >= 2 and mx is not False:
            rec.email_confidence = LIKELY
            rec.email_source = f"pattern {template} confirmed by {support} samples; MX {_mx_str(mx)}"
        else:
            rec.email_confidence = INFERRED
            rec.email_source = f"pattern {template} from {support or 1} sample; MX {_mx_str(mx)}"
    elif first:
        # 3. No pattern found — common-pattern guess (clearly Inferred-low).
        rec.email = apply_pattern("{first}.{last}", first, last, domain)
        rec.candidates = [apply_pattern(t, first, last, domain) for t in _PATTERNS[:4]]
        rec.candidates = [c for c in rec.candidates if c]
        rec.email_confidence = INFERRED if mx is not False else NOT_FOUND
        rec.email_source = f"common-pattern guess {{first}}.{{last}}; MX {_mx_str(mx)}"

    # Backup contact + Contact-form/Not-found fallbacks.
    rec.backup_contact = (harvested["generic"][0] if harvested["generic"]
                          else (f"contact form: {contact_url}" if contact_url else ""))
    if not rec.email:
        if harvested["generic"] or contact_url:
            rec.email_confidence = CONTACT_FORM
            rec.email_source = "no personal email derivable; generic inbox / contact form only"
        else:
            rec.email_confidence = NOT_FOUND
            rec.email_source = rec.email_source or "no email found"
    return rec


def _mx_str(mx: Optional[bool]) -> str:
    return "valid" if mx else ("invalid" if mx is False else "unknown")
