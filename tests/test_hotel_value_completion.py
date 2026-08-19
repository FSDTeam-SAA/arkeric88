from app.router.city_content_route import _complete_hotel_values


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
