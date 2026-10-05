"""POST /recommend_travel_dates: specific, explained check-in/check-out dates for flexible timing."""

import json
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import main
from src.core.date_recommendation import AVAILABILITY_NOTE, peak_days
from intake_fixtures import build_intake

client = TestClient(main.app)

VANCOUVER = dict(departure_location="Vancouver", departure_latitude=49.28, departure_longitude=-123.12,
                 departure_country="CA", travel_distance="nearby")
SYDNEY = dict(departure_location="Sydney", departure_latitude=-33.87, departure_longitude=151.21,
              departure_country="AU", travel_distance="nearby")
# Recorded-climate shape for a temperate northern coast: dry, warm July-August.
CLIMATE = {"source": "test", "period": "2021-2025", "months": {
    month: {"avg_high_c": high, "avg_low_c": high - 6, "rainy_days": rain}
    for month, high, rain in [(1, 8, 20), (2, 8, 17), (3, 9, 20), (4, 11, 16), (5, 14, 14), (6, 17, 11),
                              (7, 25, 3), (8, 25, 4), (9, 18, 11), (10, 14, 17), (11, 10, 19), (12, 8, 22)]
}}


def _body(**overrides) -> dict:
    body = build_intake(**{"travel_timing": "flexible", "trip_nights": 4, **VANCOUVER, **overrides})
    body.pop("budget_per_night")
    return body


def _recommend(body: dict, ai=None, climate=None):
    ai = ai or MagicMock(side_effect=Exception("model unavailable"))
    with patch("app.router.dates_route.get_ai_response", ai), \
         patch("app.router.dates_route.monthly_climate", lambda lat, lng: climate or {"error": "offline"}):
        return client.post("/recommend_travel_dates", json=body)


def _all_options(data: dict) -> list:
    return [data["recommended"], *data["alternatives"]]


# ==================== request rules ====================

def test_budget_is_not_needed_yet_but_exact_dates_are_rejected():
    assert _recommend(_body()).status_code == 200
    exact = _body(travel_timing="exact_dates", check_in_date=(date.today() + timedelta(days=30)).isoformat(),
                  check_out_date=(date.today() + timedelta(days=34)).isoformat())
    assert _recommend(exact).status_code == 422


def test_a_range_shorter_than_the_trip_is_a_clear_error():
    start = date.today() + timedelta(days=40)
    response = _recommend(_body(trip_nights=6, earliest_check_in=start.isoformat(),
                                latest_check_out=(start + timedelta(days=3)).isoformat()))
    assert response.status_code == 422
    assert "shorter than 6 nights" in response.json()["detail"]


# ==================== dates ====================

def test_dates_match_the_nights_and_stay_inside_the_window():
    data = _recommend(_body(trip_nights=5)).json()
    window = data["window"]
    assert window["source"] == "flexible_default"
    for option in _all_options(data):
        check_in, check_out = date.fromisoformat(option["check_in"]), date.fromisoformat(option["check_out"])
        assert (check_out - check_in).days == 5 == option["nights"]
        assert window["earliest_check_in"] <= option["check_in"] and option["check_out"] <= window["latest_check_out"]
    # Enough lead time to arrange a nearby trip.
    assert date.fromisoformat(data["recommended"]["check_in"]) >= date.today() + timedelta(days=7)


def test_options_are_at_least_a_week_apart_and_labelled():
    data = _recommend(_body()).json()
    starts = [date.fromisoformat(option["check_in"]) for option in _all_options(data)]
    assert all(abs((a - b).days) >= 7 for i, a in enumerate(starts) for b in starts[i + 1:])
    assert data["recommended"]["label"] == "Best overall"


def test_guest_range_is_respected():
    start = date.today() + timedelta(days=60)
    data = _recommend(_body(earliest_check_in=start.isoformat(),
                            latest_check_out=(start + timedelta(days=20)).isoformat())).json()
    assert data["window"]["source"] == "guest_range"
    for option in _all_options(data):
        assert start.isoformat() <= option["check_in"] and option["check_out"] <= (start + timedelta(days=20)).isoformat()


def test_seasons_follow_the_guests_hemisphere():
    north = _recommend(_body(travel_timing="month_season", travel_period="summer")).json()
    south = _recommend(_body(travel_timing="month_season", travel_period="summer", **SYDNEY)).json()
    assert date.fromisoformat(north["recommended"]["check_in"]).month in (6, 7, 8)
    assert date.fromisoformat(south["recommended"]["check_in"]).month in (12, 1, 2)
    assert south["basis"]["hemisphere"] == "southern"


def test_the_same_answers_always_give_the_same_dates():
    first, second = _recommend(_body()).json(), _recommend(_body()).json()
    assert first["recommended"]["check_in"] == second["recommended"]["check_in"]
    assert [o["check_in"] for o in first["alternatives"]] == [o["check_in"] for o in second["alternatives"]]


# ==================== factors ====================

def test_recorded_climate_steers_an_active_coast_trip_to_the_dry_warm_months():
    body = _body(travel_timing="month_season", travel_period="summer", trip_nights=5, trip_goals=["adventure"],
                 preferred_moments=["beaches_water", "movement_adventure"], preferred_environments=["coast"],
                 recent_feelings=["energized"], trip_prompt="curious_to_explore")
    data = _recommend(body, climate=CLIMATE).json()
    best = data["recommended"]
    assert date.fromisoformat(best["check_in"]).month in (7, 8)
    assert any("averages highs around 25°C with about 3 rainy days" in reason or "about 4 rainy days" in reason
               for reason in best["reasons"])
    assert data["basis"]["climate"]["place"] == "the Vancouver area"


def test_a_restful_trip_offers_a_quieter_option_and_a_sunnier_one():
    body = _body(travel_timing="month_season", travel_period="summer", trip_nights=5, trip_goals=["restoration"],
                 preferred_environments=["coast"])
    data = _recommend(body, climate=CLIMATE).json()
    labels = {option["label"]: option for option in _all_options(data)}
    assert "Best weather" in labels
    assert date.fromisoformat(labels["Best weather"]["check_in"]).month in (7, 8)
    assert any("school holidays" in note for note in labels["Best weather"]["considerations"])


def test_quiet_seekers_avoid_public_holidays_in_their_home_country():
    # Canadian Thanksgiving (2nd Monday of October) is a provincial holiday in most provinces.
    year = date.today().year + 1
    thanksgiving = next(date(year, 10, day) for day in range(8, 15) if date(year, 10, day).weekday() == 0)
    data = _recommend(_body(trip_nights=3, earliest_check_in=date(year, 10, 1).isoformat(),
                            latest_check_out=date(year, 10, 31).isoformat())).json()
    for option in _all_options(data):
        stay = {date.fromisoformat(option["check_in"]) + timedelta(days=n) for n in range(3)}
        assert thanksgiving not in stay


def test_families_with_children_are_steered_to_school_holidays():
    data = _recommend(_body(travel_timing="month_season", travel_period="summer", trip_nights=7,
                            travel_party="family", party_adults=2, party_children=2)).json()
    best = data["recommended"]
    assert date.fromisoformat(best["check_in"]).month in (7, 8)
    assert any("school holidays" in reason for reason in best["reasons"])


def test_trip_length_shapes_the_days_of_the_week():
    weekend = _recommend(_body(trip_nights=2, trip_goals=["discovery"], recent_feelings=["curious"],
                               preferred_moments=["food_drinks"], trip_prompt="curious_to_explore")).json()
    assert weekend["recommended"]["check_in_weekday"] in ("Thursday", "Friday")
    week = _recommend(_body(trip_nights=7)).json()
    assert week["recommended"]["check_in_weekday"] in ("Saturday", "Sunday")


def test_holiday_calendar_includes_the_major_peaks():
    peaks = peak_days([2027], None, southern=False)
    assert peaks[date(2027, 12, 25)] == "the Christmas and New Year holidays"
    assert peaks[date(2027, 3, 28)] == "the Easter weekend"  # Easter Sunday 2027
    assert date(2027, 7, 15) in peaks
    southern = peak_days([2027], None, southern=True)
    assert date(2027, 7, 15) not in southern and date(2028, 1, 15) in southern


# ==================== explanation ====================

def test_every_option_explains_why_and_never_claims_availability_or_price():
    data = _recommend(_body()).json()
    assert data["availability_note"] == AVAILABILITY_NOTE
    assert data["status"] == "SUGGESTED_NOT_CHECKED"
    for option in _all_options(data):
        assert option["reasons"], option
        for text in option["reasons"]:
            assert not any(word in text.lower() for word in ("available", "cheap", "lowest", "deal"))


def test_ai_summary_is_used_when_it_follows_the_rules():
    ai = MagicMock(return_value=json.dumps({"summary": "Midweek dates in October keep things calm and unhurried for your break."}))
    data = _recommend(_body(), ai=ai).json()
    assert data["summary_source"] == "ai"
    assert data["summary"].startswith("Midweek dates in October")
    prompt = ai.call_args.args[0]
    assert "Never say or imply the dates are available" in prompt


@pytest.mark.parametrize("bad", [
    "These dates are available and the cheapest of the season.",
    "Book now for the best price on calm October days.",
])
def test_ai_summary_that_implies_availability_or_price_is_replaced(bad):
    ai = MagicMock(return_value=json.dumps({"summary": bad}))
    data = _recommend(_body(), ai=ai).json()
    assert data["summary_source"] == "fallback"
    assert data["summary"].startswith("We suggest ")
    assert ai.call_count == 2  # one retry with the reason, then the factual fallback


def test_private_free_text_never_reaches_the_model():
    ai = MagicMock(return_value=json.dumps({"summary": "Calm midweek dates suit a slower break."}))
    body = _body(recent_feelings=["something_else"], recent_feelings_other="PRIVATE-FEELING",
                 trip_prompt="something_else", trip_prompt_other="PRIVATE-REASON")
    _recommend(body, ai=ai)
    assert "PRIVATE" not in repr(ai.call_args_list)


def test_a_chosen_destination_uses_its_own_climate():
    initial = client.post("/get_suggested_city", json=build_intake()).json()
    chosen = initial["suggested_cities"][0]
    seen = []

    def climate(lat, lng):
        seen.append((lat, lng))
        return CLIMATE

    located = {"latitude": 38.7, "longitude": -9.1}
    with patch("app.router.dates_route.get_ai_response", MagicMock(side_effect=Exception("no model"))), \
         patch("app.router.dates_route.monthly_climate", climate), \
         patch("app.router.dates_route.lookup_destination_place", lambda destination: located):
        data = client.post("/recommend_travel_dates", json=_body(destination_id=chosen["destination_id"],
                                                                travel_distance="anywhere")).json()
    assert data["basis"]["climate"]["place"] == chosen["city_name"]
    assert seen, "the destination's coordinates were used for climate"
    assert _recommend(_body(destination_id="NOPE")).status_code == 404
