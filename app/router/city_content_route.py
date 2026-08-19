from fastapi import APIRouter, HTTPException
from src.core.prompt_templete import PromptGenerator
from src.service.chat_services import get_ai_response
from app.schemas.city_body import (
    RegenerateInputData,
    RegenerateActivityInputData,
    TourPlanRequestData,
    StayInfo,
)
from app.schemas.retreat_v2_schema import RetreatRecommendationRequest
from src.core.data_processor import ProcessData
from src.session.city_session_store import CitySessionStore, ActivitySessionStore
from src.tools.tools import get_cityinfo, get_detailed_tourist_places, get_google_hotels_sorted_by_rating, get_google_hotels_by_facilities, calculate_distance_routes_api, get_nearby_restaurants
from src.core.image_registry import image_registry
from src.core.geography import country_matches_region
from src.core.retreat_catalog import find_retreat_candidates, retreat_facilities, get_retreat_by_property_id
from src.core.legacy_profile_adapter import build_legacy_answers
from src.core.retreat_explanation import generate_match_explanations
from src.core.retreat_matching_orchestrator import build_ranked_pool, select_city_representatives
import re
router = APIRouter()


def _parse_ai_response(response_text: str) -> dict:
    """Parse AI response text and convert to structured dict."""
    try:
        return ProcessData.EnsureDict(response_text)
    except ValueError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


def _merge_regenerated_field(
    previous_response: dict,
    generated_response: dict,
    update_field_name: str,
) -> dict:
    """Merge regenerated field into previous response."""
    if update_field_name not in generated_response:
        raise HTTPException(
            status_code=502,
            detail=f"AI response did not include '{update_field_name}'.",
        )
    updated_response = previous_response.copy()
    updated_response[update_field_name] = generated_response[update_field_name]
    return updated_response


def _enrich_city_suggestions(suggested_cities: list, preferred_region: str = "") -> list:
    """
    Enrich LLM-proposed city suggestions with real data from get_cityinfo tool.
    For each city in the list:
    1. Call get_cityinfo with the city name
    2. Merge the tool's verified country, lat, lng, and photos list into the city dict
    3. If the tool errors, fall back to empty image list and null coordinates
    The LLM output should never contain city_image, latitude, or longitude -
    those are supplied entirely by this enrichment step.
    """
    enriched = []
    for city in suggested_cities:
        city_name = city.get("city_name", "")
        if not city_name:
            continue
        raw_country = city.get("country_name")
        raw_region_match = country_matches_region(raw_country or "", preferred_region)
        lookup_hint = raw_country if raw_region_match is True else preferred_region
        # Default values in case tool fails
        tool_country = None
        tool_photos = []
        tool_lat = None
        tool_lng = None
        try:
            result = get_cityinfo.invoke({
                "city_name": city_name,
                "region_hint": lookup_hint or None,
            })
            if result and "error" not in result:
                tool_country = result.get("country")
                tool_photos = result.get("photos", [])
                # Ensure photos is always a clean list[str]
                if not isinstance(tool_photos, list):
                    tool_photos = []
                else:
                    tool_photos = _clean_photos(tool_photos)
                location = result.get("lat")
                if location is not None:
                    tool_lat = float(location)
                location = result.get("lng")
                if location is not None:
                    tool_lng = float(location)
        except Exception:
            # Tool failure - keep defaults (empty photos, null coords)
            pass
        tool_region_match = country_matches_region(tool_country or "", preferred_region)
        if tool_region_match is False:
            # Never attach coordinates/photos from a same-named city in the wrong region.
            tool_country = None
            tool_photos = []
            tool_lat = None
            tool_lng = None
            if raw_region_match is not True:
                continue
        elif tool_region_match is None and raw_region_match is True:
            # An unknown lookup country must not override a region-valid model country.
            tool_country = None
            tool_photos = []
            tool_lat = None
            tool_lng = None
        elif tool_country is None and raw_region_match is False:
            continue

        # country_name priority: region-valid tool value > region-valid model value
        merged_country = tool_country if tool_country else city.get("country_name")
        passthrough = {
            key: value
            for key, value in city.items()
            if key not in {
                "city_name", "country_name", "city_image", "latitude",
                "longitude", "number_of_days", "description",
            }
        }
        enriched.append({
            "city_name": city_name,
            "country_name": merged_country,
            "city_image": tool_photos,
            "latitude": tool_lat,
            "longitude": tool_lng,
            "number_of_days": city.get("number_of_days"),
            "description": city.get("description", ""),
            **passthrough,
        })
    return enriched

# ==================== TOUR PLAN ENRICHMENT HELPERS ====================


def _find_hotel(
    city_name: str,
    total_budget: float,
    num_nights: int,
    profile_search_query: str = "",
    preferred_region: str = "",
    forced_retreat: dict | None = None,
) -> dict:
    """
    Find the best-rated hotel in the city that fits within the user's total budget.
    1. Call get_google_hotels_sorted_by_rating for the city
    2. Try best-rated first, then fall back to cheaper options if budget allows
    3. Returns hotel dict with name, address, rating, price_level, photos, coords

    `forced_retreat`: when a caller already selected an exact catalog record via
    a stable property_id (see BACKEND_DEVELOPER_CHANGES.md "Itinerary generation
    must accept property_id"), pass it here to skip the fuzzy city/facility
    search and enrich that single record with live Google Maps data instead.
    """
    nightly_budget = total_budget / max(num_nights, 1)
    catalog_candidates = (
        [forced_retreat]
        if forced_retreat is not None
        else find_retreat_candidates(
            city_name=city_name,
            preferred_region=preferred_region,
            facility_terms=profile_search_query,
            nightly_budget=nightly_budget,
        )
    )
    for retreat in catalog_candidates:
        property_name = retreat.get("Property Name", "")
        location = ", ".join(
            part for part in [retreat.get("Region", ""), retreat.get("Country", "")] if part
        )
        try:
            map_results = get_google_hotels_by_facilities.invoke({
                "location_name": location or city_name,
                "facilities": [],
                "search_query": property_name,
            })
            if map_results and not _is_tool_error_list(map_results):
                map_hotel = _best_hotel_name_match(map_results, property_name)
                if map_hotel:
                    return _merge_catalog_and_map_hotel(retreat, map_hotel)
        except Exception:
            continue

    if catalog_candidates:
        return _merge_catalog_and_map_hotel(catalog_candidates[0], {})

    try:
        hotels = get_google_hotels_sorted_by_rating.invoke({
            "location_name": city_name,
            "search_query": profile_search_query or None,
        })
        if not hotels or "error" in hotels[0]:
            # Fallback: return a placeholder
            return _fallback_hotel(city_name)
        # Estimate hotel cost: if price_level indicates a numeric range, use mid-point
        # Otherwise assume ~$150/night as a reasonable estimate
        best_hotel = hotels[0]
        hotel_cost_per_night = _estimate_hotel_cost(best_hotel)
        total_hotel_cost = hotel_cost_per_night * num_nights
        # If the best hotel fits within budget (leaving at least some for activities),
        # return it. Otherwise try cheaper options.
        if total_hotel_cost < total_budget * 0.7:  # Leaves 30%+ for activities
            return best_hotel
        # Try cheaper hotels
        for hotel in hotels[1:]:
            hotel_cost_per_night = _estimate_hotel_cost(hotel)
            total_hotel_cost = hotel_cost_per_night * num_nights
            if total_hotel_cost < total_budget * 0.5:
                return hotel
        # If nothing fits well, return the cheapest available
        return hotels[-1] if len(hotels) > 1 else best_hotel
    except Exception:
        return _fallback_hotel(city_name)


def _estimate_hotel_cost(hotel: dict) -> float:
    """Estimate nightly hotel cost from price_level or return a default."""
    nightly_price = str(hotel.get("average_nightly_price", ""))
    price_match = re.search(r"[\d,]+", nightly_price)
    if price_match:
        return float(price_match.group(0).replace(",", ""))
    price_str = hotel.get("price_level", "NOT_AVAILABLE")
    # Google price levels: PRICE_LEVEL_UNSPECIFIED=0, INEXPENSIVE=1, MODERATE=2, EXPENSIVE=3, LUXURY=4
    price_map = {
        "PRICE_LEVEL_UNSPECIFIED": 100,
        "PRICE_LEVEL_INEXPENSIVE": 80,
        "PRICE_LEVEL_MODERATE": 150,
        "PRICE_LEVEL_EXPENSIVE": 250,
        "PRICE_LEVEL_LUXURY": 400,
        "NOT_AVAILABLE": 150,
    }
    return price_map.get(price_str, 150)


def _fallback_hotel(city_name: str) -> dict:
    """Return a fallback hotel object when the tool fails."""
    return {
        "name": f"Recommended hotel/resort in {city_name} (approximately)",
        "address": f"Near central {city_name} (approximately)",
        "rating": 0.0,
        "price_level": "Moderate (approximately)",
        "photos": [],
        "coords": None,
        "average_nightly_price": "$150 per night (approximately)",
        "budget_tier": "Premium (approximately)",
        "facilities": [],
        "website": "",
        "estimate_note": "Hotel details were unavailable; displayed values are (approximately).",
    }


def _complete_hotel_values(
    hotel: dict,
    city_name: str,
    nightly_budget: float,
    profile_search_query: str = "",
) -> dict:
    """Populate unavailable hotel fields with clearly labelled estimates."""
    completed = hotel.copy()
    approximate_fields = []
    rating = float(completed.get("rating") or 0)

    price_level = completed.get("price_level")
    if not price_level or price_level == "NOT_AVAILABLE":
        if rating >= 4.8 or nightly_budget > 1000:
            price_level = "PRICE_LEVEL_LUXURY (approximately)"
        elif rating >= 4.5 or nightly_budget > 400:
            price_level = "PRICE_LEVEL_EXPENSIVE (approximately)"
        elif nightly_budget > 150:
            price_level = "PRICE_LEVEL_MODERATE (approximately)"
        else:
            price_level = "PRICE_LEVEL_INEXPENSIVE (approximately)"
        completed["price_level"] = price_level
        approximate_fields.append("price level")

    if not completed.get("average_nightly_price"):
        estimated_price = {
            "PRICE_LEVEL_INEXPENSIVE": 100,
            "PRICE_LEVEL_MODERATE": 225,
            "PRICE_LEVEL_EXPENSIVE": 350,
            "PRICE_LEVEL_LUXURY": 500,
        }
        normalized_level = price_level.replace(" (approximately)", "")
        amount = estimated_price.get(normalized_level, max(150, round(nightly_budget)))
        completed["average_nightly_price"] = f"${amount} per night (approximately)"
        approximate_fields.append("nightly price")

    if not completed.get("budget_tier"):
        amount = _estimate_hotel_cost(completed)
        if amount <= 150:
            tier = "Entry"
        elif amount <= 400:
            tier = "Premium"
        elif amount <= 1000:
            tier = "Luxury"
        else:
            tier = "Ultra Luxury"
        completed["budget_tier"] = f"{tier} (approximately)"
        approximate_fields.append("budget tier")

    if not completed.get("facilities"):
        profile = _normalize_place_text(profile_search_query)
        facility_rules = {
            "spa": "Spa/wellness facilities (approximately)",
            "mindful": "Mindfulness spaces or sessions (approximately)",
            "fitness": "Fitness facilities (approximately)",
            "nature": "Nature-focused surroundings or access (approximately)",
            "forest": "Nature-focused surroundings or access (approximately)",
            "water": "Water or coastal access (approximately)",
            "nutrition": "Health-conscious dining (approximately)",
            "diagnostic": "Wellness consultation services (approximately)",
            "luxury": "Premium guest amenities (approximately)",
        }
        facilities = []
        for keyword, label in facility_rules.items():
            if keyword in profile and label not in facilities:
                facilities.append(label)
        completed["facilities"] = facilities or [
            "Guest accommodation services (approximately)",
            "Wi-Fi (approximately)",
        ]
        approximate_fields.append("facilities")

    completed["website"] = completed.get("website") or "Not available"
    prior_note = completed.get("estimate_note", "").strip()
    fields_text = ", ".join(approximate_fields)
    new_note = (
        f"Unavailable {fields_text} values are (approximately)."
        if approximate_fields else ""
    )
    completed["estimate_note"] = " ".join(
        note for note in [prior_note, new_note] if note
    )
    return completed

def _best_hotel_name_match(hotels: list[dict], property_name: str) -> dict | None:
    """Return a Google Maps result only when its name matches the workbook property."""
    generic_tokens = {"hotel", "resort", "retreat", "spa", "estate", "the"}
    target_tokens = set(_normalize_place_text(property_name).split()) - generic_tokens
    scored = [
        (
            len(target_tokens & set(_normalize_place_text(hotel.get("name", "")).split())),
            hotel,
        )
        for hotel in hotels
    ]
    score, hotel = max(scored, key=lambda item: item[0])
    return hotel if score > 0 else None


def _merge_catalog_and_map_hotel(retreat: dict, map_hotel: dict) -> dict:
    """Use the workbook for selection and Google Maps for current details/images."""
    approximate_fields = []
    region_country = ", ".join(
        part for part in [retreat.get("Region", ""), retreat.get("Country", "")] if part
    )
    address = map_hotel.get("address")
    if not address or address == "No address listed":
        address = f"{region_country or 'Location unavailable'} (approximately)"
        approximate_fields.append("address")
    rating = map_hotel.get("rating")
    if rating in (None, 0, 0.0):
        experience_score = retreat.get("⌀ Score", "")
        try:
            rating = round(min(5.0, float(experience_score) / 2), 1)
        except (TypeError, ValueError):
            rating = 4.0
        approximate_fields.append("rating")
    price_level = map_hotel.get("price_level")
    if not price_level or price_level == "NOT_AVAILABLE":
        price_level = f"{retreat.get('Budget Tier') or 'Premium'} (approximately)"
        approximate_fields.append("price level")
    average_nightly_price = retreat.get("Avg Night", "")
    if not average_nightly_price:
        average_nightly_price = "$150 per night (approximately)"
        approximate_fields.append("nightly price")
    return {
        "name": map_hotel.get("name") or retreat.get("Property Name") or "Retreat",
        "address": address,
        "rating": rating,
        "price_level": price_level,
        "photos": map_hotel.get("photos", []),
        "coords": map_hotel.get("coords"),
        "average_nightly_price": average_nightly_price,
        "budget_tier": retreat.get("Budget Tier", ""),
        "facilities": retreat_facilities(retreat),
        "website": retreat.get("Website", ""),
        "estimate_note": (
            f"Unavailable {', '.join(approximate_fields)} values are (approximately)."
            if approximate_fields else ""
        ),
    }

_PHOTO_SENTINELS = {"No photos available", "No photo available"}
_MEAL_SCHEDULE = {
    "Breakfast": ("08:00 AM - 09:00 AM", 18),
    "Lunch": ("12:30 PM - 01:30 PM", 30),
    "Dinner": ("07:00 PM - 08:30 PM", 55),
}


def _dump_tour_plan(tour_plan: list) -> list:
    """Convert Pydantic/dataclass tour plan objects to plain dicts."""
    dumped = []
    for day in tour_plan or []:
        day_dict = day.model_dump() if hasattr(day, "model_dump") else dict(day)
        dumped.append(day_dict)
    return dumped


def _normalize_place_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def _place_key(place: dict) -> str:
    name = _normalize_place_text(place.get("name") or place.get("activity_name", ""))
    address = _normalize_place_text(place.get("address") or place.get("activity_address", ""))
    return f"{name}|{address}" if name or address else ""


def _clean_photos(photos: list) -> list:
    if not isinstance(photos, list):
        return []
    return [
        image_registry.resolve(photo)
        for photo in photos
        if isinstance(photo, str) and photo not in _PHOTO_SENTINELS
    ]


def _is_tool_error_list(results: list) -> bool:
    return bool(results and isinstance(results[0], dict) and "error" in results[0])


def _build_profile_search_context(questions_answers) -> str:
    """Create positive Google Places terms from all experience-shaping answers."""
    nightly_budget = questions_answers.budget_per_person_per_night
    if nightly_budget is None:
        nightly_budget = questions_answers.effective_total_budget / questions_answers.trip_length_days
    if nightly_budget <= 150:
        budget_tier = "entry budget"
    elif nightly_budget <= 400:
        budget_tier = "premium"
    elif nightly_budget <= 1000:
        budget_tier = "luxury"
    else:
        budget_tier = "ultra luxury"
    terms = [
        questions_answers.experience_kind,
        questions_answers.travel_style,
        questions_answers.trip_organization,
        questions_answers.life_season,
        " ".join(questions_answers.preferred_environments),
        f"{questions_answers.energy_level} energy",
        budget_tier,
        "wellness restorative experiences",
    ]
    return " ".join(str(term).strip() for term in terms if str(term).strip())


def _fetch_attraction_dataset(city_name: str, profile_search_query: str = "") -> list[dict]:
    """Fetch all relevant attractions once for the whole itinerary."""
    try:
        results = get_detailed_tourist_places.invoke({
            "location_name": city_name,
            "search_query": profile_search_query or None,
        })
        if not results or _is_tool_error_list(results):
            return []
        return results
    except Exception:
        return []


def _activity_is_meal(activity: dict) -> bool:
    name = (activity.get("activity_name") or "").lower()
    return any(meal.lower() in name for meal in _MEAL_SCHEDULE)


def _seed_used_place_keys(existing_plan: list | None, exclude_day: int | None = None) -> set[str]:
    used_place_keys = set()
    for day in _dump_tour_plan(existing_plan or []):
        if exclude_day is not None and day.get("day") == exclude_day:
            continue
        for activity in day.get("activities", []):
            if _activity_is_meal(activity):
                continue
            key = _place_key({
                "name": activity.get("activity_name", ""),
                "address": activity.get("activity_address", ""),
            })
            if key:
                used_place_keys.add(key)
    return used_place_keys


def _enrich_activities(
    tour_plan: list,
    city_name: str,
    existing_plan: list | None = None,
    day_to_regenerate: int | None = None,
    profile_search_query: str = "",
) -> list:
    """Enrich activities from one shared, profile-led dataset and avoid repeats."""
    attraction_dataset = _fetch_attraction_dataset(city_name, profile_search_query)
    used_place_keys = _seed_used_place_keys(existing_plan, day_to_regenerate)
    enriched_plan = []
    for day in tour_plan:
        enriched_day = {
            "day": day.get("day"),
            "activities": [],
        }
        for activity in day.get("activities", []):
            if _activity_is_meal(activity):
                continue
            activity_name = activity.get("activity_name", "")
            activity_location = activity.get("activity_location", "")
            verified_name = activity_name
            verified_address = "N/A"
            verified_photos = []
            best_match = _find_best_match(
                attraction_dataset,
                activity_name,
                activity_location,
                used_place_keys=used_place_keys,
            )
            if best_match:
                verified_name = best_match.get("name", activity_name)
                verified_address = best_match.get("address", "N/A")
                verified_photos = _clean_photos(best_match.get("photos", []))
                match_key = _place_key(best_match)
                if match_key:
                    used_place_keys.add(match_key)
            enriched_activity = {
                "activity_name": verified_name,
                "activity_description": activity.get("activity_description", ""),
                "activity_location": activity.get("activity_location", ""),
                "activity_address": verified_address,
                "activity_image": verified_photos,
                "activity_time": activity.get("activity_time", ""),
                "activity_cost": activity.get("activity_cost", 0),
                "distance_from_previous_km": None,
            }
            enriched_day["activities"].append(enriched_activity)
        enriched_plan.append(enriched_day)
    return enriched_plan


def _find_best_match(
    results: list,
    activity_name: str,
    location_hint: str = "",
    used_place_keys: set[str] | None = None,
) -> dict | None:
    """Find the best unused place for an activity, falling back only if necessary."""
    if not results:
        return None
    used_place_keys = used_place_keys or set()
    activity_lower = _normalize_place_text(activity_name)
    location_lower = _normalize_place_text(location_hint)
    scored = []
    for place in results:
        key = _place_key(place)
        name = _normalize_place_text(place.get("name", ""))
        address = _normalize_place_text(place.get("address", ""))
        score = 0
        if activity_lower == name:
            score += 100
        elif activity_lower and activity_lower in name:
            score += 50
        elif name and name in activity_lower:
            score += 35
        activity_tokens = set(activity_lower.split())
        name_tokens = set(name.split())
        if activity_tokens and name_tokens:
            score += len(activity_tokens & name_tokens) * 6
        if location_lower and (location_lower in address or location_lower in name):
            score += 20
        already_used = key in used_place_keys
        scored.append((already_used, score, place))
    unused_matches = [item for item in scored if not item[0]]
    candidates = unused_matches or scored
    candidates.sort(key=lambda item: item[1], reverse=True)
    return candidates[0][2] if candidates else None


def _start_minutes(time_range: str) -> int | None:
    match = re.search(r"(\d{1,2}):(\d{2})\s*([AP]M)", time_range or "", re.IGNORECASE)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2))
    suffix = match.group(3).upper()
    if suffix == "PM" and hour != 12:
        hour += 12
    elif suffix == "AM" and hour == 12:
        hour = 0
    return hour * 60 + minute


def _choose_anchor(activities: list, meal_name: str, hotel_address: str, city_name: str) -> str:
    real_activities = [activity for activity in activities if not _activity_is_meal(activity)]
    if not real_activities:
        return hotel_address or city_name
    if meal_name == "Breakfast":
        anchor = real_activities[0]
    elif meal_name == "Dinner":
        anchor = real_activities[-1]
    else:
        anchor = min(
            real_activities,
            key=lambda activity: abs((_start_minutes(activity.get("activity_time", "")) or 720) - 720),
        )
    return (
        anchor.get("activity_address")
        if anchor.get("activity_address") and anchor.get("activity_address") != "N/A"
        else anchor.get("activity_location")
        or hotel_address
        or city_name
    )


def _find_restaurant_for_meal(
    anchor_location: str,
    meal_name: str,
    used_restaurant_keys: set[str],
    city_name: str,
) -> dict:
    try:
        restaurants = get_nearby_restaurants.invoke({
            "location_name": anchor_location or city_name,
            "meal_type": meal_name.lower(),
        })
        if restaurants and not _is_tool_error_list(restaurants):
            for restaurant in restaurants:
                key = _place_key(restaurant)
                if key and key not in used_restaurant_keys:
                    used_restaurant_keys.add(key)
                    return restaurant
            restaurant = restaurants[0]
            key = _place_key(restaurant)
            if key:
                used_restaurant_keys.add(key)
            return restaurant
    except Exception:
        pass
    fallback = {
        "name": f"{meal_name} stop",
        "address": anchor_location or city_name,
        "photos": [],
    }
    used_restaurant_keys.add(_place_key(fallback))
    return fallback


def _meal_activity(meal_name: str, restaurant: dict, anchor_location: str) -> dict:
    time_window, cost = _MEAL_SCHEDULE[meal_name]
    restaurant_name = restaurant.get("name") or f"{meal_name} stop"
    address = restaurant.get("address") or anchor_location or "N/A"
    return {
        "activity_name": f"{meal_name} at {restaurant_name}",
        "activity_description": f"A convenient {meal_name.lower()} stop near the day's planned route.",
        "activity_location": restaurant_name,
        "activity_address": address,
        "activity_image": _clean_photos(restaurant.get("photos", [])),
        "activity_time": time_window,
        "activity_cost": cost,
        "distance_from_previous_km": None,
    }


def _add_daily_meals(tour_plan: list, hotel_address: str, city_name: str) -> list:
    used_restaurant_keys = set()
    for day in tour_plan:
        activities = [activity for activity in day.get("activities", []) if not _activity_is_meal(activity)]
        meal_activities = []
        for meal_name in _MEAL_SCHEDULE:
            anchor = _choose_anchor(activities, meal_name, hotel_address, city_name)
            restaurant = _find_restaurant_for_meal(anchor, meal_name, used_restaurant_keys, city_name)
            meal_activities.append(_meal_activity(meal_name, restaurant, anchor))
        day["activities"] = sorted(
            [*activities, *meal_activities],
            key=lambda activity: _start_minutes(activity.get("activity_time", "")) or 24 * 60,
        )
    return tour_plan


def _merge_tour_plan_days(existing_plan: list, replacement_days: list) -> list:
    replacement_by_day = {day.get("day"): day for day in replacement_days}
    merged = []
    replaced_days = set()
    for day in _dump_tour_plan(existing_plan):
        day_number = day.get("day")
        if day_number in replacement_by_day:
            merged.append(replacement_by_day[day_number])
            replaced_days.add(day_number)
        else:
            merged.append(day)
    for day in replacement_days:
        if day.get("day") not in replaced_days:
            merged.append(day)
    merged.sort(key=lambda day: day.get("day") or 0)
    return merged


def _calculate_distances(tour_plan: list, hotel_address: str) -> list:
    """
    For each day, calculate real distances using calculate_distance_routes_api:
    - First activity: from hotel address to that activity's real address
    - Every subsequent activity: from previous activity's real address to current
    - Chain resets at the start of each new day (back to hotel)
    If a tool lookup fails, distance_from_previous_km stays None.
    """
    for day in tour_plan:
        activities = day.get("activities", [])
        previous_address = hotel_address
        for activity in activities:
            current_address = activity.get("activity_address", "N/A")
            if current_address and current_address != "N/A":
                try:
                    distance_result = calculate_distance_routes_api.invoke({
                        "origin_address": previous_address,
                        "destination_address": current_address,
                    })
                    if "error" not in distance_result:
                        activity["distance_from_previous_km"] = distance_result.get("distance_km")
                    else:
                        activity["distance_from_previous_km"] = None
                except Exception:
                    activity["distance_from_previous_km"] = None
            else:
                activity["distance_from_previous_km"] = None
            # Update previous_address for chaining (use tool-verified address even if N/A)
            previous_address = current_address if current_address and current_address != "N/A" else previous_address
    return tour_plan


def _check_budget(
    tour_plan: list,
    hotel: dict,
    num_nights: int,
    total_budget: float,
) -> tuple:
    """
    Calculate total cost and verify it's within budget.
    Total cost = (hotel price_per_night -- num_nights) + sum of all activity costs.
    Returns (total_cost, is_within_budget).
    """
    # Calculate hotel total
    hotel_cost_per_night = _estimate_hotel_cost(hotel)
    hotel_total = hotel_cost_per_night * num_nights
    # Sum all activity costs
    activities_total = 0.0
    for day in tour_plan:
        for activity in day.get("activities", []):
            activities_total += activity.get("activity_cost", 0)
    total_cost = hotel_total + activities_total
    is_within_budget = total_cost <= total_budget
    return total_cost, is_within_budget


def _build_final_response(
    hotel: dict,
    tour_plan: list,
    total_cost: float,
    packing_tips: str,
    travel_tips: str,
    activity_session_id: str,
    city_name: str,
    source: str,
) -> dict:
    """Build the final response matching the required schema."""
    return {
        "activity_session_id": activity_session_id,
        "city": city_name,
        "stay": StayInfo(
            name=hotel.get("name", "N/A"),
            address=hotel.get("address", "N/A"),
            rating=hotel.get("rating", 0.0),
            price_level=hotel.get("price_level", "NOT_AVAILABLE"),
            photos=_clean_photos(hotel.get("photos", [])),
            coords=hotel.get("coords"),
            average_nightly_price=hotel.get("average_nightly_price", ""),
            budget_tier=hotel.get("budget_tier", ""),
            facilities=hotel.get("facilities", []),
            website=hotel.get("website", ""),
            estimate_note=hotel.get("estimate_note", ""),
        ),
        "tour_plan": tour_plan,
        "total_cost_estimate": round(total_cost, 2),
        "packing_tips": packing_tips,
        "travel_tips": travel_tips,
        "source": source,
    }

# ==================== CITY SUGGESTION FLOW ====================


def _build_raw_city_suggestions(candidates, explanations, number_of_days: int) -> list[dict]:
    """Turn ranked property candidates into the city-suggestion dict shape."""
    raw_cities = []
    for candidate in candidates:
        explanation = explanations.get(candidate.property_id, {"match_reasons": [], "warnings": []})
        raw_cities.append({
            "city_name": candidate.record.get("Region", ""),
            "country_name": candidate.record.get("Country", ""),
            "number_of_days": number_of_days,
            "description": " ".join(explanation["match_reasons"][:2]),
            "property_id": candidate.property_id,
            "match_score": round(candidate.total_score),
            "warnings": [*candidate.warnings, *explanation["warnings"]],
        })
    return raw_cities


def _enrich_city_suggestions_per_country(raw_cities: list[dict]) -> list[dict]:
    """
    Enrich one city at a time, using that city's own (ground-truth) country as
    the region hint/hard-constraint for _enrich_city_suggestions, since a
    single v2 session can suggest cities across several different countries
    (unlike the old flow, which had one preferred_region for the whole batch).
    """
    enriched = []
    for city in raw_cities:
        enriched.extend(_enrich_city_suggestions([city], preferred_region=city["country_name"]))
    return enriched


def _build_city_suggestion_response(
    enriched_cities: list[dict],
    ranking,
    profile,
) -> dict:
    return {
        "suggested_cities": enriched_cities,
        "excluded_count": ranking.excluded_count,
        "total_candidate_count": ranking.total_candidate_count,
        "data_gaps": ranking.data_gaps,
        "extracted_restrictions": {
            "codes": profile.restrictions.codes,
            "accessibility_needs": profile.restrictions.accessibility_needs,
            "unresolved_text": profile.restrictions.unresolved_text,
        },
    }


@router.post("/get_suggested_city")


async def get_suggested_city(request: RetreatRecommendationRequest):
    """
    GENERATE: run the deterministic property-matching engine (identical
    pipeline to POST /v2/retreat-recommendations) against the 15-question
    profile, then group the ranked properties into city-level suggestions --
    the single best-scoring, real, bookable property per distinct city.
    Stores the profile and shown property_ids in session for regenerate and
    for Step 2 (POST /get_tour_plan, unchanged). See API_CITY_FLOW_DOCS.md.
    """
    try:
        profile, ranking = build_ranked_pool(request)
        representatives = select_city_representatives(ranking.ranked, limit=5)
        explanations = generate_match_explanations(representatives)

        raw_cities = _build_raw_city_suggestions(
            representatives, explanations, profile.duration_nights_estimate
        )
        enriched_cities = _enrich_city_suggestions_per_country(raw_cities)
        response = _build_city_suggestion_response(enriched_cities, ranking, profile)

        # The itinerary/activity pipeline (Step 2) still runs on the legacy
        # QuestionAnswers shape -- see src/core/legacy_profile_adapter.py.
        legacy_answers = build_legacy_answers(request, profile)

        session = CitySessionStore.create(
            questions_answers=legacy_answers,
            suggested_cities=enriched_cities,
            response=response,
            v2_request=request.model_dump(mode="json"),
            shown_property_ids=[city["property_id"] for city in raw_cities],
        )
        return {
            "session_id": session.session_id,
            "suggested_cities": session.suggested_cities,
            "response": session.response,
        }
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error)) from error

@router.post("/regenerate_suggested_city")


async def regenerate_suggested_city(regenerate_data: RegenerateInputData):
    """
    REGENERATE: re-run the same deterministic matching pipeline for the same
    profile, excluding every property already shown in this session, and
    surface the next best distinct cities. Ranking is deterministic, so this
    never needs another model call to pick different cities.
    """
    session = CitySessionStore.get(regenerate_data.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    if not session.v2_request:
        raise HTTPException(
            status_code=409,
            detail=(
                "This session predates the property-matching questionnaire and has no "
                "stored v2 profile, so it cannot be regenerated. Start a new session "
                "with POST /get_suggested_city."
            ),
        )
    try:
        request = RetreatRecommendationRequest(**session.v2_request)
        profile, ranking = build_ranked_pool(request)
        already_shown = set(session.shown_property_ids)
        representatives = select_city_representatives(
            ranking.ranked, exclude_property_ids=already_shown, limit=5
        )
        if not representatives:
            raise HTTPException(
                status_code=404,
                detail="No further distinct cities are available for this profile.",
            )
        explanations = generate_match_explanations(representatives)

        raw_cities = _build_raw_city_suggestions(
            representatives, explanations, profile.duration_nights_estimate
        )
        enriched_cities = _enrich_city_suggestions_per_country(raw_cities)
        response = _build_city_suggestion_response(enriched_cities, ranking, profile)

        updated_session = CitySessionStore.update_response(
            session_id=regenerate_data.session_id,
            response=response,
            update_field_name="suggested_cities",
            user_instruction=regenerate_data.user_instruction or "",
            shown_property_ids=[
                *session.shown_property_ids,
                *(city["property_id"] for city in raw_cities),
            ],
        )
        if updated_session is None:
            raise HTTPException(status_code=404, detail="Session not found.")
        return {
            "session_id": updated_session.session_id,
            "suggested_cities": updated_session.suggested_cities,
            "response": updated_session.response,
        }
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error)) from error

# ==================== ACTIVITY/TOUR PLAN FLOW ====================

@router.post("/get_tour_plan")


async def get_tour_plan(request_data: TourPlanRequestData):
    """
    GENERATE: First time -> create day-wise activity plan for selected city.
    Two-step pattern:
    1. LLM proposes activity names, descriptions, areas, times, costs
    2. Tool enrichment: hotel lookup, address/photos enrichment, distance calculation, budget check
    Returns: stay, tour_plan (with enriched addresses/images/distances), total_cost_estimate.
    """
    # Fetch parent city session
    city_session = CitySessionStore.get(request_data.session_id)
    if city_session is None:
        raise HTTPException(status_code=404, detail="City session not found.")
    # Resolve the destination. property_id (from POST /v2/retreat-recommendations)
    # takes precedence over selected_city per BACKEND_DEVELOPER_CHANGES.md
    # ("Itinerary generation must accept property_id, not only a city name.").
    forced_retreat = None
    city_name = request_data.selected_city
    if request_data.property_id:
        forced_retreat = get_retreat_by_property_id(request_data.property_id)
        if forced_retreat is None:
            raise HTTPException(
                status_code=404,
                detail=f"No property found for property_id '{request_data.property_id}'.",
            )
        city_name = forced_retreat.get("Region") or forced_retreat.get("Property Name") or city_name
    try:
        # Check if activity session already exists for this destination (to avoid re-generation)
        activity_session = ActivitySessionStore.get_by_city(
            session_id=request_data.session_id,
            city_name=city_name,
        )
        if activity_session is not None:
            # Activities already generated -> return cached version
            return {
                "activity_session_id": activity_session.activity_session_id,
                "city": activity_session.city,
                "stay": activity_session.stay,
                "tour_plan": activity_session.tour_plan,
                "total_cost_estimate": activity_session.total_cost_estimate,
                "packing_tips": activity_session.packing_tips,
                "travel_tips": activity_session.travel_tips,
                "source": "cached",
            }
        # === STEP 1: LLM proposes activities (names, descriptions, areas, times, costs only) ===
        prompt = PromptGenerator.gen_tour_plan_prompt(
            questions_answers=city_session.questions_answers,
            selected_city=city_name,
            trip_length_days=city_session.questions_answers.trip_length_days,
        )
        response_text = get_ai_response(prompt)
        response = _parse_ai_response(response_text)
        llm_tour_plan = response.get("tour_plan", [])
        packing_tips = response.get("packing_tips", "")
        travel_tips = response.get("travel_tips", "")
        # === STEP 2a: Tool - Find hotel ===
        budget = city_session.questions_answers.effective_total_budget
        num_nights = city_session.questions_answers.trip_length_days
        profile_search_query = _build_profile_search_context(city_session.questions_answers)
        hotel = _find_hotel(
            city_name,
            budget,
            num_nights,
            profile_search_query,
            city_session.questions_answers.preferred_region or "",
            forced_retreat=forced_retreat,
        )
        hotel = _complete_hotel_values(
            hotel,
            city_name,
            budget / max(num_nights, 1),
            profile_search_query,
        )
        hotel["photos"] = _clean_photos(hotel.get("photos", []))
        hotel_address = hotel.get("address", f"City Center, {city_name}")
        # === STEP 2b: Tool - Enrich activities with real addresses & photos ===
        enriched_plan = _enrich_activities(
            llm_tour_plan, city_name, profile_search_query=profile_search_query
        )
        enriched_plan = _add_daily_meals(enriched_plan, hotel_address, city_name)
        # === STEP 2c: Tool - Calculate real distances ===
        enriched_plan = _calculate_distances(enriched_plan, hotel_address)
        # === STEP 5: Budget check ===
        total_cost, is_within_budget = _check_budget(
            enriched_plan, hotel, num_nights, budget
        )
        # If over budget, try to find a cheaper hotel
        if not is_within_budget:
            # Try cheaper hotels
            try:
                all_hotels = get_google_hotels_sorted_by_rating.invoke({
                    "location_name": city_name,
                    "search_query": profile_search_query,
                })
                if all_hotels and "error" not in all_hotels[0]:
                    all_hotels = [
                        _complete_hotel_values(
                            candidate,
                            city_name,
                            budget / max(num_nights, 1),
                            profile_search_query,
                        )
                        for candidate in all_hotels
                    ]
                    # Sort by estimated price ascending
                    all_hotels_with_price = [
                        (h, _estimate_hotel_cost(h) * num_nights)
                        for h in all_hotels
                    ]
                    all_hotels_with_price.sort(key=lambda x: x[1])
                    for cheaper_hotel, _ in all_hotels_with_price:
                        test_cost, test_budget = _check_budget(
                            enriched_plan, cheaper_hotel, num_nights, budget
                        )
                        if test_budget:
                            hotel = cheaper_hotel
                            hotel["photos"] = _clean_photos(hotel.get("photos", []))
                            hotel_address = hotel.get("address", f"City Center, {city_name}")
                            # Recalculate distances since hotel changed
                            enriched_plan = _calculate_distances(enriched_plan, hotel_address)
                            total_cost = test_cost
                            is_within_budget = True
                            break
            except Exception:
                pass
        # Replace total_cost_estimate in response with actual hotel+activities total
        response["total_cost_estimate"] = round(total_cost, 2)
        # === STEP 6: Store in session ===
        # Create new activity session with enriched data
        activity_session = ActivitySessionStore.create(
            parent_session_id=request_data.session_id,
            city_name=city_name,
            tour_plan=enriched_plan,
            response=response,
            stay_data=hotel,
            total_cost_estimate=round(total_cost, 2),
            packing_tips=packing_tips,
            travel_tips=travel_tips,
        )
        return _build_final_response(
            hotel=hotel,
            tour_plan=enriched_plan,
            total_cost=total_cost,
            packing_tips=packing_tips,
            travel_tips=travel_tips,
            activity_session_id=activity_session.activity_session_id,
            city_name=city_name,
            source="generated",
        )
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error)) from error

@router.post("/regenerate_tour_plan")


async def regenerate_tour_plan(regenerate_data: RegenerateActivityInputData):
    """
    REGENERATE: User wants different day-wise activities (same city, new plan).
    Same two-step pattern as generate:
    1. LLM proposes new activity names/descriptions/areas/times/costs
    2. Tool enrichment: address/photos lookup, distance recalculation, budget re-check
    """
    # Fetch activity session
    activity_session = ActivitySessionStore.get(regenerate_data.activity_session_id)
    if activity_session is None:
        raise HTTPException(status_code=404, detail="Activity session not found.")
    # Fetch parent city session for Q&A
    city_session = CitySessionStore.get(activity_session.parent_session_id)
    if city_session is None:
        raise HTTPException(status_code=404, detail="Parent city session not found.")
    try:
        # === STEP 1: LLM proposes new activities ===
        prompt = PromptGenerator.regenerate_tour_plan_prompt(
            questions_answers=city_session.questions_answers,
            city_name=activity_session.city,
            current_tour_plan=activity_session.tour_plan,
            day_to_regenerate=regenerate_data.day_to_regenerate,
            user_instruction=regenerate_data.user_instruction,
        )
        response_text = get_ai_response(prompt)
        generated_response = _parse_ai_response(response_text)
        llm_tour_plan = generated_response.get("tour_plan", [])
        # === STEP 2: Tool enrichment ===
        city_name = activity_session.city
        budget = city_session.questions_answers.effective_total_budget
        num_nights = city_session.questions_answers.trip_length_days
        profile_search_query = _build_profile_search_context(city_session.questions_answers)
        # Reuse the existing hotel from the stored response if available
        hotel_data = activity_session.stay
        if hotel_data:
            hotel = {
                "name": hotel_data.name,
                "address": hotel_data.address,
                "rating": hotel_data.rating,
                "price_level": hotel_data.price_level,
                "photos": hotel_data.photos,
                "coords": hotel_data.coords,
                "average_nightly_price": hotel_data.average_nightly_price,
                "budget_tier": hotel_data.budget_tier,
                "facilities": hotel_data.facilities,
                "website": hotel_data.website,
                "estimate_note": hotel_data.estimate_note,
            }
        else:
            hotel = _find_hotel(
                city_name,
                budget,
                num_nights,
                profile_search_query,
                city_session.questions_answers.preferred_region or "",
            )
        hotel = _complete_hotel_values(
            hotel,
            city_name,
            budget / max(num_nights, 1),
            profile_search_query,
        )
        hotel["photos"] = _clean_photos(hotel.get("photos", []))
        hotel_address = hotel.get("address", f"City Center, {city_name}")
        # Enrich activities with real data
        enriched_plan = _enrich_activities(
            llm_tour_plan,
            city_name,
            existing_plan=activity_session.tour_plan,
            day_to_regenerate=regenerate_data.day_to_regenerate,
            profile_search_query=profile_search_query,
        )
        enriched_plan = _add_daily_meals(enriched_plan, hotel_address, city_name)
        # Calculate distances for the regenerated days, then merge if needed.
        enriched_plan = _calculate_distances(enriched_plan, hotel_address)
        full_tour_plan = (
            _merge_tour_plan_days(activity_session.tour_plan, enriched_plan)
            if regenerate_data.day_to_regenerate is not None
            else enriched_plan
        )
        # Budget check
        total_cost, is_within_budget = _check_budget(
            full_tour_plan, hotel, num_nights, budget
        )
        if not is_within_budget:
            try:
                all_hotels = get_google_hotels_sorted_by_rating.invoke({
                    "location_name": city_name,
                    "search_query": profile_search_query,
                })
                if all_hotels and "error" not in all_hotels[0]:
                    all_hotels = [
                        _complete_hotel_values(
                            candidate,
                            city_name,
                            budget / max(num_nights, 1),
                            profile_search_query,
                        )
                        for candidate in all_hotels
                    ]
                    all_hotels_with_price = [
                        (h, _estimate_hotel_cost(h) * num_nights)
                        for h in all_hotels
                    ]
                    all_hotels_with_price.sort(key=lambda x: x[1])
                    for cheaper_hotel, _ in all_hotels_with_price:
                        candidate_full_plan = (
                            _merge_tour_plan_days(activity_session.tour_plan, enriched_plan)
                            if regenerate_data.day_to_regenerate is not None
                            else enriched_plan
                        )
                        test_cost, test_budget = _check_budget(
                            candidate_full_plan, cheaper_hotel, num_nights, budget
                        )
                        if test_budget:
                            hotel = cheaper_hotel
                            hotel["photos"] = _clean_photos(hotel.get("photos", []))
                            hotel_address = hotel.get("address", f"City Center, {city_name}")
                            enriched_plan = _calculate_distances(enriched_plan, hotel_address)
                            full_tour_plan = (
                                _merge_tour_plan_days(activity_session.tour_plan, enriched_plan)
                                if regenerate_data.day_to_regenerate is not None
                                else enriched_plan
                            )
                            total_cost = test_cost
                            is_within_budget = True
                            break
            except Exception:
                pass
        generated_response["total_cost_estimate"] = round(total_cost, 2)
        generated_response["tour_plan"] = full_tour_plan
        # Merge enriched tour plan into response
        response = _merge_regenerated_field(
            previous_response=activity_session.response,
            generated_response=generated_response,
            update_field_name="tour_plan",
        )
        # Update activity session with enriched data
        updated_session = ActivitySessionStore.update_response(
            activity_session_id=regenerate_data.activity_session_id,
            response=response,
            day_to_regenerate=regenerate_data.day_to_regenerate,
            user_instruction=regenerate_data.user_instruction or "",
            stay_data=hotel,
            total_cost_estimate=round(total_cost, 2),
        )
        if updated_session is None:
            raise HTTPException(status_code=404, detail="Activity session not found.")
        return _build_final_response(
            hotel=hotel,
            tour_plan=full_tour_plan,
            total_cost=total_cost,
            packing_tips=activity_session.packing_tips if hasattr(activity_session, 'packing_tips') else "",
            travel_tips=activity_session.travel_tips if hasattr(activity_session, 'travel_tips') else "",
            activity_session_id=updated_session.activity_session_id,
            city_name=city_name,
            source="regenerated",
        )
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error)) from error

# ==================== SESSION MANAGEMENT ====================

@router.get("/session/{session_id}")


async def get_session_details(session_id: str):
    """Get full session details: Q&A, suggestions, and regeneration history."""
    session = CitySessionStore.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    return {
        "session_id": session.session_id,
        "questions_answers": session.questions_answers.model_dump(),
        "suggested_cities": [city.model_dump() for city in session.suggested_cities],
        "regeneration_history": session.history,
    }

@router.get("/activity_session/{activity_session_id}")


async def get_activity_session_details(activity_session_id: str):
    """Get full activity session details: city, tour plan, and regeneration history."""
    session = ActivitySessionStore.get(activity_session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Activity session not found.")
    return {
        "activity_session_id": session.activity_session_id,
        "parent_session_id": session.parent_session_id,
        "city": session.city,
        "stay": session.stay,
        "tour_plan": [day.model_dump() for day in session.tour_plan],
        "total_cost_estimate": session.total_cost_estimate,
        "packing_tips": session.packing_tips,
        "travel_tips": session.travel_tips,
        "regeneration_history": session.history,
    }

@router.delete("/session/{session_id}")


async def delete_session(session_id: str):
    """Delete a city session and all linked activity sessions."""
    if not CitySessionStore.delete(session_id):
        raise HTTPException(status_code=404, detail="Session not found.")
    ActivitySessionStore.delete_by_parent(session_id)
    return {"message": "Session and all linked activity sessions deleted successfully."}

@router.delete("/activity_session/{activity_session_id}")


async def delete_activity_session(activity_session_id: str):
    """Delete an activity session (does NOT delete parent city session)."""
    if not ActivitySessionStore.delete(activity_session_id):
        raise HTTPException(status_code=404, detail="Activity session not found.")
    return {"message": "Activity session deleted successfully."}
