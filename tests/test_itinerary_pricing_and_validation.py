"""Price breakdown from visible items, final validation, booking status and guest wording."""

from src.core.guest_text import clean, has_internal_wording, is_generic_reason, price_indication
from src.core.itinerary_pricing import build_price_breakdown, meal_cost
from src.core.itinerary_validation import NOT_READY_LABEL, booking_status, validate_itinerary
from src.core.travel_time import SOURCE_ESTIMATE, SOURCE_ROUTES

GOOD_REASON = "A quiet ridge walk that suits your unhurried mornings together."


def _stop(**overrides) -> dict:
    stop = {
        "stop": 1, "base_area": "Tofino", "nights": 3, "first_day": 1, "last_day": 3,
        "base": {"latitude": 49.15, "longitude": -125.90}, "stay_found": True, "nightly_usd": 225.0,
        "hotel": {"name": "Pacific Sands", "price_status": "ESTIMATED"},
    }
    stop.update(overrides)
    return stop


def _experience(name: str = "Chesterman Beach walk", cost: float = 20, minutes: int = 10, **overrides) -> dict:
    item = {
        "item_type": "experience", "activity_name": name, "activity_description": "Walk the beach at low tide.",
        "why_selected": GOOD_REASON, "activity_cost": cost, "latitude": 49.12, "longitude": -125.88,
        "travel_minutes_from_base": minutes, "travel_minutes_from_previous": minutes,
        "distance_from_previous_km": 5.0, "travel_time_source": SOURCE_ROUTES, "place_id": "p1",
    }
    item.update(overrides)
    return item


def _meal(cost: float = 30, **overrides) -> dict:
    item = _experience("Lunch at Wolf in the Fog", cost, item_type="meal", meal="Lunch", place_id="r1")
    item.update(overrides)
    return item


# ==================== pricing ====================

def test_total_is_exactly_the_sum_of_the_visible_lines():
    days = [{"day": 1, "activities": [_experience(cost=20), _meal(cost=30), _experience("Harbour walk", cost=0)]}]
    breakdown = build_price_breakdown([_stop()], days, party_size=2, rooms=1, nights=3)
    lines = {line["category"]: line for line in breakdown["lines"]}
    assert lines["accommodation"]["amount"] == 675.0  # $225 x 3 nights x 1 room
    assert lines["accommodation"]["details"][0]["nightly_rate"] == 225.0
    assert lines["experiences"]["amount"] == 40.0 and lines["experiences"]["per_person"] == 20.0
    assert lines["dining"]["amount"] == 60.0 and lines["dining"]["per_person"] == 30.0
    assert lines["local_transportation"]["amount"] == round(15.0 * 0.9, 2)
    assert lines["taxes_fees_gratuities"]["amount"] is None
    required = [line["amount"] for category, line in lines.items() if category != "taxes_fees_gratuities"]
    assert breakdown["total"] == round(sum(required), 2)
    assert breakdown["applies_to"] == "Full trip for 2 guests in 1 room, 3 nights"
    assert breakdown["status"] == "ESTIMATED"


def test_every_line_says_who_it_applies_to():
    days = [{"day": 1, "activities": [_experience(), _meal()]}]
    for line in build_price_breakdown([_stop()], days, 2, 1, 3)["lines"]:
        assert line["basis"], line["category"]


def test_total_is_withheld_when_a_stay_has_no_real_price():
    breakdown = build_price_breakdown([_stop(nightly_usd=None)], [{"day": 1, "activities": [_experience()]}], 2, 1, 3)
    assert breakdown["total"] is None
    assert "no nightly price for Pacific Sands" in breakdown["total_withheld_reason"]


def test_transfers_are_priced_for_the_whole_party():
    transfer = {"item_type": "transfer", "transfer_km": 300.0, "activity_cost": 0}
    breakdown = build_price_breakdown([_stop()], [{"day": 4, "activities": [transfer]}], 4, 2, 3)
    transport = next(line for line in breakdown["lines"] if line["category"] == "local_transportation")
    assert transport["amount"] == 270.0
    assert "whole party" in transport["basis"]


def test_dining_estimates_follow_the_restaurant_price_level():
    assert meal_cost("Dinner") == 55.0
    assert meal_cost("Dinner", "PRICE_LEVEL_INEXPENSIVE") < meal_cost("Dinner") < meal_cost("Dinner", "PRICE_LEVEL_EXPENSIVE")


# ==================== validation ====================

def _validate(stops, days):
    return validate_itinerary(stops, days, build_price_breakdown(stops, days, 2, 1, 3))


def test_a_coherent_itinerary_passes():
    result = _validate([_stop()], [{"day": 1, "activities": [_experience(), _meal()]}])
    assert result["status"] == "passed", result["issues"]
    assert result["display_ready"] is True


def test_a_leg_over_the_limit_fails_validation():
    result = _validate([_stop()], [{"day": 1, "activities": [_experience(minutes=95)]}])
    assert result["status"] == "failed" and result["display_ready"] is False
    assert {issue["code"] for issue in result["issues"]} >= {"LEG_OVER_LIMIT", "STAY_NOT_CENTRAL"}


def test_missing_stay_base_and_transfer_are_errors():
    second = _stop(stop=2, base_area="Victoria", first_day=4, last_day=5, stay_found=False,
                   base={"latitude": None, "longitude": None})
    result = _validate([_stop(), second], [{"day": 4, "activities": [_experience()]}])
    codes = {issue["code"] for issue in result["issues"]}
    assert {"STAY_NOT_FOUND", "STOP_BASE_UNLOCATED", "TRANSFER_MISSING"} <= codes


def test_generic_reasons_unlocated_items_and_estimates_are_flagged():
    days = [{"day": 1, "activities": [
        _experience(why_selected="A convenient stop near the day's planned route."),
        _experience("Unknown cove", latitude=None, longitude=None, place_id=None),
        _experience("Harbour walk", travel_time_source=SOURCE_ESTIMATE),
    ]}]
    codes = {issue["code"] for issue in _validate([_stop()], days)["issues"]}
    assert {"REASON_MISSING", "ITEM_UNLOCATED", "TRAVEL_TIME_ESTIMATED"} <= codes


def test_backend_wording_in_guest_text_is_an_error():
    days = [{"day": 1, "activities": [_experience(activity_description="Our editors tag this for Coast|Forest.")]}]
    result = _validate([_stop()], days)
    assert any(issue["code"] == "INTERNAL_WORDING" for issue in result["issues"])


def test_google_place_names_with_pipes_are_not_internal_wording():
    days = [{"day": 1, "activities": [_experience("Shelter | Restaurant & Bar")]}]
    assert _validate([_stop()], days)["status"] == "passed"


# ==================== booking status ====================

def test_nothing_unverified_is_ever_ready_to_book():
    stops, days = [_stop()], [{"day": 1, "activities": [_experience(), _meal()]}]
    validation = _validate(stops, days)
    status = booking_status(validation, stops, days, destination_verified=False)
    assert status["ready_to_book"] is False
    assert status["guest_label"] == NOT_READY_LABEL
    assert any("booking provider" in reason for reason in status["reasons"])


def test_ready_to_book_only_when_everything_is_verified():
    stops = [_stop(hotel={"name": "Pacific Sands", "price_status": "LIVE"})]
    days = [{"day": 1, "activities": [_experience(availability_status="CONFIRMED"), _meal()]}]
    status = booking_status(_validate(stops, days), stops, days, destination_verified=True)
    assert status == {"ready_to_book": True, "guest_label": "Ready to book", "reasons": []}


# ==================== guest wording ====================

def test_price_levels_become_guest_labels():
    assert price_indication("PRICE_LEVEL_MODERATE") == "$$ · Moderate"
    assert price_indication("PRICE_LEVEL_LUXURY (approximately)") == "$$$$ · Luxury"
    assert price_indication("NOT_AVAILABLE") is None


def test_backend_markers_are_cleaned_from_guest_text():
    assert clean("$225 per night (approximately)") == "$225 per night"
    assert clean("Price: PRICE_LEVEL_MODERATE.") == "Price: $$ · Moderate."
    assert not has_internal_wording(clean("Rated PRICE_LEVEL_EXPENSIVE"))


def test_generic_reasons_are_detected():
    assert is_generic_reason("A convenient lunch stop near the day's planned route.")
    assert is_generic_reason("Nice place.")
    assert is_generic_reason("")
    assert not is_generic_reason(
        "A relaxed waterfront lunch that gives you time to slow down after the morning's adventure."
    )
