import os

os.environ.setdefault("SECURITY_JWT_SECRET", "unit-test-secret-1234567890-abcdefghijklmnopqrstuvwxyz")

from app.services import source_policy_engine as engine


def test_classify_source_tier_marks_allowlisted_domain_as_tier0(monkeypatch):
    monkeypatch.setattr(engine._settings().web_search, "official_source_allowlist", ["daad.de"])
    result = engine.classify_source_tier("https://www2.daad.de/deutschland/studienangebote")
    assert result["source_tier"] == engine.TIER0_OFFICIAL


def test_classify_source_tier_marks_gov_source_as_tier1():
    result = engine.classify_source_tier("https://www.study-in-germany.de/en/")
    assert result["source_tier"] in {engine.TIER1_CORROBORATION, engine.TIER0_OFFICIAL}


def test_slot_allows_tier_respects_slot_requirement():
    slot = {"slot_id": "application_deadline", "source_tier_requirement": "tier0_official"}
    assert engine.slot_allows_tier(slot, engine.TIER0_OFFICIAL) is True
    assert engine.slot_allows_tier(slot, engine.TIER1_CORROBORATION) is False
