"""Viator Partner API: client requests, product matching, schedules and itinerary pricing."""

from datetime import date
from unittest.mock import MagicMock, patch

from src.core.itinerary_pricing import build_price_breakdown
from src.core.viator_match import attach_viator_products, best_product, nearest_destination, schedule_on
from src.tools import viator

TOFINO = (49.153, -125.907)
DESTINATIONS = [
    {"destination_id": 75, "name": "Canada", "type": "COUNTRY", "latitude": 49.2, "longitude": -125.9},
    {"destination_id": 954, "name": "Tofino", "type": "CITY", "latitude": 49.15, "longitude": -125.90},
    {"destination_id": 616, "name": "Victoria", "type": "CITY", "latitude": 48.43, "longitude": -123.37},
]
KAYAK = {
    "productCode": "123KAYAK", "title": "Clayoquot Sound Sea Kayak Tour from Tofino",
    "productUrl": "https://www.viator.com/tours/Tofino/kayak/d954-123KAYAK?pid=P00000001&mcid=42383",
    "reviews": {"totalReviews": 412, "combinedAverageRating": 4.9},
    "pricing": {"summary": {"fromPrice": 129.0}, "currency": "USD"},
}
WHALES = {"productCode": "9WHALE", "title": "Whale Watching Cruise", "reviews": {"totalReviews": 50},
          "pricing": {"summary": {"fromPrice": 150.0}, "currency": "USD"}}
# Runs Tuesday-Sunday from May to September; closed on 9 June; C$140 per adult.
SCHEDULE = {"productCode": "123KAYAK", "currency": "CAD", "bookableItems": [{"productOptionCode": "AM", "seasons": [{
    "startDate": "2027-05-01", "endDate": "2027-09-30",
    "pricingRecords": [{
        "daysOfWeek": ["TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"],
        "timedEntries": [{"startTime": "09:00", "unavailableDates": [{"date": "2027-06-09", "reason": "SOLD_OUT"}]}],
        "pricingDetails": [{"pricingPackageType": "PER_PERSON", "ageBand": "ADULT",
                            "price": {"original": {"recommendedRetailPrice": 140.0}}},
                           {"pricingPackageType": "PER_PERSON", "ageBand": "CHILD",
                            "price": {"original": {"recommendedRetailPrice": 90.0}}}],
    }],
}]}]}


# ==================== client ====================

def test_client_is_a_no_op_without_a_key():
    with patch("src.tools.viator.requests.post") as post:
        assert "error" in viator.search_products("kayak", 954)
    post.assert_not_called()


def test_client_sends_the_documented_headers_and_body(monkeypatch):
    monkeypatch.setattr("src.config.config_env.settings.viator_api_key", "test-key")
    response = MagicMock(status_code=200)
    response.json.return_value = {"products": {"totalCount": 1, "results": [KAYAK]}}
    with patch("src.tools.viator.requests.post", return_value=response) as post:
        result = viator.search_products("Sea kayak", 954, "2027-06-08", "2027-06-08")
    assert result["products"] == [KAYAK]
    url, kwargs = post.call_args.args[0], post.call_args.kwargs
    assert url.endswith("/search/freetext")
    assert kwargs["headers"]["exp-api-key"] == "test-key"
    assert kwargs["headers"]["Accept"] == "application/json;version=2.0"
    assert kwargs["json"]["productFiltering"] == {"destination": "954", "dateRange": {"from": "2027-06-08", "to": "2027-06-08"}}
    assert kwargs["json"]["searchTypes"][0]["searchType"] == "PRODUCTS"
    assert kwargs["json"]["currency"] == "USD"


def test_exchange_rates_are_fetched_once_per_currency(monkeypatch):
    monkeypatch.setattr("src.config.config_env.settings.viator_api_key", "test-key")
    response = MagicMock(status_code=200)
    response.json.return_value = {"rates": [{"sourceCurrency": "CAD", "targetCurrency": "USD", "rate": 0.73}]}
    with patch("src.tools.viator.requests.post", return_value=response) as post:
        assert viator.usd_rate("CAD") == 0.73
        assert viator.usd_rate("CAD") == 0.73
        assert viator.usd_rate("USD") == 1.0
    assert post.call_count == 1


# ==================== matching ====================

def test_nearest_town_level_destination_is_used():
    assert nearest_destination(TOFINO, DESTINATIONS)["name"] == "Tofino"
    assert nearest_destination((10.0, 10.0), DESTINATIONS) is None


def test_products_must_genuinely_match_the_experience():
    assert best_product("Sea kayak in Clayoquot Sound", [WHALES, KAYAK])["productCode"] == "123KAYAK"
    assert best_product("Sunset beach walk", [WHALES, KAYAK]) is None
    assert best_product("Kayak", [KAYAK])["productCode"] == "123KAYAK"


def test_schedule_tells_whether_it_runs_and_its_price():
    assert schedule_on(SCHEDULE, date(2027, 6, 8)) == {"runs": True, "adult_price": 140.0, "currency": "CAD"}  # Tuesday
    assert schedule_on(SCHEDULE, date(2027, 6, 7))["runs"] is False   # Monday: not an operating day
    assert schedule_on(SCHEDULE, date(2027, 6, 9))["runs"] is False   # sold out that day
    assert schedule_on(SCHEDULE, date(2027, 11, 2))["runs"] is None   # outside every season


# ==================== itinerary ====================

def _days():
    return [
        {"day": 1, "stop": 1, "activities": [
            {"item_type": "experience", "activity_name": "Sea kayak in Clayoquot Sound", "activity_cost": 60},
            {"item_type": "meal", "activity_name": "Lunch at Wolf in the Fog", "activity_cost": 30},
        ]},
        {"day": 2, "stop": 1, "activities": [
            {"item_type": "experience", "activity_name": "Sea kayak at dawn in Clayoquot Sound", "activity_cost": 60},
        ]},
    ]


STOPS = [{"stop": 1, "base_area": "Tofino", "nights": 2, "first_day": 1, "last_day": 2,
          "base": {"latitude": TOFINO[0], "longitude": TOFINO[1]}, "nightly_usd": 200.0, "hotel": {"name": "Inn"}}]


def _attach(days, travel_dates=None):
    return attach_viator_products(
        days, STOPS, travel_dates,
        search=lambda term, destination_id, start, end: {"products": [WHALES, KAYAK]},
        schedule=lambda code: SCHEDULE,
        destinations=DESTINATIONS,
        usd_rate=lambda currency: 0.73,
    )


def test_exact_dates_use_the_schedule_price_and_flag_days_it_does_not_run():
    days = _days()
    notes = _attach(days, {1: date(2027, 6, 8), 2: date(2027, 6, 9)})
    kayak = days[0]["activities"][0]
    assert kayak["viator"]["product_code"] == "123KAYAK"
    assert kayak["viator"]["booking_url"].startswith("https://www.viator.com/")
    assert kayak["viator"]["runs_on_date"] is True
    assert kayak["activity_cost"] == round(140.0 * 0.73, 2) and kayak["price_source"] == "viator_schedule"
    assert kayak["availability_status"] == "SCHEDULED"
    assert "viator" not in days[0]["activities"][1], "meals are never matched to tours"
    sold_out = days[1]["activities"][0]
    assert sold_out["viator"]["runs_on_date"] is False
    assert sold_out.get("availability_status") is None
    assert [note["day"] for note in notes] == [2]
    assert "isn't scheduled on Viator for Wednesday 9 June" in notes[0]["guest_note"]


def test_without_dates_the_from_price_is_used():
    days = _days()
    assert _attach(days) == []
    kayak = days[0]["activities"][0]
    assert kayak["activity_cost"] == 129.0 and kayak["price_source"] == "viator_from_price"
    assert kayak["viator"]["runs_on_date"] is None


def test_price_breakdown_says_how_many_experiences_are_priced_from_viator():
    days = _days()
    _attach(days)
    days[1]["activities"][0].pop("price_source")
    experiences = next(line for line in build_price_breakdown(STOPS, days, 2, 1, 2)["lines"] if line["category"] == "experiences")
    assert experiences["basis"] == "Per person × 2 guests; 1 of 2 priced from Viator"


def test_nothing_happens_when_viator_is_not_configured():
    days = _days()
    assert attach_viator_products(days, STOPS) == []
    assert "viator" not in days[0]["activities"][0]
