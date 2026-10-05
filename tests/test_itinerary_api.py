"""
End-to-end itinerary validation through the API: two bases with a transfer,
the 60-minute rule with a closer replacement, price breakdown from the visible
items, final validation, booking status and guest-safe wording.
"""

import json
import re
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

import main
from src.core.itinerary_validation import NOT_READY_LABEL
from src.core.guest_text import is_generic_reason
from src.core.travel_time import estimate_leg
from intake_fixtures import build_intake, exact_dates

client = TestClient(main.app)

NORTH = (10.00, 20.00)   # first base
SOUTH = (9.00, 20.00)    # second base, ~111 km away
PLACES = {
    "Cliff walk": (10.02, 20.01),
    "Far canyon hike": (11.30, 20.00),   # ~145 km from the north base: must not stay in the plan
    "Quiet garden": (10.01, 20.02),      # a closer alternative from the same search
    "Tide pools": (10.03, 20.00),
    "Forest bath": (10.00, 20.03),
    "Harbour stroll": (9.01, 20.01),
    "Lighthouse visit": (9.02, 20.02),
    "Sea kayak": (9.02, 20.00),
    "Beach picnic": (9.01, 20.00),
}
REASON = "Gives you unhurried time outdoors, which suits your wish to slow down and reflect."


def _activity(name: str, time: str) -> dict:
    return {"activity_name": name, "activity_description": f"Time at {name}.", "activity_location": "Coast",
            "activity_time": time, "activity_cost": 25, "why_selected": REASON}


PLAN = {
    "stops": [{"base_area": "North Bay", "nights": 3}, {"base_area": "South Point", "nights": 2}],
    "tour_plan": [
        {"day": 1, "activities": [_activity("Cliff walk", "09:30 AM - 11:00 AM"),
                                  _activity("Far canyon hike", "02:00 PM - 05:00 PM")]},
        {"day": 2, "activities": [_activity("Tide pools", "10:00 AM - 11:30 AM")]},
        {"day": 3, "activities": [_activity("Forest bath", "10:00 AM - 11:30 AM")]},
        {"day": 4, "activities": [_activity("Harbour stroll", "04:30 PM - 05:30 PM"),
                                  _activity("Lighthouse visit", "06:00 PM - 06:45 PM")]},
        {"day": 5, "activities": [_activity("Sea kayak", "10:00 AM - 12:00 PM")]},
    ],
    "packing_tips": "Layers (approximately)",
    "travel_tips": "Check official entry guidance",
}


def _feeling(prompt: str) -> str:
    destination = re.search(r"DESTINATION: (.+?), [^,\n]+\n", prompt).group(1)
    return json.dumps({
        "intention": "You want space to slow down.",
        "narrative": f"{destination}'s cliff walk and tide pools give you unhurried room to breathe.",
        "supporting_experience_ids": ["E1", "E2"], "supports_feeling": True, "mismatch_reason": "",
    })


def _ai(plan: dict) -> MagicMock:
    return MagicMock(side_effect=lambda prompt: _feeling(prompt) if "The feeling behind your journey" in prompt
                     else json.dumps(plan))


def _country(args: dict) -> str:
    return args["location_name"].rsplit(",", 1)[1].strip()


def _places(args: dict) -> list:
    return [
        {"name": name, "place_id": f"p-{name}", "business_status": "OPERATIONAL",
         "address": f"{name} Rd, {_country(args)}", "photos": [], "coords": {"lat": lat, "lng": lng}}
        for name, (lat, lng) in PLACES.items()
    ]


def _hotels(args: dict) -> list:
    north = "North Bay" in args["location_name"]
    point = NORTH if north else SOUTH
    return [{"name": "Cliff House" if north else "Point Lodge", "address": f"1 Stay Rd, {_country(args)}",
             "rating": 4.6, "price_level": "PRICE_LEVEL_MODERATE", "photos": [],
             "coords": {"lat": point[0], "lng": point[1]}}]


def _restaurants() -> MagicMock:
    counter = iter(range(1, 100))

    def search(args: dict) -> list:
        number = next(counter)
        lat, lng = args.get("latitude", NORTH[0]), args.get("longitude", NORTH[1])
        return [{"name": f"Harbour Table {number}", "place_id": f"r{number}", "business_status": "OPERATIONAL",
                 "address": f"{number} Food Rd, {_country(args)}", "photos": [], "rating": 4.5,
                 "price_level": "PRICE_LEVEL_MODERATE", "coords": {"lat": lat + 0.004, "lng": lng}}]
    tool = MagicMock()
    tool.invoke.side_effect = search
    return tool


def _matrix(origins, destinations) -> dict:
    """Measured drive times, shaped like the Routes matrix response."""
    return {"elements": [
        {"origin": o, "destination": d, "minutes": leg.minutes, "km": leg.km, "route_found": True}
        for o, origin in enumerate(origins) for d, destination in enumerate(destinations)
        for leg in [estimate_leg(origin, destination)]
    ]}


def _tool(side_effect) -> MagicMock:
    tool = MagicMock()
    tool.invoke.side_effect = side_effect
    return tool


def _patched(ai: MagicMock):
    return [
        patch("app.router.city_content_route.get_ai_response", ai),
        patch("app.router.city_content_route.get_detailed_tourist_places", _tool(_places)),
        patch("app.router.city_content_route.get_google_hotels_sorted_by_rating", _tool(_hotels)),
        patch("app.router.city_content_route.get_nearby_restaurants", _restaurants()),
        patch("src.core.travel_time.compute_route_matrix", _matrix),
        patch("app.router.city_content_route.compute_drive_route",
              lambda origin, destination: {"minutes": 150, "km": 120.0, "includes_ferry": True}),
    ]


def _post(path: str, body: dict, ai: MagicMock):
    patches = _patched(ai)
    for item in patches:
        item.start()
    try:
        return client.post(path, json=body)
    finally:
        for item in patches:
            item.stop()


def _new_itinerary():
    initial = client.post("/get_suggested_city", json=build_intake(**exact_dates(nights=5))).json()
    chosen = initial["suggested_cities"][0]
    response = _post("/get_tour_plan", {"session_id": initial["session_id"], "destination_id": chosen["destination_id"]},
                     _ai(PLAN))
    assert response.status_code == 200, response.text
    return response.json()


def _items(data: dict) -> list:
    return [item for day in data["tour_plan"] for item in day["activities"]]


def test_two_bases_with_a_transfer_and_a_light_travel_day():
    data = _new_itinerary()
    assert [(stop["base_area"], stop["nights"], stop["first_day"]) for stop in data["stops"]] == [
        ("North Bay", 3, 1), ("South Point", 2, 4),
    ]
    assert [stop["stay"]["name"] for stop in data["stops"]] == ["Cliff House", "Point Lodge"]
    assert data["stay"]["name"] == "Cliff House"
    day4 = data["tour_plan"][3]
    assert day4["day_type"] == "transfer" and day4["stop"] == 2
    kinds = [item["item_type"] for item in day4["activities"]]
    transfer = day4["activities"][kinds.index("transfer")]
    assert transfer["includes_ferry"] is True
    assert "experience" not in kinds[:kinds.index("transfer")], "nothing is planned before the move except breakfast"
    assert not any(item.get("meal") == "Lunch" for item in day4["activities"]), "lunch falls during the drive"
    experiences = [item["activity_name"] for item in day4["activities"] if item["item_type"] == "experience"]
    assert experiences == ["Harbour stroll"], "one light experience on the travel day"
    assert any("ferry" in note for note in data["guest_notes"])


def test_far_activity_is_replaced_and_every_leg_is_within_an_hour():
    data = _new_itinerary()
    names = {item["activity_name"] for item in _items(data)}
    assert "Far canyon hike" not in names and "Quiet garden" in names
    replaced = next(adjustment for adjustment in data["adjustments"] if adjustment["item"] == "Far canyon hike")
    assert replaced["type"] == "replaced" and replaced["replacement"] == "Quiet garden"
    assert any("swapped Far canyon hike for Quiet garden" in note for note in data["guest_notes"])
    for item in _items(data):
        if item["item_type"] in ("experience", "meal") and not item["open_slot"]:
            assert item["travel_minutes_from_base"] <= 60 and item["travel_minutes_from_previous"] <= 60
            assert item["travel_time_source"] == "google_routes"
    # The transfer-day breakfast is at the hotel being left, not near the new base.
    assert data["tour_plan"][3]["activities"][0]["activity_name"] == "Breakfast before you leave North Bay"


def test_every_recommendation_has_a_specific_reason():
    data = _new_itinerary()
    for item in _items(data):
        if item["item_type"] in ("experience", "meal"):
            assert not is_generic_reason(item["why_selected"]), item
    for stop in data["stops"]:
        assert stop["stay"]["why_selected"].startswith(f"Your base for {stop['nights']} nights in {stop['base_area']}")


def test_price_breakdown_is_the_sum_of_the_visible_items():
    data = _new_itinerary()
    breakdown = data["price_breakdown"]
    lines = {line["category"]: line for line in breakdown["lines"]}
    assert [detail["nights"] for detail in lines["accommodation"]["details"]] == [3, 2]
    visible_experiences = sum(item["activity_cost"] for item in _items(data) if item["item_type"] == "experience")
    visible_dining = sum(item["activity_cost"] for item in _items(data) if item["item_type"] == "meal")
    assert lines["experiences"]["per_person"] == visible_experiences
    assert lines["dining"]["per_person"] == round(visible_dining, 2)
    required = [line["amount"] for category, line in lines.items() if category != "taxes_fees_gratuities"]
    assert breakdown["total"] == round(sum(required), 2) == data["total_cost_estimate"]
    assert breakdown["applies_to"] == "Full trip for 1 guest in 1 room, 5 nights"


def test_validation_passes_but_nothing_is_presented_as_bookable():
    data = _new_itinerary()
    assert data["validation"]["status"] == "passed", data["validation"]["issues"]
    assert data["validation"]["display_ready"] is True
    assert data["booking_status"]["ready_to_book"] is False
    assert data["booking_status"]["guest_label"] == NOT_READY_LABEL


def test_guest_facing_output_has_no_backend_values():
    data = _new_itinerary()
    guest_view = json.dumps({key: data[key] for key in ("stay", "stops", "tour_plan", "guest_notes", "packing_tips")})
    assert "PRICE_LEVEL" not in guest_view
    assert "approximately" not in guest_view
    assert "convenient" not in guest_view
    assert data["stops"][0]["stay"]["price_level"] == "$$ · Moderate"
    assert data["stops"][0]["stay"]["facilities"] == []
    block = data["feeling_block"]
    assert block["markdown"].startswith("Designed to help you feel: **Restored**\n")


def test_feeling_block_is_written_from_experiences_only():
    prompts = []
    ai = _ai(PLAN)
    original = ai.side_effect
    ai.side_effect = lambda prompt: (prompts.append(prompt), original(prompt))[1]
    initial = client.post("/get_suggested_city", json=build_intake(**exact_dates(nights=5))).json()
    _post("/get_tour_plan", {"session_id": initial["session_id"],
                             "destination_id": initial["suggested_cities"][0]["destination_id"]}, ai)
    feeling_prompt = next(prompt for prompt in prompts if "The feeling behind your journey" in prompt)
    listed = feeling_prompt.split("EXPERIENCES IN THE ITINERARY")[1].split("WRITE:")[0]
    assert "Cliff walk" in listed
    assert "Transfer from" not in listed and "Open time" not in listed and "Lunch at" not in listed


def test_viator_products_price_the_experiences_when_configured(monkeypatch):
    monkeypatch.setattr("src.config.config_env.settings.viator_api_key", "test-key")
    kayak = {"productCode": "K1", "title": "Sea kayak Tide pools tour", "productUrl": "https://www.viator.com/k1",
             "reviews": {"totalReviews": 80, "combinedAverageRating": 4.8},
             "pricing": {"summary": {"fromPrice": 95.0}, "currency": "USD"}}
    searches = []

    def search(term, destination_id, start, end):
        searches.append((term, destination_id, start))
        return {"products": [kayak]}

    monkeypatch.setattr("src.tools.viator.get_destinations", lambda: [
        {"destination_id": 7, "name": "North Bay", "type": "CITY", "latitude": NORTH[0], "longitude": NORTH[1]},
        {"destination_id": 8, "name": "South Point", "type": "CITY", "latitude": SOUTH[0], "longitude": SOUTH[1]},
    ])
    monkeypatch.setattr("src.tools.viator.search_products", search)
    monkeypatch.setattr("src.tools.viator.availability_schedule", lambda code: {"error": "no schedule"})
    data = _new_itinerary()
    tide_pools = next(item for item in _items(data) if item["activity_name"] == "Tide pools")
    assert tide_pools["viator"]["product_code"] == "K1"
    assert tide_pools["activity_cost"] == 95.0 and tide_pools["price_source"] == "viator_from_price"
    assert {destination for _, destination, _ in searches} == {7, 8}, "each stop searches its own Viator destination"
    assert all(start for _, _, start in searches), "exact dates are passed to the search"
    experiences = next(line for line in data["price_breakdown"]["lines"] if line["category"] == "experiences")
    assert "priced from Viator" in experiences["basis"]


def test_saved_itinerary_reopens_with_the_same_stops_and_checks():
    data = _new_itinerary()
    saved = client.get(f"/activity_session/{data['activity_session_id']}").json()
    assert saved["stops"] == data["stops"]
    assert saved["price_breakdown"] == data["price_breakdown"]
    assert saved["validation"]["status"] == data["validation"]["status"]
    assert saved["parent_session_id"]


def test_regenerating_one_day_keeps_the_stays_and_revalidates():
    data = _new_itinerary()
    new_day = {"tour_plan": [{"day": 5, "activities": [_activity("Beach picnic", "11:00 AM - 12:00 PM")]}]}
    response = _post("/regenerate_tour_plan", {
        "activity_session_id": data["activity_session_id"], "day_to_regenerate": 5, "user_instruction": "slower",
    }, _ai(new_day))
    assert response.status_code == 200, response.text
    updated = response.json()
    assert [stop["stay"]["name"] for stop in updated["stops"]] == ["Cliff House", "Point Lodge"]
    day5 = [item["activity_name"] for item in updated["tour_plan"][4]["activities"] if item["item_type"] == "experience"]
    assert day5 == ["Beach picnic"]
    assert updated["tour_plan"][0] == data["tour_plan"][0], "other days are unchanged"
    assert updated["validation"]["status"] == "passed"
    assert updated["price_breakdown"]["total"] == updated["total_cost_estimate"]
