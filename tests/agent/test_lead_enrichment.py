"""Tests for the free contact-enrichment core (E1)."""

from unittest import mock

import agent.lead_enrichment as le
from agent.lead_enrichment import (
    parse_name, apply_pattern, harvest_emails, extract_name_email_pairs,
    infer_pattern, enrich, VERIFIED, LIKELY, INFERRED, CONTACT_FORM, NOT_FOUND,
)

_TEAM = ("Our team: John Doe, CEO — john.doe@acme.aero. "
         "Jane Roe, CTO — jane.roe@acme.aero. Contact: info@acme.aero")


class TestPureHelpers:
    def test_parse_name(self):
        assert parse_name("Jane A. Smith") == ("jane", "smith")
        assert parse_name("Dr. John Doe") == ("john", "doe")
        assert parse_name("Cher") == ("cher", "")

    def test_apply_pattern(self):
        assert apply_pattern("{first}.{last}", "Jane", "Smith", "d.com") == "jane.smith@d.com"
        assert apply_pattern("{f}{last}", "Jane", "Smith", "d.com") == "jsmith@d.com"
        assert apply_pattern("{first}", "Jane", "Smith", "d.com") == "jane@d.com"

    def test_harvest_classifies_and_filters_domain(self):
        h = harvest_emails(_TEAM, domain="acme.aero")
        assert set(h["personal"]) == {"john.doe@acme.aero", "jane.roe@acme.aero"}
        assert h["generic"] == ["info@acme.aero"]

    def test_extract_pairs(self):
        pairs = extract_name_email_pairs(_TEAM, domain="acme.aero")
        emails = {e for _, e in pairs}
        assert "john.doe@acme.aero" in emails and "jane.roe@acme.aero" in emails

    def test_infer_pattern(self):
        tmpl, support = infer_pattern([("John Doe", "john.doe@acme.aero"),
                                       ("Jane Roe", "jane.roe@acme.aero")])
        assert tmpl == "{first}.{last}" and support == 2

    def test_infer_pattern_flast(self):
        tmpl, _ = infer_pattern([("John Doe", "jdoe@x.com")])
        assert tmpl == "{f}{last}"

    def test_infer_pattern_single_name_matches_first_not_firstlast(self):
        # With no last name, "{first}.{last}" renders identical to "{first}" —
        # the inference must credit "{first}", not a template needing a last name.
        tmpl, support = infer_pattern([("Cher", "cher@x.com")])
        assert tmpl == "{first}" and support == 1


class TestEnrich:
    def test_likely_from_confirmed_pattern(self, monkeypatch):
        monkeypatch.setattr(le, "mx_valid", lambda d, **k: True)
        monkeypatch.setattr(le, "external_enrich", lambda *a, **k: None)
        rec = enrich("acme.aero", "Sam Park", "Director of Technical Records", page_text=_TEAM)
        assert rec.email == "sam.park@acme.aero"
        assert rec.email_confidence == LIKELY
        assert "confirmed by 2" in rec.email_source
        assert rec.backup_contact == "info@acme.aero"

    def test_inferred_common_pattern_when_no_samples(self, monkeypatch):
        monkeypatch.setattr(le, "mx_valid", lambda d, **k: True)
        monkeypatch.setattr(le, "external_enrich", lambda *a, **k: None)
        rec = enrich("acme.aero", "Sam Park", page_text="reach us: info@acme.aero")
        assert rec.email == "sam.park@acme.aero"
        assert rec.email_confidence == INFERRED
        assert "common-pattern guess" in rec.email_source
        assert len(rec.candidates) >= 2  # offers alternatives

    def test_contact_form_only_when_no_target(self, monkeypatch):
        monkeypatch.setattr(le, "mx_valid", lambda d, **k: True)
        rec = enrich("acme.aero", "", page_text="info@acme.aero", contact_url="https://acme.aero/contact")
        assert rec.email == ""
        assert rec.email_confidence == CONTACT_FORM
        assert rec.backup_contact == "info@acme.aero"

    def test_not_found_when_nothing(self, monkeypatch):
        monkeypatch.setattr(le, "mx_valid", lambda d, **k: None)
        rec = enrich("acme.aero", "", page_text="no emails here")
        assert rec.email_confidence == NOT_FOUND

    def test_verified_via_external_provider(self, monkeypatch):
        monkeypatch.setattr(le, "mx_valid", lambda d, **k: True)
        monkeypatch.setattr(le, "external_enrich",
                            lambda dom, f, l: {"email": "sam.park@acme.aero",
                                               "confidence": VERIFIED, "source": "Hunter"})
        rec = enrich("acme.aero", "Sam Park", page_text=_TEAM)
        assert rec.email_confidence == VERIFIED and rec.email_source == "Hunter"

    def test_never_verified_without_provider(self, monkeypatch):
        # Free path must never claim Verified.
        monkeypatch.setattr(le, "mx_valid", lambda d, **k: True)
        monkeypatch.delenv("HUNTER_API_KEY", raising=False)
        rec = enrich("acme.aero", "Sam Park", page_text=_TEAM)
        assert rec.email_confidence in (LIKELY, INFERRED)


class TestMxAndProviderGates:
    def test_external_enrich_no_key_returns_none(self, monkeypatch):
        monkeypatch.delenv("HUNTER_API_KEY", raising=False)
        assert le.external_enrich("acme.aero", "sam", "park") is None

    def test_mx_valid_doh_true(self, monkeypatch):
        class R:
            status_code = 200
            def json(self):
                return {"Status": 0, "Answer": [{"type": 15, "data": "10 mx.acme"}]}
        monkeypatch.setattr("httpx.get", lambda *a, **k: R())
        assert le.mx_valid("acme.aero") is True

    def test_mx_valid_doh_none_on_error(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("net")
        monkeypatch.setattr("httpx.get", boom)
        assert le.mx_valid("acme.aero") is None

    def test_mx_valid_nxdomain_is_false(self, monkeypatch):
        class R:
            status_code = 200
            def json(self):
                return {"Status": 3}  # NXDOMAIN — domain doesn't exist
        monkeypatch.setattr("httpx.get", lambda *a, **k: R())
        assert le.mx_valid("no-such-domain.aero") is False

    def test_mx_valid_servfail_is_unknown_not_false(self, monkeypatch):
        # A resolver hiccup (SERVFAIL) must not penalize confidence.
        class R:
            status_code = 200
            def json(self):
                return {"Status": 2}  # SERVFAIL
        monkeypatch.setattr("httpx.get", lambda *a, **k: R())
        assert le.mx_valid("acme.aero") is None
