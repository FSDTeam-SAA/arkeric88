"""
Enrichment layer.

The LLM prompts (see prompt_generator.py) deliberately do NOT ask the model
for images, coordinates, addresses, or distances — an LLM has no reliable
way to know these and will hallucinate plausible-but-wrong values.

This module takes the LLM's bare JSON output (city/activity names +
reasoning) and enriches it with real data pulled from the Google Places /
Routes tools:

- get_cityinfo            -> country, lat/lng, photos (list[str])
- get_detailed_tourist_places -> address, photos (list[str]) for an activity
- calculate_distance_routes_api -> real driving distance between two points

Distance rule:
- The FIRST activity of each day is measured FROM THE HOTEL/RESORT.
- Every activity after that is measured from the PREVIOUS activity in that
  same day (chained), not from the hotel and not from day 1's start point.
- Each new day restarts the chain at the hotel/resort again.
"""

from typing import Optional

from src.tools.tools import (
    get_cityinfo,
    get_detailed_tourist_places,
    calculate_distance_routes_api,
)


def enrich_city_suggestions(llm_response: dict) -> dict:
    """Enrich LLM-suggested cities with real images, country, and coordinates.

    Input: the raw LLM JSON (`{"suggested_cities": [...], "reasoning": ...}`)
    where each city only has city_name, country_name, number_of_days,
    description.

    Output: same structure, with each city additionally carrying:
    - country (verified, from Places API)
    - lat, lng
    - photos: list[str] (up to 4 image URLs, never a bare string)
    """
    enriched_cities = []

    for city in llm_response.get("suggested_cities", []):
        city_name = city.get("city_name", "")
        info = get_cityinfo.invoke({"city_name": city_name})

        if "error" in info:
            enriched_cities.append(
                {
                    **city,
                    "country": city.get("country_name", "N/A"),
                    "lat": None,
                    "lng": None,
                    "photos": [],
                }
            )
            continue

        enriched_cities.append(
            {
                **city,
                "country": info.get("country", city.get("country_name", "N/A")),
                "lat": info.get("lat"),
                "lng": info.get("lng"),
                "photos": info.get("photos", []),  # always a list[str]
            }
        )

    return {
        **llm_response,
        "suggested_cities": enriched_cities,
    }


def _lookup_activity_place(activity_name: str, city_name: str, area: str = "") -> dict:
    """Look up a single activity's real place data via Places API.

    Returns a dict with: address, photos (list[str]), lat, lng.
    Falls back to empty-safe defaults if nothing is found.
    """
    query_location = f"{area}, {city_name}" if area else city_name
    places = get_detailed_tourist_places.invoke(
        {"location_name": f"{activity_name} {query_location}"}
    )

    if not places or "error" in places[0]:
        return {"address": "N/A", "photos": [], "lat": None, "lng": None}

    place = places[0]
    return {
        "address": place.get("address", "N/A"),
        "photos": place.get("photos", []),  # always a list[str]
        "lat": place.get("lat"),
        "lng": place.get("lng"),
    }


def enrich_tour_plan(
    llm_response: dict,
    city_name: str,
    hotel_address: str,
) -> dict:
    """Enrich LLM-generated activities with real address, images, and chained distance.

    Input: raw LLM JSON (`{"tour_plan": [...], ...}`) where each activity only
    has activity_name, activity_description, activity_area, activity_time,
    activity_cost.

    hotel_address: the address/name of the hotel or resort the traveler is
    staying at. Used as the origin point for the first activity of each day.

    Distance rule:
    - Day's activity[0].distance_from_previous_km = distance(hotel -> activity[0])
    - Day's activity[i].distance_from_previous_km = distance(activity[i-1] -> activity[i])  for i > 0
    - This resets for every day (always starts back at the hotel).
    """
    enriched_days = []

    for day in llm_response.get("tour_plan", []):
        enriched_activities = []
        previous_address = hotel_address  # each day starts from the hotel/resort

        for activity in day.get("activities", []):
            activity_name = activity.get("activity_name", "")
            area = activity.get("activity_area", "")

            place_info = _lookup_activity_place(activity_name, city_name, area)
            current_address = (
                place_info["address"] if place_info["address"] != "N/A" else area or city_name
            )

            distance_result = calculate_distance_routes_api.invoke(
                {
                    "origin_address": previous_address,
                    "destination_address": current_address,
                }
            )
            distance_km = distance_result.get("distance_km", None)

            enriched_activities.append(
                {
                    **activity,
                    "activity_address": place_info["address"],
                    "activity_photos": place_info["photos"],  # list[str]
                    "activity_lat": place_info["lat"],
                    "activity_lng": place_info["lng"],
                    "distance_from_previous_km": distance_km,
                    "distance_from": "hotel" if previous_address == hotel_address else "previous activity",
                }
            )

            # chain forward: this activity becomes the origin for the next one
            previous_address = current_address

        enriched_days.append(
            {
                **day,
                "activities": enriched_activities,
            }
        )

    return {
        **llm_response,
        "tour_plan": enriched_days,
    }
