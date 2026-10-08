from datetime import date

import requests

from src.core.hotel_rates import (
    HotelRateRequest,
    LiteAPIProvider,
    build_occupancies,
    enrich_hotel_with_live_rates,
    normalise_rates,
)
from src.core.itinerary_pricing import build_price_breakdown


def _payload():
    return {
        "data": [{
            "hotelId": "lp-1",
            "roomTypes": [{
                "offerId": "offer-1", "supplier": "Nuitee",
                "offerRetailRate": {"amount": 400.0, "currency": "USD"},
                "rates": [{
                    "rateId": "rate-1", "occupancyNumber": 1, "name": "Deluxe King",
                    "adultCount": 2, "childCount": 0, "boardName": "Breakfast Included",
                    "retailRate": {"total": [{"amount": 400.0, "currency": "USD"}]},
                    "taxesAndFees": [
                        {"included": True, "description": "VAT", "amount": 20, "currency": "USD"},
                        {"included": False, "description": "Resort fee", "amount": 30, "currency": "USD"},
                    ],
                    "cancellationPolicies": {
                        "refundableTag": "RFN",
                        "cancelPolicyInfos": [{"cancelTime": "2027-01-01 12:00:00", "amount": 0, "currency": "USD"}],
                    },
                }],
            }],
        }],
    }


def test_normalise_rates_keeps_included_and_property_charges_separate():
    offer = normalise_rates(_payload(), nights=2)["hotels"][0]["offers"][0]
    assert offer["total_price"] == 400.0
    assert offer["price_per_night"] == 200.0
    assert offer["included_taxes_and_fees"][0]["amount"] == 20.0
    assert offer["payable_at_property"][0]["amount"] == 30.0
    assert offer["rates"][0]["refundable"] is True
    assert offer["rates"][0]["meal_plan"] == "Breakfast Included"


def test_live_offer_total_is_not_taxed_or_multiplied_again_in_itinerary_total():
    stop = {
        "base_area": "Test City", "nights": 2, "nightly_usd": 200.0, "live_total": 400.0,
        "hotel": {"name": "Test Hotel", "price_status": "LIVE"},
    }
    result = build_price_breakdown([stop], [{"day": 1, "activities": []}], party_size=2, rooms=1, nights=2)
    stay = result["lines"][0]["details"][0]
    assert stay["subtotal"] == 400.0
    assert stay["basis"] == "verified live offer total"


def test_multi_room_search_requires_explicit_room_occupancies():
    try:
        build_occupancies(adults=3, children=1, rooms=2, child_ages=[6], room_occupancies=None)
    except ValueError as error:
        assert "Room-by-room" in str(error)
    else:
        raise AssertionError("Multi-room occupancy must not be invented")
    assert build_occupancies(
        adults=3, children=1, rooms=2, child_ages=[6],
        room_occupancies=[{"adults": 2, "children": [6]}, {"adults": 1, "children": []}],
    ) == [{"adults": 2, "children": [6]}, {"adults": 1, "children": []}]


class _Response:
    status_code = 200

    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body


class _Session:
    def __init__(self):
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if url.endswith("/data/hotels"):
            return _Response({"data": [{
                "id": "lp-1", "name": "Hotel Calm", "address": "1 Ocean Road, Test City",
                "latitude": 10.0, "longitude": 20.0,
            }]})
        return _Response(_payload())


def test_provider_verifies_google_mapping_then_uses_matched_id_for_rates():
    session = _Session()
    provider = LiteAPIProvider("key", "https://inventory.example/v3.0", "https://booking.example/v3.0", session=session)
    hotel = {"place_id": "google-place-1", "name": "Hotel Calm", "address": "1 Ocean Road, Test City", "coords": {"lat": 10.0, "lng": 20.0}}
    match = provider.match_hotel(hotel)
    assert match["status"] == "MATCHED" and match["hotel_id"] == "lp-1"
    rates = provider.get_rates("lp-1", HotelRateRequest("2027-01-10", "2027-01-12", "US", "USD", [{"adults": 2, "children": []}], 2))
    assert rates["hotels"][0]["available"] is True
    assert session.calls[1][2]["json"]["hotelIds"] == ["lp-1"]


class _UnavailableProvider:
    name = "test"

    def match_hotel(self, hotel):
        return {"status": "UNMATCHED", "reason": "ambiguous"}

    def get_rates(self, hotel_id, request):
        raise AssertionError("Rates must never be requested for an unmatched property")

    def prebook(self, offer_id):
        raise AssertionError("not used")


def test_unmatched_hotel_never_receives_a_price_or_availability_claim():
    result = enrich_hotel_with_live_rates(
        {"name": "Hotel Calm", "price_status": "ESTIMATED"},
        HotelRateRequest("2027-01-10", "2027-01-12", "US", "USD", [{"adults": 2, "children": []}], 2),
        _UnavailableProvider(),
    )
    assert result["live_rate_status"] == "UNMATCHED"
    assert result["availability_status"] == "UNMATCHED"
    assert result["price_status"] == "ESTIMATED"
