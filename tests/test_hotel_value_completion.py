from unittest.mock import patch

from app.router.city_content_route import _complete_hotel_values, _fallback_hotel, _find_hotel


def test_google_hotel_missing_values_receive_labelled_estimates():
    hotel = _complete_hotel_values(
        {
            "name": "Villa Mara Carmel",
            "address": "2408 Bay View Ave, Carmel, CA 93923, USA",
            "rating": 4.9,
            "price_level": "NOT_AVAILABLE",
            "photos": ["https://maps.example/villa.jpg"],
            "coords": {"lat": 36.5439376, "lng": -121.9303782},
        },
        city_name="Carmel-by-the-Sea",
        nightly_budget=500,
        profile_search_query="luxury restorative nature spa mindfulness",
    )

    assert hotel["price_level"] == "PRICE_LEVEL_LUXURY (approximately)"
    assert hotel["average_nightly_price"] == "$500 per night (approximately)"
    assert hotel["budget_tier"] == "Luxury (approximately)"
    assert hotel["facilities"] == [
        "Spa/wellness facilities (approximately)",
        "Mindfulness spaces or sessions (approximately)",
        "Nature-focused surroundings or access (approximately)",
        "Premium guest amenities (approximately)",
    ]
    assert "price level" in hotel["estimate_note"]
    assert "facilities" in hotel["estimate_note"]
    assert hotel["website"] == "Not available"


def test_missing_hotel_values_are_clearly_marked_as_approximate():
    fallback = _fallback_hotel("Example City")
    assert "(approximately)" in fallback["name"]
    assert "(approximately)" in fallback["address"]
    assert "(approximately)" in fallback["average_nightly_price"]
    assert "(approximately)" in fallback["estimate_note"]


def test_stay_search_prefers_best_rated_within_the_per_room_budget():
    hotels = [
        {"name": "Palace", "rating": 4.9, "price_level": "PRICE_LEVEL_LUXURY"},
        {"name": "Garden Inn", "rating": 4.6, "price_level": "PRICE_LEVEL_MODERATE"},
        {"name": "Hostel", "rating": 4.1, "price_level": "PRICE_LEVEL_INEXPENSIVE"},
    ]
    with patch("app.router.city_content_route.get_google_hotels_sorted_by_rating") as search:
        search.invoke.return_value = hotels
        assert _find_hotel("Ubud, Indonesia", 200, False)["name"] == "Garden Inn"
        assert _find_hotel("Ubud, Indonesia", 7000, True)["name"] == "Palace"
        assert _find_hotel("Ubud, Indonesia", 50, False)["name"] == "Hostel"
        search.invoke.return_value = [{"error": "offline"}]
        assert "(approximately)" in _find_hotel("Ubud, Indonesia", 200, False)["name"]
