"""
Tests for the v2 deterministic retreat-matching system, covering the
"Required Tests" checklists in AI_DEVELOPER_CHANGES.md and
BACKEND_DEVELOPER_CHANGES.md.
"""

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

import main
from app.schemas.retreat_v2_schema import (
    Archetype,
    BreakFrom,
    ArrivalPriority,
    StructurePreference,
    ResetStyle,
    PhysicalIntensity,
    PartyType,
    Spirituality,
    PlanningServiceLevel,
    Setting,
    DurationBucket,
    RetreatRecommendationRequest,
)
from src.core.answer_mappings import (
    ARCHETYPE_TO_DB,
    PHYSICAL_INTENSITY_TO_DB,
    SPIRITUALITY_TO_DB,
    STRUCTURE_TO_DB,
    TRANSFORM_FOCUS_VALUES,
)
from src.core.matching_profile import build_matching_profile
from src.core.retreat_catalog import load_retreat_catalog, parse_avg_night, parse_best_season
from src.core.retreat_ranker import rank_properties
from src.core.retreat_scoring import apply_hard_filters, score_property
from src.core.restriction_extractor import extract_restrictions

client = TestClient(main.app)


@pytest.fixture(autouse=True)
def _stub_llm_explanations(monkeypatch):
    """
    Force the deterministic match_reasons fallback (see retreat_explanation.py)
    so these tests exercise the real filter/scoring pipeline without depending
    on network access, an API key, or non-deterministic model output.
    """
    def _disabled(*_args, **_kwargs):
        raise RuntimeError("LLM disabled in tests")

    monkeypatch.setattr("src.service.chat_services.get_ai_response", _disabled)
    yield


def _base_payload(**overrides) -> dict:
    payload = {
        "archetype": "burned_out_achiever",
        "escape_from": "noise_stimulation",
        "arrival_priority": "silence_privacy",
        "structure_preference": "optional_rituals",
        "reset_style": "digital_disconnection",
        "physical_intensity": "gentle",
        "party": {"type": "solo", "adults": 1, "children": 0},
        "spirituality": "none",
        "travel_window": {"mode": "flexible"},
        "planning_service_level": "well_planned",
        "restrictions": {"text": "", "codes": []},
        "settings": [],
        "budget": {"currency": "USD", "per_person_per_night_max": 500, "open_ended": False},
        "duration": {"bucket": "4_7_nights"},
        "transform_focus": ["Burnout Recovery", "Sleep"],
    }
    payload.update(overrides)
    return payload


def _build_request(**overrides) -> RetreatRecommendationRequest:
    return RetreatRecommendationRequest(**_base_payload(**overrides))


# ==================== Every enum maps to an intended DB dimension ====================

def test_every_archetype_enum_maps_to_a_catalog_archetype_token():
    catalog_tokens = set()
    for record in load_retreat_catalog():
        catalog_tokens.update(token.strip() for token in record.get("Archetypes", "").split(",") if token.strip())
    for archetype in Archetype:
        assert ARCHETYPE_TO_DB[archetype.value] in catalog_tokens


def test_every_structure_enum_maps_to_a_catalog_structure_value():
    catalog_values = {record.get("Structure", "").strip() for record in load_retreat_catalog()}
    for structure in StructurePreference:
        assert STRUCTURE_TO_DB[structure.value] in catalog_values


def test_every_physical_intensity_enum_maps_to_a_catalog_value():
    catalog_values = {record.get("Physical Intensity", "").strip() for record in load_retreat_catalog()}
    for intensity in PhysicalIntensity:
        assert PHYSICAL_INTENSITY_TO_DB[intensity.value] in catalog_values


def test_every_spirituality_enum_maps_to_a_catalog_value():
    catalog_values = {record.get("Spirituality", "").strip() for record in load_retreat_catalog()}
    for spirituality in Spirituality:
        assert SPIRITUALITY_TO_DB[spirituality.value] in catalog_values


def test_q4_middle_answers_both_map_to_semi_guided_but_preserve_raw_answer():
    request_optional = _build_request(structure_preference="optional_rituals")
    request_anchor = _build_request(structure_preference="one_daily_anchor")
    profile_optional = build_matching_profile(request_optional)
    profile_anchor = build_matching_profile(request_anchor)
    assert profile_optional.structure_db_value == "Semi-Guided"
    assert profile_anchor.structure_db_value == "Semi-Guided"
    assert profile_optional.raw_structure_answer == "optional_rituals"
    assert profile_anchor.raw_structure_answer == "one_daily_anchor"


def test_transform_focus_canonical_values_match_frontend_contract():
    # Copied verbatim from FRONTEND_DEVELOPER_CHANGES.md Q15 table.
    expected = {
        "Burnout Recovery", "Longevity", "Detox", "Weight Loss", "Spiritual Growth",
        "Emotional Healing", "Nervous System Reset", "Fitness", "Creativity",
        "Relationship Repair", "Community", "Sleep", "Digital Detox", "Cultural Immersion",
    }
    assert expected == TRANSFORM_FOCUS_VALUES


# ==================== Hard filters ====================

def test_spirituality_none_excludes_deeply_spiritual_retreats():
    request = _build_request(spirituality="none", budget={"currency": "USD", "per_person_per_night_max": 7000, "open_ended": True})
    profile = build_matching_profile(request)
    result = rank_properties(profile, limit=150)
    returned_ids = {candidate.property_id for candidate in result.ranked}
    for record in load_retreat_catalog():
        from src.core.retreat_catalog import make_property_id
        if record.get("Spirituality") in {"Light", "Moderate", "Deep"}:
            assert make_property_id(record) not in returned_ids


def test_budget_hard_filter_excludes_properties_above_max_when_not_open_ended():
    request = _build_request(budget={"currency": "USD", "per_person_per_night_max": 100, "open_ended": False})
    profile = build_matching_profile(request)
    result = rank_properties(profile, limit=150)
    for candidate in result.ranked:
        price = parse_avg_night(candidate.record)
        if price["amount"] is not None:
            assert price["amount"] <= 100


def test_open_ended_budget_does_not_exclude_expensive_properties():
    request = _build_request(budget={"currency": "USD", "per_person_per_night_max": 100, "open_ended": True})
    profile = build_matching_profile(request)
    result = rank_properties(profile, limit=150)
    prices = [parse_avg_night(candidate.record)["amount"] for candidate in result.ranked]
    assert any(price is not None and price > 100 for price in prices)


def test_gentle_request_excludes_challenging_properties():
    request = _build_request(physical_intensity="gentle")
    profile = build_matching_profile(request)
    result = rank_properties(profile, limit=150)
    for candidate in result.ranked:
        assert candidate.record.get("Physical Intensity") != "Challenging"


def test_specific_season_excludes_non_overlapping_months():
    request = _build_request(travel_window={"mode": "specific", "season": "choose_month", "months": [1]})
    profile = build_matching_profile(request)
    assert profile.travel_months == [1]
    result = rank_properties(profile, limit=150)
    for candidate in result.ranked:
        assert 1 in parse_best_season(candidate.record)


def test_hard_filters_execute_before_scoring():
    """A property failing a hard filter must never appear in the scored/ranked output."""
    request = _build_request(spirituality="none")
    profile = build_matching_profile(request)
    catalog = load_retreat_catalog()
    deep_record = next(r for r in catalog if r.get("Spirituality") == "Deep")
    filter_result = apply_hard_filters(deep_record, profile)
    assert filter_result.passed is False
    result = rank_properties(profile, limit=150)
    from src.core.retreat_catalog import make_property_id
    assert make_property_id(deep_record) not in {c.property_id for c in result.ranked}


# ==================== Scoring stability / audit ====================

def test_ranking_is_stable_across_repeated_calls():
    request = _build_request()
    profile = build_matching_profile(request)
    first = [c.property_id for c in rank_properties(profile, limit=20).ranked]
    second = [c.property_id for c in rank_properties(profile, limit=20).ranked]
    assert first == second


def test_score_breakdown_components_sum_to_total_score():
    request = _build_request()
    profile = build_matching_profile(request)
    catalog = load_retreat_catalog()
    scored = score_property(catalog[0], profile)
    assert round(sum(scored.breakdown.values()), 1) == round(scored.total_score, 1)


# ==================== Restriction extraction ====================

def test_unverified_restrictions_produce_warnings_via_api():
    payload = _base_payload(restrictions={"text": "No hiking or long drives please", "codes": []})
    response = client.post("/v2/retreat-recommendations", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert "no_hiking" in data["extracted_restrictions"]["codes"]
    assert "no_long_drives" in data["extracted_restrictions"]["codes"]
    assert any("Activity/Restriction Tags" in gap for gap in data["data_gaps"])
    for recommendation in data["recommendations"]:
        assert recommendation["restriction_status"] == "unverified"


def test_restriction_extraction_preserves_original_text_and_flags_unresolved():
    extracted = extract_restrictions("No hiking, and please no interpretive dance sessions", [])
    assert extracted.original_text == "No hiking, and please no interpretive dance sessions"
    assert "no_hiking" in extracted.codes
    assert any("interpretive dance" in clause for clause in extracted.unresolved_text)


def test_no_restrictions_gives_not_applicable_status():
    payload = _base_payload(restrictions={"text": "", "codes": []})
    response = client.post("/v2/retreat-recommendations", json=payload)
    data = response.json()
    for recommendation in data["recommendations"]:
        assert recommendation["restriction_status"] == "not_applicable"


# ==================== Request validation ====================

def test_transform_focus_rejects_more_than_three():
    with pytest.raises(ValidationError):
        _build_request(transform_focus=["Sleep", "Detox", "Fitness", "Creativity"])


def test_transform_focus_rejects_unknown_value():
    with pytest.raises(ValidationError):
        _build_request(transform_focus=["Not A Real Focus"])


def test_family_party_requires_at_least_one_adult():
    with pytest.raises(ValidationError):
        _build_request(party={"type": "family", "adults": 0, "children": 2})


def test_flexible_travel_window_rejects_months():
    with pytest.raises(ValidationError):
        _build_request(travel_window={"mode": "flexible", "months": [1, 2]})


def test_setting_no_preference_submits_empty_array():
    request = _build_request(settings=[])
    profile = build_matching_profile(request)
    assert profile.settings == []


# ==================== API-level response contract ====================

def test_api_returns_stable_property_ids_and_score_breakdown():
    response = client.post("/v2/retreat-recommendations", json=_base_payload())
    assert response.status_code == 200
    data = response.json()
    assert data["recommendations"], "expected at least one recommendation"
    for recommendation in data["recommendations"]:
        assert recommendation["property_id"].startswith("retreat_")
        assert set(recommendation["score_breakdown"].keys()) == {
            "archetype", "transform_focus", "emotional_tone", "structure",
            "physical_intensity", "party_social", "emotional_safety",
            "nature", "luxury", "spirituality",
        }
    assert data["excluded_count"] + len(data["recommendations"]) <= data["total_candidate_count"]


def test_recommendation_session_can_be_retrieved_by_id():
    created = client.post("/v2/retreat-recommendations", json=_base_payload())
    session_id = created.json()["recommendation_session_id"]
    fetched = client.get(f"/v2/retreat-recommendations/{session_id}")
    assert fetched.status_code == 200
    assert fetched.json()["recommendation_session_id"] == session_id


def test_unknown_recommendation_session_returns_404():
    response = client.get("/v2/retreat-recommendations/does-not-exist")
    assert response.status_code == 404


def test_invalid_enum_value_is_rejected():
    payload = _base_payload(archetype="not_a_real_archetype")
    response = client.post("/v2/retreat-recommendations", json=payload)
    assert response.status_code == 422


def test_legacy_get_suggested_city_endpoint_is_untouched():
    """The old endpoint's schema must still work unmodified after the v2 additions."""
    from app.schemas.city_body import QuestionAnswers
    legacy = QuestionAnswers(
        todays_feeling="curious",
        experience_kind="culture",
        energy_level="medium",
        travel_style="slow",
        trip_organization="loose",
        activity_restrictions=["crowds"],
        life_season="exploration",
        preferred_environments=["cities"],
        birthdate="1990-01-01",
        total_trip_budget=1200.0,
        trip_length_days=3,
    )
    assert legacy.effective_total_budget == 1200.0
