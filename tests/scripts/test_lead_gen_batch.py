"""Unit tests for the pure candidate-filter helpers in scripts/lead_gen_batch.py.

Regression context (July 2026 audit): the junk filter used raw substring
matching, so "x.com" in JUNK silently dropped every company whose domain ends
in "…x.com" (flex.com, avionx.com, …) and "bing." dropped "plumbing.com"; the
dedupe base took the last two labels, so ALL .co.uk / .com.au companies
collapsed into a single "co.uk" candidate — silent lead loss for the
UK/EU-heavy aviation ICP.
"""

import scripts.lead_gen_batch as lgb


class TestJunkDomainMatching:
    def test_junk_domains_still_filtered(self):
        for d in ("linkedin.com", "de.linkedin.com", "en.wikipedia.org",
                  "x.com", "jobs.x.com", "faa.gov", "easa.europa.eu",
                  "caa.co.uk", "news.yahoo.com"):
            assert lgb._is_junk_domain(d), d

    def test_label_boundaries_prevent_false_positives(self):
        # Substring matching used to drop all of these legitimate companies.
        for d in ("flex.com", "avionx.com", "plumbing.com", "governair.com",
                  "notlinkedin.example", "tubingsolutions.aero"):
            assert not lgb._is_junk_domain(d), d


class TestDedupeBase:
    def test_plain_tld_uses_two_labels(self):
        assert lgb._dedupe_base("acme.aero") == "acme.aero"
        assert lgb._dedupe_base("records.acme.com") == "acme.com"

    def test_public_sld_uses_three_labels(self):
        # "co.uk" must NOT become the dedupe key for every UK company.
        assert lgb._dedupe_base("acme.co.uk") == "acme.co.uk"
        assert lgb._dedupe_base("www2.beta.com.au") == "beta.com.au"
        assert lgb._dedupe_base("acme.co.uk") != lgb._dedupe_base("other.co.uk")


def _known(*urls):
    from agent.research_citations import canonicalize_url
    return {canonicalize_url(u) for u in urls}


class TestEvidenceVerification:
    """C5: evidence/source URLs must come from what the batch actually fetched."""

    def test_clean_row_is_verified(self):
        row = {
            "Evidence Found": "Site mentions AMOS migration (see https://acme.aero/about).",
            "Source URLs": "https://acme.aero/about | https://acme.aero/team",
        }
        ok, offending = lgb.verify_row_evidence(
            row, _known("https://acme.aero/about", "https://acme.aero/team"))
        assert ok is True and offending == []
        assert lgb._evidence_verified_value(ok, offending) == "Yes"

    def test_hallucinated_evidence_url_is_flagged_not_dropped(self):
        row = {
            "Evidence Found": "Per https://acme.aero/press/records-award they won an award.",
            "Source URLs": "https://acme.aero/about",
        }
        ok, offending = lgb.verify_row_evidence(row, _known("https://acme.aero/about"))
        assert ok is False
        assert offending == ["https://acme.aero/press/records-award"]
        value = lgb._evidence_verified_value(ok, offending)
        assert value.startswith("No — unretrieved:") and "press/records-award" in value

    def test_canonicalization_tolerates_tracking_and_slashes(self):
        row = {
            "Evidence Found": "",
            "Source URLs": "https://acme.aero/about/?utm_source=x | HTTPS://ACME.AERO/team",
        }
        ok, offending = lgb.verify_row_evidence(
            row, _known("https://acme.aero/about", "https://acme.aero/team/"))
        assert ok is True, offending

    def test_urls_outside_evidence_fields_are_ignored(self):
        # A LinkedIn URL in its own column is provenance, not evidence.
        row = {
            "Evidence Found": "No URLs here.",
            "Source URLs": "https://acme.aero/about",
            "Target Person LinkedIn": "https://linkedin.com/in/someone",
        }
        ok, offending = lgb.verify_row_evidence(row, _known("https://acme.aero/about"))
        assert ok is True and offending == []

    def test_overflow_offenders_are_summarized(self):
        offending = [f"https://x{i}.com/p" for i in range(5)]
        value = lgb._evidence_verified_value(False, offending)
        assert "(+2 more)" in value

    def test_column_is_appended_last(self):
        # Positional consumers rely on existing offsets; the new column is last.
        assert lgb.COLUMNS[-1] == "Evidence Verified"
        assert lgb.COLUMNS.index("Status") == len(lgb.COLUMNS) - 2


class TestOutputEmail:
    """Audit fix: MX-invalid 'Not found' guesses must not surface in the sheet."""

    def test_not_found_with_invalid_mx_is_blanked(self):
        enr = {"email": "jane.doe@acme.aero", "email_confidence": "Not found",
               "mx_valid": False}
        assert lgb._output_email(enr) == ""

    def test_not_found_with_unknown_mx_is_kept(self):
        # Spec is MX *invalid* + Not found; an unknown MX (resolver hiccup)
        # keeps the candidate visible with its honest confidence label.
        enr = {"email": "jane.doe@acme.aero", "email_confidence": "Not found",
               "mx_valid": None}
        assert lgb._output_email(enr) == "jane.doe@acme.aero"

    def test_inferred_with_invalid_mx_is_kept(self):
        enr = {"email": "jane.doe@acme.aero", "email_confidence": "Inferred",
               "mx_valid": False}
        assert lgb._output_email(enr) == "jane.doe@acme.aero"

    def test_empty_email_stays_empty(self):
        assert lgb._output_email({"email": "", "email_confidence": "Not found",
                                  "mx_valid": False}) == ""
