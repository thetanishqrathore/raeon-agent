"""Tests for the enrich_contact tool (E2)."""

import json

import agent.lead_enrichment as le
from tools.enrich_contact_tool import enrich_contact, _to_domain, ENRICH_CONTACT_SCHEMA

_TEAM = ("Team: John Doe, CEO — john.doe@acme.aero. Jane Roe — jane.roe@acme.aero. "
         "info@acme.aero")


class TestDomain:
    def test_url_to_domain(self):
        assert _to_domain("https://www.acme.aero/contact") == "acme.aero"
        assert _to_domain("acme.aero") == "acme.aero"
        assert _to_domain("@acme.aero") == "acme.aero"


class TestEnrichContact:
    def test_full_record(self, monkeypatch):
        monkeypatch.setattr(le, "mx_valid", lambda d, **k: True)
        monkeypatch.setattr(le, "external_enrich", lambda *a, **k: None)
        out = json.loads(enrich_contact("https://acme.aero", "Sam Park",
                                        "Director of Technical Records", page_text=_TEAM))
        assert out["domain"] == "acme.aero"
        assert out["email"] == "sam.park@acme.aero"
        assert out["email_confidence"] == le.LIKELY
        assert out["backup_contact"] == "info@acme.aero"
        assert out["person_title"] == "Director of Technical Records"

    def test_missing_domain_errors(self):
        assert "error" in json.loads(enrich_contact(""))

    def test_known_pairs_anchor_pattern(self, monkeypatch):
        monkeypatch.setattr(le, "mx_valid", lambda d, **k: True)
        monkeypatch.setattr(le, "external_enrich", lambda *a, **k: None)
        out = json.loads(enrich_contact(
            "acme.aero", "Sam Park",
            known_pairs=[{"name": "Bob Lee", "email": "blee@acme.aero"}],
        ))
        assert out["email"] == "spark@acme.aero"  # {f}{last} pattern from the pair

    def test_schema_shape(self):
        assert ENRICH_CONTACT_SCHEMA["name"] == "enrich_contact"
        assert ENRICH_CONTACT_SCHEMA["parameters"]["required"] == ["domain"]


class TestRegistration:
    def test_registered_and_in_web_toolset(self):
        from tools.registry import registry, discover_builtin_tools
        import toolsets
        discover_builtin_tools()
        assert registry._tools.get("enrich_contact") is not None
        assert "enrich_contact" in toolsets.resolve_toolset("web")
        assert "enrich_contact" in toolsets.resolve_toolset("hermes-cli")
