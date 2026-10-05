"""Geographic coherence: stops, the 60-minute rule, transfers and guest reasons."""

from src.core.guest_text import is_generic_reason
from src.core.itinerary_geo import (
    add_free_time,
    apply_transfer_day,
    describe_meal,
    describe_stay,
    enforce_day,
    is_transfer_day,
    normalize_stops,
    stop_for_day,
    transfer_item,
)
from src.core.travel_time import SOURCE_ESTIMATE, SOURCE_ROUTES, TravelTimes

# Real coordinates from the client's Vancouver Island example.
TOFINO = (49.1530, -125.9066)
TOFINO_BEACH = (49.1268, -125.8890)    # Chesterman Beach, ~3 km away
TOFINO_HARBOUR = (49.1544, -125.9095)
PORT_ALBERNI = (49.2339, -124.8055)    # ~80 km straight line, ~2 hr by road
VICTORIA = (48.4284, -123.3656)        # ~200 km, a different region
COWICHAN_BAY = (48.7406, -123.6206)


def _offline() -> TravelTimes:
    """Routing unavailable: every leg is a labelled straight-line estimate."""
    return TravelTimes(matrix_fn=lambda origins, destinations: {"error": "offline"})


def _item(name: str, point, time: str, kind: str = "experience") -> dict:
    return {
        "item_type": kind, "activity_name": name, "activity_time": time,
        "latitude": point[0] if point else None, "longitude": point[1] if point else None,
    }


def _stop(point=TOFINO, number: int = 1, area: str = "Tofino") -> dict:
    return {"stop": number, "base_area": area, "nights": 3, "first_day": 1, "last_day": 3,
            "base": {"latitude": point[0], "longitude": point[1]}, "hotel": {"rating": 4.6}}


# ==================== stops ====================

def test_short_trips_always_have_one_base():
    stops = normalize_stops([{"base_area": "Tofino", "nights": 2}, {"base_area": "Victoria", "nights": 1}], 3, "Vancouver Island")
    assert [stop["base_area"] for stop in stops] == ["Tofino"]
    assert (stops[0]["first_day"], stops[0]["last_day"], stops[0]["nights"]) == (1, 3, 3)


def test_longer_trips_may_split_into_consecutive_stops():
    stops = normalize_stops([{"base_area": "Tofino", "nights": 3}, {"base_area": "Victoria", "nights": 2}], 5, "Vancouver Island")
    assert [(stop["base_area"], stop["first_day"], stop["last_day"]) for stop in stops] == [
        ("Tofino", 1, 3), ("Victoria", 4, 5),
    ]
    assert stop_for_day(stops, 4)["base_area"] == "Victoria"
    assert is_transfer_day(stops, 4) and not is_transfer_day(stops, 1)


def test_one_night_stops_are_folded_and_nights_add_up():
    stops = normalize_stops(
        [{"base_area": "Tofino", "nights": 3}, {"base_area": "Ucluelet", "nights": 1}, {"base_area": "Victoria", "nights": 2}],
        6, "Vancouver Island",
    )
    assert [(stop["base_area"], stop["nights"]) for stop in stops] == [("Tofino", 4), ("Victoria", 2)]
    assert sum(stop["nights"] for stop in stops) == 6


def test_unusable_stops_fall_back_to_the_destination():
    assert normalize_stops(None, 4, "Ubud")[0]["base_area"] == "Ubud"
    assert normalize_stops([{"base_area": "", "nights": 4}, "bad"], 4, "Ubud")[0]["base_area"] == "Ubud"


# ==================== the 60-minute rule ====================

def test_vancouver_island_activities_far_from_the_tofino_stay_are_removed():
    day = {"day": 1, "activities": [
        _item("Chesterman Beach walk", TOFINO_BEACH, "09:00 AM - 11:00 AM"),
        _item("Cathedral Grove near Port Alberni", PORT_ALBERNI, "01:00 PM - 03:00 PM"),
        _item("Dinner in Victoria", VICTORIA, "07:00 PM - 08:30 PM", kind="meal"),
    ]}
    adjustments = enforce_day(day, _stop(), _offline(), replace=lambda item, base, previous: None)
    assert [item["activity_name"] for item in day["activities"]] == ["Chesterman Beach walk"]
    assert {adjustment["item"] for adjustment in adjustments} == {"Cathedral Grove near Port Alberni", "Dinner in Victoria"}
    assert all(adjustment["type"] == "removed" for adjustment in adjustments)
    assert "from your hotel in Tofino" in adjustments[0]["guest_note"]
    kept = day["activities"][0]
    assert kept["travel_minutes_from_base"] <= 60
    assert kept["travel_from"] == "your hotel"
    assert kept["travel_time_source"] == SOURCE_ESTIMATE


def test_far_item_is_swapped_for_a_closer_alternative():
    day = {"day": 2, "activities": [_item("Cowichan Bay food walk", COWICHAN_BAY, "10:00 AM - 12:00 PM")]}
    closer = _item("Tofino harbour walk", TOFINO_HARBOUR, "10:00 AM - 12:00 PM")
    adjustments = enforce_day(day, _stop(), _offline(), replace=lambda item, base, previous: dict(closer))
    assert day["activities"][0]["activity_name"] == "Tofino harbour walk"
    assert adjustments[0]["type"] == "replaced"
    assert "swapped Cowichan Bay food walk for Tofino harbour walk" in adjustments[0]["guest_note"]


def test_each_leg_is_also_checked_from_the_previous_item():
    near_base, then_far = (49.20, -125.90), (49.75, -125.90)  # second item ~60 km from the first
    day = {"day": 1, "activities": [_item("A", near_base, "09:00 AM - 10:00 AM"), _item("B", then_far, "11:00 AM - 12:00 PM")]}
    enforce_day(day, _stop(), _offline(), replace=lambda item, base, previous: None)
    assert [item["activity_name"] for item in day["activities"]] == ["A"]


def test_measured_route_times_are_used_and_cached():
    calls = []

    def matrix(origins, destinations):
        calls.append((list(origins), list(destinations)))
        return {"elements": [
            {"origin": o, "destination": d, "minutes": 12, "km": 6.0, "route_found": True}
            for o in range(len(origins)) for d in range(len(destinations))
        ]}

    travel = TravelTimes(matrix_fn=matrix)
    day = {"day": 1, "activities": [_item("Beach", TOFINO_BEACH, "09:00 AM - 10:00 AM"),
                                    _item("Harbour", TOFINO_HARBOUR, "11:00 AM - 12:00 PM")]}
    enforce_day(day, _stop(), travel, replace=lambda item, base, previous: None)
    assert len(calls) == 1, "one batched matrix call per day"
    assert all(item["travel_time_source"] == SOURCE_ROUTES for item in day["activities"])
    assert day["activities"][1]["travel_from"] == "Beach"
    assert day["activities"][1]["return_to_base_km"] == 6.0


def test_no_drivable_route_is_never_within_the_limit():
    travel = TravelTimes(matrix_fn=lambda origins, destinations: {"elements": [
        {"origin": 0, "destination": 0, "minutes": None, "km": None, "route_found": False},
    ]})
    leg = travel.leg(TOFINO, TOFINO_BEACH)
    assert leg.minutes is None and not leg.within(60)


def test_unlocated_items_are_kept_but_not_timed():
    day = {"day": 1, "activities": [_item("Mystery stop", None, "09:00 AM - 10:00 AM")]}
    enforce_day(day, _stop(), _offline(), replace=lambda item, base, previous: None)
    assert day["activities"][0]["travel_minutes_from_base"] is None


# ==================== transfers ====================

def test_transfer_between_stops_has_buffers_and_ferry_note():
    tofino = _stop()
    victoria = {**_stop(VICTORIA, 2, "Victoria"), "first_day": 4, "last_day": 5}
    transfer = transfer_item(tofino, victoria, lambda origin, destination: {"minutes": 270, "km": 310.0, "includes_ferry": True})
    assert transfer["item_type"] == "transfer"
    assert transfer["transfer_minutes"] == 270
    assert transfer["transfer_buffer_minutes"] == 40 + 45  # 15% road buffer + ferry buffer
    assert "ferry crossing" in transfer["activity_description"]
    assert not is_generic_reason(transfer["why_selected"])


def test_transfer_falls_back_to_an_estimate_when_routing_fails():
    transfer = transfer_item(_stop(), _stop(VICTORIA, 2, "Victoria"), lambda origin, destination: {"error": "offline"})
    assert transfer["travel_time_source"] == SOURCE_ESTIMATE
    assert transfer["transfer_minutes"] > 60


def test_transfer_day_is_light_and_never_overlaps_the_drive():
    transfer = transfer_item(_stop(), _stop(VICTORIA, 2, "Victoria"), lambda o, d: {"minutes": 240, "km": 300.0})
    day = {"day": 4, "activities": [
        _item("Morning museum", VICTORIA, "10:00 AM - 11:00 AM"),        # during the drive
        _item("Inner harbour stroll", VICTORIA, "04:30 PM - 05:30 PM"),
        _item("Evening garden visit", VICTORIA, "06:00 PM - 07:00 PM"),  # second experience: too much
        _item("Dinner", VICTORIA, "07:30 PM - 08:30 PM", kind="meal"),
    ]}
    adjustments = apply_transfer_day(day, transfer)
    names = [item["activity_name"] for item in day["activities"]]
    assert names == [transfer["activity_name"], "Inner harbour stroll", "Dinner"]  # chronological
    assert day["day_type"] == "transfer"
    assert {adjustment["item"] for adjustment in adjustments} == {"Morning museum", "Evening garden visit"}


def test_breakfast_before_checkout_stays_at_the_previous_base():
    transfer = transfer_item(_stop(), _stop(VICTORIA, 2, "Victoria"), lambda o, d: {"minutes": 240, "km": 300.0})
    breakfast = {**_item("Breakfast at Victoria Cafe", VICTORIA, "08:00 AM - 09:00 AM", kind="meal"),
                 "meal": "Breakfast", "place_id": "victoria-cafe", "restaurant_name": "Victoria Cafe"}
    day = {"day": 4, "activities": [breakfast]}
    apply_transfer_day(day, transfer)
    first = day["activities"][0]
    assert first["activity_name"] == "Breakfast before you leave Tofino"
    assert first["open_slot"] is True and first["latitude"] is None and first["place_id"] is None
    assert not is_generic_reason(first["why_selected"])


# ==================== guest reasons ====================

def test_stay_reason_is_specific_to_the_plan():
    stop = _stop()
    days = [{"day": 1, "activities": [
        {"item_type": "experience", "travel_minutes_from_base": 10},
        {"item_type": "experience", "travel_minutes_from_base": 20},
    ]}]
    reason = describe_stay(stop, days)
    assert reason == (
        "Your base for 3 nights in Tofino, rated 4.6 on Google. "
        "All 2 planned experiences are within an hour of it, about 15 minutes away on average."
    )
    assert not is_generic_reason(reason)


def test_meal_reason_uses_rating_travel_time_and_dietary_needs():
    meal = {"restaurant_name": "Wolf in the Fog", "rating": 4.6, "travel_minutes_from_previous": 7,
            "travel_from": "Chesterman Beach walk"}
    reason = describe_meal(meal, "Lunch", "Harbour walk", ["vegetarian"])
    assert reason.startswith("Wolf in the Fog is rated 4.6 on Google and about 7 minutes from Chesterman Beach walk")
    assert "before Harbour walk" in reason and "vegetarian" in reason
    assert "convenient" not in reason and "near the" not in reason
    assert not is_generic_reason(reason)


def test_long_gaps_become_intentional_free_time():
    day = {"day": 1, "activities": [
        _item("Morning surf lesson", TOFINO_BEACH, "09:00 AM - 11:00 AM"),
        _item("Sunset walk", TOFINO_BEACH, "05:00 PM - 06:00 PM"),
    ]}
    add_free_time(day, "mostly open time")
    free = [item for item in day["activities"] if item["item_type"] == "free_time"]
    assert len(free) == 1
    assert free[0]["activity_time"] == "11:00 AM - 05:00 PM"
    assert "after Morning surf lesson" in free[0]["activity_description"]
