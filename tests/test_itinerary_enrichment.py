"""Activity-to-place matching and meal scheduling in the itinerary step."""

from unittest.mock import MagicMock, patch

from app.router.city_content_route import _add_daily_meals, _enrich_activities, _find_best_match

LOCATION = "Ubud, Indonesia"
IGNORE = frozenset({"ubud", "indonesia"})


def _tool(return_value) -> MagicMock:
    tool = MagicMock()
    tool.invoke.return_value = return_value
    return tool


def test_unrelated_place_is_never_a_match():
    spas = [{"name": "Mekar Ubud Jungle Spa by K club", "address": "Ubud"}]
    assert _find_best_match(spas, "Campuhan ridge walk", ignore_tokens=IGNORE) is None
    # Sharing only the destination name is not a match either.
    assert _find_best_match([{"name": "Ubud Ancient Spa"}], "Ubud sunrise trek", ignore_tokens=IGNORE) is None


def test_one_shared_proper_noun_is_not_enough_for_a_multi_word_activity():
    places = [{"name": "Tirta Ayu Spa Alam Puisi Ubud"}, {"name": "Tirta Empul Temple"}]
    assert _find_best_match(places, "Tirta Empul water temple", ignore_tokens=IGNORE)["name"] == "Tirta Empul Temple"
    assert _find_best_match(places[:1], "Tirta Empul water temple", ignore_tokens=IGNORE) is None


def test_lodging_listing_is_not_an_activity_venue():
    villa = [{"name": "The Calm Ubud - 1BR Romance Villa with Rice Field View"}]
    assert _find_best_match(villa, "Rice field stroll", ignore_tokens=IGNORE) is None


def test_meaningful_word_overlap_is_a_match():
    places = [{"name": "Ubud Ancient Spa"}, {"name": "Campuhan Ridge Walk"}]
    assert _find_best_match(places, "Campuhan ridge walk", ignore_tokens=IGNORE)["name"] == "Campuhan Ridge Walk"


def _plan(name: str) -> list:
    return [{"day": 1, "activities": [{
        "activity_name": name, "activity_description": "d", "activity_location": "Ubud",
        "activity_time": "10:00 AM - 11:00 AM", "activity_cost": 0,
    }]}]


def test_activity_uses_a_targeted_search_when_the_shared_dataset_has_no_match():
    search = MagicMock()
    search.invoke.side_effect = [
        [{"name": "Mekar Ubud Jungle Spa", "address": "Ubud", "photos": []}],  # shared dataset
        [{"name": "Campuhan Ridge Walk", "place_id": "ridge", "business_status": "OPERATIONAL",
          "address": "Jl. Raya Campuhan, Ubud, Bali, Indonesia", "photos": []}],  # targeted search
    ]
    with patch("app.router.city_content_route.get_detailed_tourist_places", search):
        plan = _enrich_activities(_plan("Campuhan ridge walk"), LOCATION)
    activity = plan[0]["activities"][0]
    assert activity["activity_name"] == "Campuhan Ridge Walk"
    assert activity["place_id"] == "ridge"
    assert search.invoke.call_args_list[1].args[0] == {"location_name": LOCATION, "search_query": "Campuhan ridge walk"}


def test_unmatched_activity_keeps_its_name_and_is_marked_unverified():
    with patch("app.router.city_content_route.get_detailed_tourist_places", _tool([
        {"name": "Mekar Ubud Jungle Spa", "address": "Ubud", "photos": []},
    ])):
        plan = _enrich_activities(_plan("Campuhan ridge walk"), LOCATION)
    activity = plan[0]["activities"][0]
    assert activity["activity_name"] == "Campuhan ridge walk"
    assert activity["place_id"] is None
    assert "not verified" in activity["availability_note"]


def test_same_sounding_place_in_another_country_is_rejected():
    # Text search for "fortress walls viewpoint in Cartagena, Colombia" once
    # returned Petrovaradin fortress in Novi Sad, Serbia.
    with patch("app.router.city_content_route.get_detailed_tourist_places", _tool([
        {"name": "Petrovaradin fortress walls viewpoint", "place_id": "serbia",
         "address": "Petrovaradin, Novi Sad, Serbia", "photos": []},
    ])):
        plan = _enrich_activities(_plan("Fortress walls viewpoint"), "Cartagena, Colombia")
    activity = plan[0]["activities"][0]
    assert activity["place_id"] is None
    assert activity["activity_name"] == "Fortress walls viewpoint"


def test_restaurant_search_stays_inside_the_destination():
    plan = [{"day": 1, "activities": [{
        "activity_name": "City walls walk", "activity_description": "d", "activity_location": "Old City walls",
        "activity_address": "N/A", "activity_time": "03:00 PM - 04:00 PM", "activity_cost": 0,
    }]}]
    restaurants = _tool([])
    with patch("app.router.city_content_route.get_nearby_restaurants", restaurants):
        plan = _add_daily_meals(plan, "1 Hotel Rd, Cartagena, Colombia", "Cartagena, Colombia")
    locations = {call.args[0]["location_name"] for call in restaurants.invoke.call_args_list}
    assert locations == {"Old City walls, Cartagena, Colombia"}
    names = [activity["activity_name"] for activity in plan[0]["activities"]]
    assert "Lunch at a spot of your choice nearby" in names


def test_restaurant_search_keeps_an_address_already_in_the_country():
    plan = [{"day": 1, "activities": [{
        "activity_name": "Fort visit", "activity_description": "d", "activity_location": "Fort",
        "activity_address": "Av. Pedro de Heredia, Cartagena, Bolívar, Colombia",
        "activity_time": "03:00 PM - 04:00 PM", "activity_cost": 0,
    }]}]
    restaurants = _tool([])
    with patch("app.router.city_content_route.get_nearby_restaurants", restaurants):
        _add_daily_meals(plan, "1 Hotel Rd", "Cartagena, Colombia")
    locations = {call.args[0]["location_name"] for call in restaurants.invoke.call_args_list}
    assert locations == {"Av. Pedro de Heredia, Cartagena, Bolívar, Colombia"}


def test_meal_that_clashes_with_an_activity_is_skipped():
    plan = [{"day": 1, "activities": [{
        "activity_name": "Sunrise trek", "activity_description": "d", "activity_location": "Batur",
        "activity_address": "N/A", "activity_time": "04:30 AM - 11:00 AM", "activity_cost": 0,
    }]}]
    with patch("app.router.city_content_route.get_nearby_restaurants", _tool([])):
        plan = _add_daily_meals(plan, "1 Hotel Rd", LOCATION)
    names = [activity["activity_name"] for activity in plan[0]["activities"]]
    assert not any(name.startswith("Breakfast") for name in names)
    assert any(name.startswith("Lunch") for name in names)
    assert any(name.startswith("Dinner") for name in names)
