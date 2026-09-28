"""
API tests for the Velari intake flow: POST /get_suggested_city ranks the
destination catalog, /regenerate_suggested_city shows new options or applies
refined answers, and POST /get_tour_plan only plans a destination that was
suggested in the session. See API_CITY_FLOW_DOCS.md.
"""

import json
import re
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

import main
from src.core.destination_catalog import load_discovery_backlog
from intake_fixtures import build_intake, exact_dates, with_restriction

client = TestClient(main.app)

TOUR_PLAN_JSON = json.dumps({
    "tour_plan": [{"day": 1, "activities": [
        {
            "activity_name": "Hot spring soak",
            "activity_description": "Unhurried soak; confirm with the operator",
            "activity_location": "Old town",
            "activity_time": "10:00 AM - 11:30 AM",
            "activity_cost": 20,
        },
        {
            "activity_name": "Garden walk",
            "activity_description": "Quiet walk through the old gardens",
            "activity_location": "Old town",
            "activity_time": "03:00 PM - 04:30 PM",
            "activity_cost": 0,
        },
    ]}],
    "total_cost_estimate": 20,
    "packing_tips": "Layers",
    "travel_tips": "Check official entry guidance",
})


def _feeling_reply(prompt: str, supports: bool = True) -> str:
    destination = re.search(r"DESTINATION: (.+?), [^,\n]+\n", prompt).group(1)
    return json.dumps({
        "intention": "You want time to slow down and hear yourself think.",
        "narrative": f"{destination}'s hot spring soak and garden walk give you unhurried room to think.",
        "supporting_experience_ids": ["E1", "E2"],
        "supports_feeling": supports,
        "mismatch_reason": "" if supports else "Nothing in the plan leaves quiet time.",
    })


def _fake_ai(feeling_supported=lambda prompt: True) -> MagicMock:
    """Answers itinerary prompts with TOUR_PLAN_JSON and feeling prompts with a feeling block."""
    def reply(prompt: str) -> str:
        if "The feeling behind your journey" in prompt:
            return _feeling_reply(prompt, feeling_supported(prompt))
        return TOUR_PLAN_JSON
    return MagicMock(side_effect=reply)


def _suggest(payload: dict):
    return client.post("/get_suggested_city", json=payload)


def _tool(return_value) -> MagicMock:
    tool = MagicMock()
    tool.invoke.return_value = return_value
    return tool


# ==================== /get_suggested_city ====================

def test_get_suggested_city_returns_two_to_three_catalog_destinations():
    response = _suggest(build_intake())
    assert response.status_code == 200
    data = response.json()
    assert data["match_status"] == "matched"
    cities = data["suggested_cities"]
    assert 2 <= len(cities) <= 3
    assert len({city["country_name"] for city in cities}) == len(cities)
    for city in cities:
        assert city["destination_id"]
        assert isinstance(city["match_score"], int)
        assert city["match_reasons"] and city["tradeoffs"] and city["unresolved_facts"]
        assert city["verification"]["candidate_status"] == "CANDIDATE_ONLY"
        assert city["evidence"]["place_lookup"]["outcome"] == "UNVERIFIED"  # offline in tests
    body = data["response"]
    assert body["total_candidate_count"] == 72
    assert body["catalog_version"]["backlog_count"] == 928
    assert body["origin"] == {"status": "NOT_NEEDED"}
    assert body["guest_context"]["trip_goals"][0]["label"] == "Restoration"


def test_old_questionnaire_payload_is_rejected():
    response = _suggest({
        "archetype": "burned_out_achiever",
        "escape_from": "noise_stimulation",
        "budget": {"currency": "USD", "per_person_per_night_max": 500},
    })
    assert response.status_code == 422


def test_no_valid_result_names_the_constraint_instead_of_relaxing_it():
    payload = with_restriction(build_intake(), "mobility_accessibility", "must_avoid")
    response = _suggest(payload)
    assert response.status_code == 200
    data = response.json()
    assert data["match_status"] == "no_valid_result"
    assert data["suggested_cities"] == []
    no_result = data["response"]["no_valid_result"]
    assert no_result["relaxed_automatically"] is False
    assert no_result["blocking_constraints"][0]["field"] == "restriction_severity_mobility_accessibility"


def test_departure_geocode_receives_only_the_departure_place():
    geocode = _tool({"city_name": "Lisbon", "country": "Portugal", "lat": 38.72, "lng": -9.14, "photos": []})
    payload = build_intake(
        travel_distance="nearby",
        recent_feelings=["something_else"],
        recent_feelings_other="PRIVATE-FEELING",
    )
    with patch("src.core.origin.get_cityinfo", geocode):
        response = _suggest(payload)
    assert response.status_code == 200
    geocode.invoke.assert_called_once_with({"city_name": "Lisbon"})
    body = response.json()["response"]
    assert body["origin"]["status"] == "GEOCODED"
    for city in response.json()["suggested_cities"]:
        km = city["distance_check"]["straight_line_km"]
        assert km is None or km <= 1500
    assert "PRIVATE-FEELING" not in response.text


def test_guest_supplied_coordinates_skip_the_geocode():
    geocode = _tool({"error": "should not be called"})
    payload = build_intake(
        travel_distance="nearby",
        departure_latitude=38.72,
        departure_longitude=-9.14,
        departure_country="Portugal",
    )
    with patch("src.core.origin.get_cityinfo", geocode):
        response = _suggest(payload)
    assert response.json()["response"]["origin"]["status"] == "GUEST_SUPPLIED"
    geocode.invoke.assert_not_called()


# ==================== /regenerate_suggested_city ====================

def test_regenerate_never_repeats_until_exhausted_then_404():
    initial = _suggest(build_intake()).json()
    session_id = initial["session_id"]
    seen = {city["destination_id"] for city in initial["suggested_cities"]}
    for _ in range(40):
        response = client.post("/regenerate_suggested_city", json={"session_id": session_id})
        if response.status_code == 404:
            break
        assert response.status_code == 200
        batch = {city["destination_id"] for city in response.json()["suggested_cities"]}
        assert batch and not batch & seen
        seen |= batch
    else:
        raise AssertionError("regeneration never ran out of destinations")
    assert "intake_updates" in response.json()["detail"]


def test_regenerate_with_intake_updates_lets_the_guest_flex_a_constraint():
    blocked = _suggest(with_restriction(build_intake(), "mobility_accessibility", "must_avoid")).json()
    assert blocked["match_status"] == "no_valid_result"
    response = client.post("/regenerate_suggested_city", json={
        "session_id": blocked["session_id"],
        "user_instruction": "It's workable with planning",
        "intake_updates": {"restriction_severity_mobility_accessibility": "prefer_avoid"},
    })
    assert response.status_code == 200
    data = response.json()
    assert data["match_status"] == "matched"
    details = client.get(f"/session/{blocked['session_id']}").json()
    assert details["intake"]["restriction_severity"] == {"mobility_accessibility": "prefer_avoid"}
    assert details["shown_destination_ids"] == [c["destination_id"] for c in data["suggested_cities"]]
    assert details["regeneration_history"][-1]["intake_updated"] is True


def test_regenerate_party_change_does_not_carry_stale_counts():
    initial = _suggest(build_intake(travel_party="family", party_adults=2, party_children=2)).json()
    response = client.post("/regenerate_suggested_city", json={
        "session_id": initial["session_id"],
        "intake_updates": {"travel_party": "solo"},
    })
    assert response.status_code == 200


def test_regenerate_with_invalid_update_is_422_and_keeps_the_session():
    initial = _suggest(build_intake()).json()
    response = client.post("/regenerate_suggested_city", json={
        "session_id": initial["session_id"],
        "intake_updates": {"trip_goals": ["restoration", "growth", "adventure"]},
    })
    assert response.status_code == 422
    details = client.get(f"/session/{initial['session_id']}").json()
    assert details["intake"]["trip_goals"] == ["restoration", "reflection"]


def test_regenerate_unknown_session_is_404():
    response = client.post("/regenerate_suggested_city", json={"session_id": "missing"})
    assert response.status_code == 404


# ==================== /get_tour_plan ====================

def _country_of(args: dict) -> str:
    return args["location_name"].rsplit(",", 1)[1].strip()


def _plan(session_id: str, ai: MagicMock = None, **body):
    # Places are returned inside the destination's own country, as a real search would.
    places = MagicMock()
    places.invoke.side_effect = lambda args: [
        {"name": "Hot spring soak", "place_id": "place-open", "business_status": "OPERATIONAL",
         "address": f"1 Spring Rd, {_country_of(args)}", "photos": []},
        {"name": "Hot spring soak annex", "place_id": "place-closed", "business_status": "CLOSED_PERMANENTLY",
         "address": f"2 Spring Rd, {_country_of(args)}", "photos": []},
        {"name": "Garden walk", "place_id": "place-garden", "business_status": "OPERATIONAL",
         "address": f"5 Garden Rd, {_country_of(args)}", "photos": []},
    ]
    hotels = _tool([{
        "name": "Quiet Stay", "address": "3 Stay Rd", "rating": 4.6,
        "price_level": "PRICE_LEVEL_MODERATE", "photos": [], "coords": None,
    }])
    restaurants = _tool([{"name": "Cafe", "place_id": "r1", "business_status": "OPERATIONAL",
                          "address": "4 Food Rd", "photos": []}])
    ai = ai or _fake_ai()
    with patch("app.router.city_content_route.get_ai_response", ai), \
         patch("app.router.city_content_route.get_detailed_tourist_places", places), \
         patch("app.router.city_content_route.get_google_hotels_sorted_by_rating", hotels), \
         patch("app.router.city_content_route.get_nearby_restaurants", restaurants), \
         patch("app.router.city_content_route.calculate_distance_routes_api", _tool({"error": "skip"})):
        response = client.post("/get_tour_plan", json={"session_id": session_id, **body})
    return response, ai, places, hotels, restaurants


def test_get_tour_plan_builds_on_the_selected_destination_with_estimates_only():
    initial = _suggest(build_intake(**exact_dates(nights=3))).json()
    chosen = initial["suggested_cities"][0]
    response, ai, places, hotels, _ = _plan(initial["session_id"], destination_id=chosen["destination_id"])
    assert response.status_code == 200
    data = response.json()
    assert data["city"] == chosen["city_name"]
    assert data["destination_id"] == chosen["destination_id"]
    assert data["stay"]["price_status"] == "ESTIMATED"
    assert data["stay"]["availability_status"] == "NOT_CHECKED"
    assert data["budget_check"]["status"] == "ESTIMATED"
    assert data["budget_check"]["stay_within_budget"] is True
    activity = next(a for a in data["tour_plan"][0]["activities"] if a["activity_name"] == "Hot spring soak")
    assert activity["place_id"] == "place-open"
    assert "confirm" in activity["availability_note"]
    assert all(a.get("place_id") != "place-closed" for a in data["tour_plan"][0]["activities"])
    location = f"{chosen['city_name']}, {chosen['country_name']}"
    assert places.invoke.call_args.args[0]["location_name"] == location
    assert hotels.invoke.call_args.args[0]["location_name"] == location


def test_itinerary_opens_with_the_feeling_behind_the_journey():
    initial = _suggest(build_intake(trip_goals=["reflection"])).json()
    chosen = initial["suggested_cities"][0]
    response, ai, *_ = _plan(initial["session_id"], destination_id=chosen["destination_id"])
    assert response.status_code == 200
    data = response.json()
    assert list(data)[:4] == ["activity_session_id", "destination_id", "city", "feeling_block"]
    block = data["feeling_block"]
    assert block["headline"] == "THE FEELING: REFLECTIVE"
    assert block["markdown"].startswith("**THE FEELING: REFLECTIVE**")
    assert block["alignment"]["status"] == "aligned"
    names = {a["activity_name"] for day in data["tour_plan"] for a in day["activities"]}
    assert {item["activity_name"] for item in block["supporting_experiences"]} <= names
    assert ai.call_count == 2  # itinerary + feeling block, no revision needed

    details = client.get(f"/activity_session/{data['activity_session_id']}").json()
    assert details["feeling_block"]["headline"] == block["headline"]
    cached, *_ = _plan(initial["session_id"], destination_id=chosen["destination_id"])
    assert cached.json()["feeling_block"]["headline"] == block["headline"]


def test_itinerary_that_does_not_support_the_feeling_is_revised_once():
    initial = _suggest(build_intake(trip_goals=["reflection"])).json()
    chosen = initial["suggested_cities"][0]
    calls = []

    def reply(prompt: str) -> str:
        calls.append(prompt)
        if "The feeling behind your journey" in prompt:
            # The first draft doesn't support the feeling; the revised draft does.
            return _feeling_reply(prompt, supports=sum("The feeling behind your journey" in p for p in calls) > 1)
        return TOUR_PLAN_JSON

    response, *_ = _plan(initial["session_id"], ai=MagicMock(side_effect=reply), destination_id=chosen["destination_id"])
    block = response.json()["feeling_block"]
    assert block["alignment"]["status"] == "revised"
    assert "Nothing in the plan leaves quiet time" in block["alignment"]["detail"]
    revision_prompts = [p for p in calls if "REVISION REQUIRED" in p]
    assert len(revision_prompts) == 1
    assert "Reflective" in revision_prompts[0]


def test_itinerary_that_still_does_not_support_the_feeling_is_flagged():
    initial = _suggest(build_intake(trip_goals=["reflection"])).json()
    chosen = initial["suggested_cities"][0]
    never = _fake_ai(feeling_supported=lambda prompt: False)
    response, *_ = _plan(initial["session_id"], ai=never, destination_id=chosen["destination_id"])
    block = response.json()["feeling_block"]
    assert block["alignment"]["status"] == "mismatch"
    assert "flagged" in block["alignment"]["detail"]
    assert block["narrative"] is None
    assert "Do not present" in block["alignment"]["display_guidance"]


def test_private_free_text_never_reaches_the_llm_or_travel_tools():
    payload = build_intake(
        recent_feelings=["something_else"],
        recent_feelings_other="PRIVATE-FEELING",
        trip_prompt="something_else",
        trip_prompt_other="PRIVATE-REASON",
    )
    initial = _suggest(payload).json()
    chosen = initial["suggested_cities"][0]
    response, ai, places, hotels, restaurants = _plan(initial["session_id"], destination_id=chosen["destination_id"])
    assert response.status_code == 200
    sent = repr(ai.call_args_list) + repr(places.invoke.call_args_list) + repr(hotels.invoke.call_args_list) \
        + repr(restaurants.invoke.call_args_list)
    assert "PRIVATE" not in sent
    assert "PRIVATE" not in repr(client.get(f"/session/{initial['session_id']}").json()["regeneration_history"])


def test_get_tour_plan_accepts_a_suggested_city_by_name():
    initial = _suggest(build_intake()).json()
    chosen = initial["suggested_cities"][0]
    response, *_ = _plan(initial["session_id"], selected_city=chosen["city_name"].upper())
    assert response.status_code == 200
    assert response.json()["destination_id"] == chosen["destination_id"]


def test_get_tour_plan_rejects_destinations_not_suggested_in_the_session():
    initial = _suggest(build_intake()).json()
    shown = {city["destination_id"] for city in initial["suggested_cities"]}
    other = next(d for d in ("NE-1159151609", "NE-1159151503", "US-TUC") if d not in shown)
    response, *_ = _plan(initial["session_id"], destination_id=other)
    assert response.status_code == 400
    response, *_ = _plan(initial["session_id"], selected_city="Atlantis")
    assert response.status_code == 400
    backlog_id = load_discovery_backlog()[0]["destination_id"]
    response, *_ = _plan(initial["session_id"], destination_id=backlog_id)
    assert response.status_code == 404


def test_get_tour_plan_is_cached_per_destination():
    initial = _suggest(build_intake()).json()
    chosen = initial["suggested_cities"][0]["destination_id"]
    first, *_ = _plan(initial["session_id"], destination_id=chosen)
    second, ai, *_ = _plan(initial["session_id"], destination_id=chosen)
    assert second.json()["source"] == "cached"
    assert second.json()["activity_session_id"] == first.json()["activity_session_id"]
    ai.assert_not_called()


def test_stay_over_budget_is_reported_not_hidden():
    initial = _suggest(build_intake(budget_per_night=100)).json()
    chosen = initial["suggested_cities"][0]["destination_id"]
    luxury = _tool([{
        "name": "Grand Palace", "address": "5 Palace Rd", "rating": 4.9,
        "price_level": "PRICE_LEVEL_LUXURY", "photos": [], "coords": None,
    }])
    with patch("app.router.city_content_route.get_ai_response", MagicMock(return_value=TOUR_PLAN_JSON)), \
         patch("app.router.city_content_route.get_detailed_tourist_places", _tool([])), \
         patch("app.router.city_content_route.get_google_hotels_sorted_by_rating", luxury), \
         patch("app.router.city_content_route.get_nearby_restaurants", _tool([])), \
         patch("app.router.city_content_route.calculate_distance_routes_api", _tool({"error": "skip"})):
        response = client.post("/get_tour_plan", json={"session_id": initial["session_id"], "destination_id": chosen})
    budget = response.json()["budget_check"]
    assert budget["stay_within_budget"] is False
    assert "not changed" in budget["note"]
    assert budget["budget_per_night_usd"] == 100
