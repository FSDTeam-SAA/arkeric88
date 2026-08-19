from unittest.mock import patch

from app.router.city_content_route import _fallback_hotel, _find_hotel
from src.core.retreat_catalog import find_retreat_candidates, load_retreat_catalog


def test_retreat_catalog_uses_location_before_facilities_and_budget():
    catalog = load_retreat_catalog()
    assert len(catalog) == 150

    candidates = find_retreat_candidates(
        city_name="Bali",
        preferred_region="Asia",
        facility_terms="spa mindfulness nature",
        nightly_budget=400,
    )

    assert candidates
    assert all("bali" in item["Region"].lower() for item in candidates)
    assert candidates[0]["Avg Night"].startswith("$")
    assert candidates[0]["Budget Tier"] in {
        "Entry", "Premium", "Luxury", "Ultra Luxury"
    }
    assert find_retreat_candidates("City Not In Workbook") == []


def test_hotel_selection_enriches_workbook_choice_with_google_maps_images():
    google_result = [{
        "name": "Fivelements Retreat Bali",
        "address": "Banjar Begawan, Bali, Indonesia",
        "rating": 4.8,
        "price_level": "PRICE_LEVEL_LUXURY",
        "photos": ["https://maps.example/fivelements.jpg"],
        "coords": {"lat": -8.48, "lng": 115.24},
    }]

    with patch(
        "app.router.city_content_route.get_google_hotels_by_facilities"
    ) as google_hotels:
        google_hotels.invoke.return_value = google_result
        hotel = _find_hotel(
            city_name="Bali",
            total_budget=5000,
            num_nights=5,
            profile_search_query="spa mindfulness nature",
            preferred_region="Asia",
        )

    assert hotel["name"] == "Fivelements Retreat Bali"
    assert hotel["photos"] == ["https://maps.example/fivelements.jpg"]
    assert hotel["average_nightly_price"].startswith("$")
    assert hotel["facilities"]
    assert hotel["estimate_note"] == ""


def test_missing_hotel_values_are_clearly_marked_as_approximate():
    fallback = _fallback_hotel("Example City")
    assert "(approximately)" in fallback["name"]
    assert "(approximately)" in fallback["address"]
    assert "(approximately)" in fallback["average_nightly_price"]
    assert "(approximately)" in fallback["estimate_note"]
