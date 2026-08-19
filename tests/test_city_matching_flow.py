"""
Tests for the merged city-suggestion flow: POST /get_suggested_city now runs
the deterministic property-matching engine and groups ranked properties into
cities, instead of asking a model to invent destinations. See
API_CITY_FLOW_DOCS.md for the full contract this covers.
"""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import main
from app.schemas.retreat_v2_schema import RetreatRecommendationRequest
from src.core.legacy_profile_adapter import build_legacy_answers
from src.core.matching_profile import build_matching_profile
from src.core.retreat_matching_orchestrator import (
    build_ranked_pool,
    select_city_representatives,
)

client = TestClient(main.app)


@pytest.fixture(autouse=True)
def _stub_llm_explanations(monkeypatch):
    def _disabled(*_args, **_kwargs):
        raise RuntimeError("LLM disabled in tests")

    monkeypatch.setattr("src.service.chat_services.get_ai_response", _disabled)
    yield


def _v2_payload(**overrides) -> dict:
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


# ==================== select_city_representatives ====================

def test_select_city_representatives_returns_distinct_locations_only():
    request = RetreatRecommendationRequest(**_v2_payload())
    _, ranking = build_ranked_pool(request)
    representatives = select_city_representatives(ranking.ranked, limit=5)
    locations = [
        (c.record.get("Region", "").lower(), c.record.get("Country", "").lower())
        for c in representatives
    ]
    assert len(locations) == len(set(locations))


def test_select_city_representatives_excludes_given_property_ids():
    request = RetreatRecommendationRequest(**_v2_payload())
    _, ranking = build_ranked_pool(request)
    first_batch = select_city_representatives(ranking.ranked, limit=5)
    exclude = {c.property_id for c in first_batch}
    second_batch = select_city_representatives(ranking.ranked, exclude_property_ids=exclude, limit=5)
    assert not exclude & {c.property_id for c in second_batch}


def test_select_city_representatives_picks_best_score_per_location():
    """Within one location, the representative must be the highest-scoring property there."""
    request = RetreatRecommendationRequest(**_v2_payload())
    _, ranking = build_ranked_pool(request, pool_size=150)
    representatives = select_city_representatives(ranking.ranked, limit=150)
    best_by_location = {}
    for candidate in ranking.ranked:
        key = (candidate.record.get("Region", "").lower(), candidate.record.get("Country", "").lower())
        best_by_location.setdefault(key, candidate)
    for representative in representatives:
        key = (representative.record.get("Region", "").lower(), representative.record.get("Country", "").lower())
        assert representative.property_id == best_by_location[key].property_id


# ==================== legacy_profile_adapter ====================

def test_legacy_adapter_maps_planning_service_and_settings_directly():
    request = RetreatRecommendationRequest(**_v2_payload(
        planning_service_level="hour_by_hour",
        settings=["ocean_beach", "mountains"],
    ))
    profile = build_matching_profile(request)
    legacy = build_legacy_answers(request, profile)
    assert "hour-by-hour" in legacy.trip_organization
    assert "ocean/beach" in legacy.preferred_environments
    assert "mountains" in legacy.preferred_environments


def test_legacy_adapter_has_no_birthdate_and_carries_budget_and_duration():
    request = RetreatRecommendationRequest(**_v2_payload())
    profile = build_matching_profile(request)
    legacy = build_legacy_answers(request, profile)
    assert legacy.birthdate is None
    assert legacy.budget_per_person_per_night == 500
    assert legacy.trip_length_days == profile.duration_nights_estimate
    assert legacy.effective_total_budget == 500 * profile.duration_nights_estimate


def test_legacy_adapter_surfaces_restrictions_as_activity_restrictions():
    request = RetreatRecommendationRequest(**_v2_payload(
        restrictions={"text": "No hiking or long drives", "codes": []}
    ))
    profile = build_matching_profile(request)
    legacy = build_legacy_answers(request, profile)
    assert "no_hiking" in legacy.activity_restrictions
    assert "no_long_drives" in legacy.activity_restrictions


# ==================== API-level: /get_suggested_city ====================

def test_get_suggested_city_returns_real_properties_grouped_by_city():
    response = client.post("/get_suggested_city", json=_v2_payload())
    assert response.status_code == 200
    data = response.json()
    cities = data["suggested_cities"]
    assert cities
    for city in cities:
        assert city["property_id"].startswith("retreat_")
        assert isinstance(city["match_score"], int)
    city_names = [c["city_name"] for c in cities]
    assert len(city_names) == len(set(city_names))
    assert "response" in data
    assert "data_gaps" in data["response"]
    assert "extracted_restrictions" in data["response"]


def test_get_suggested_city_rejects_legacy_payload_shape():
    """The old 12-question InputData shape is no longer a valid request body."""
    response = client.post(
        "/get_suggested_city",
        json={
            "questions_answers": {"todays_feeling": "curious"},
            "hope_of_this_trip": "relax",
        },
    )
    assert response.status_code == 422


def test_regenerate_suggested_city_never_repeats_a_property_across_calls():
    initial = client.post("/get_suggested_city", json=_v2_payload())
    session_id = initial.json()["session_id"]
    first_ids = {c["property_id"] for c in initial.json()["suggested_cities"]}

    regenerated = client.post(
        "/regenerate_suggested_city",
        json={"session_id": session_id, "user_instruction": "more variety"},
    )
    assert regenerated.status_code == 200
    second_ids = {c["property_id"] for c in regenerated.json()["suggested_cities"]}
    assert not first_ids & second_ids


def test_regenerate_suggested_city_unknown_session_is_404():
    response = client.post(
        "/regenerate_suggested_city",
        json={"session_id": "does-not-exist", "user_instruction": "x"},
    )
    assert response.status_code == 404


def test_get_tour_plan_resolves_the_exact_selected_property():
    initial = client.post("/get_suggested_city", json=_v2_payload())
    session_id = initial.json()["session_id"]
    chosen = initial.json()["suggested_cities"][0]

    with patch("app.router.city_content_route.get_ai_response") as mock_ai, \
         patch("app.router.city_content_route.get_google_hotels_by_facilities") as mock_hotel_fac, \
         patch("app.router.city_content_route.get_detailed_tourist_places") as mock_places, \
         patch("app.router.city_content_route.get_nearby_restaurants") as mock_restaurants, \
         patch("app.router.city_content_route.calculate_distance_routes_api") as mock_distance:
        mock_ai.return_value = (
            '{"tour_plan":[{"day":1,"activities":['
            '{"activity_name":"Spa","activity_description":"relax","activity_location":"onsite",'
            '"activity_time":"10:00 AM - 11:00 AM","activity_cost":0}]}],'
            '"total_cost_estimate":0,"packing_tips":"t","travel_tips":"t"}'
        )
        mock_hotel_fac.invoke.return_value = [{
            "name": "Test Hotel", "address": "1 Rd", "rating": 4.5,
            "price_level": "PRICE_LEVEL_LUXURY", "photos": [], "coords": None,
        }]
        mock_places.invoke.return_value = []
        mock_restaurants.invoke.return_value = []
        mock_distance.invoke.return_value = {"error": "skip"}

        plan = client.post(
            "/get_tour_plan",
            json={"session_id": session_id, "selected_city": "irrelevant", "property_id": chosen["property_id"]},
        )
    assert plan.status_code == 200
    assert plan.json()["city"] == chosen["city_name"]
