from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import ValidationError

from src.core.prompt_templete import PromptGenerator
from src.service.chat_services import get_ai_response
from app.schemas.city_body import (
    RegenerateInputData,
    RegenerateActivityInputData,
    TourPlanRequestData,
    StayInfo,
)
from app.schemas.intake_schema import (
    INTAKE_FORM_ID,
    INTAKE_FORM_VERSION,
    STORED_INTAKE_CONTEXT,
    TravelIntakeRequest,
)
from src.core.data_processor import ProcessData
from src.session.city_session_store import CitySessionStore, ActivitySessionStore
from src.tools.tools import get_detailed_tourist_places, get_google_hotels_sorted_by_rating, calculate_distance_routes_api, get_nearby_restaurants
from src.core.image_registry import image_registry
from src.core.destination_catalog import (
    Destination,
    get_catalog_version,
    get_destination,
    load_destination_candidates,
)
from src.core.destination_places import lookup_destination_place, lookup_missing_coordinates
from src.core.destination_matching import (
    build_no_valid_result,
    build_suggestion,
    rank_destinations,
    select_diverse,
)
from src.core.intake_mappings import INTAKE_MAPPING_VERSION, SCORING_VERSION
from src.core.origin import resolve_origin
from src.core.trip_profile import TripProfile, build_trip_profile
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


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _enrich_destination_media(suggestion: dict) -> dict:
    """
    Attach photos (and coordinates only when the catalog has none) from the
    shared, country-checked map lookup, and record its outcome and timestamp
    as evidence (IMPORT_RULES.csv "Freshness and privacy").
    """
    destination = get_destination(suggestion["destination_id"])
    place = lookup_destination_place(destination)
    enriched = dict(suggestion)
    enriched["city_image"] = _clean_photos(place["photos"])
    if suggestion.get("latitude") is not None and suggestion.get("longitude") is not None:
        coordinates_source = "catalog"
    elif place["latitude"] is not None and place["longitude"] is not None:
        enriched["latitude"] = place["latitude"]
        enriched["longitude"] = place["longitude"]
        coordinates_source = "place_lookup"
    else:
        coordinates_source = None
    enriched["evidence"] = {
        **suggestion.get("evidence", {}),
        "coordinates_source": coordinates_source,
        "place_lookup": {
            "query": place["query"],
            "outcome": place["outcome"],
            "checked_at_utc": place["checked_at_utc"],
        },
    }
    return enriched

# ==================== TOUR PLAN ENRICHMENT HELPERS ====================


def _find_hotel(
    location: str,
    nightly_budget: float,
    budget_open_ended: bool,
    profile_search_query: str = "",
) -> dict:
    """
    Pick a stay from a live Google Places search: the best-rated result whose
    estimated nightly cost fits the per-room budget, else the cheapest result.
    Places has no bookable rates, so every price here is an estimate and
    availability is never claimed (IMPORT_RULES.csv "Booking search and
    availability"). No historical property sheet is used.
    """
    try:
        hotels = get_google_hotels_sorted_by_rating.invoke({
            "location_name": location,
            "search_query": profile_search_query or None,
        })
        if not hotels or "error" in hotels[0]:
            return _fallback_hotel(location)
        if budget_open_ended:
            return hotels[0]
        for hotel in hotels:
            if _estimate_hotel_cost(hotel) <= nightly_budget:
                return hotel
        return min(hotels, key=_estimate_hotel_cost)
    except Exception:
        return _fallback_hotel(location)


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


def _build_profile_search_context(profile: TripProfile) -> str:
    """
    Positive Google Places terms from the guest's chosen moments, desired
    feelings and settings. Only option labels are used -- never free text --
    and avoided activities are left out (they are filtered after retrieval).
    """
    nightly_budget = profile.budget_per_night
    if nightly_budget <= 150:
        budget_tier = "entry budget"
    elif nightly_budget <= 400:
        budget_tier = "premium"
    elif nightly_budget <= 1000:
        budget_tier = "luxury"
    else:
        budget_tier = "ultra luxury"
    terms = [
        *profile.moment_labels,
        *profile.goal_labels,
        *([] if profile.surprise_me else profile.environment_labels),
        budget_tier,
    ]
    return " ".join(str(term).strip().lower() for term in terms if str(term).strip())


_CLOSED_BUSINESS_STATUSES = {"CLOSED_PERMANENTLY", "CLOSED_TEMPORARILY"}
_PLACE_AVAILABILITY_NOTE = (
    "Opening hours do not confirm tickets or availability; confirm time-sensitive activities with the operator."
)


def _is_open_business(place: dict) -> bool:
    return place.get("business_status") not in _CLOSED_BUSINESS_STATUSES


def _fetch_attraction_dataset(city_name: str, profile_search_query: str = "") -> list[dict]:
    """Fetch all relevant attractions once for the whole itinerary."""
    try:
        results = get_detailed_tourist_places.invoke({
            "location_name": city_name,
            "search_query": profile_search_query or None,
        })
        if not results or _is_tool_error_list(results):
            return []
        return [place for place in results if _is_open_business(place)]
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
            place_id = None
            business_status = None
            availability_note = "Place not verified on the map; confirm details before visiting."
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
                place_id = best_match.get("place_id")
                business_status = best_match.get("business_status")
                availability_note = _PLACE_AVAILABILITY_NOTE
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
                "place_id": place_id,
                "business_status": business_status,
                "availability_note": availability_note,
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
            restaurants = [restaurant for restaurant in restaurants if _is_open_business(restaurant)]
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
        "place_id": restaurant.get("place_id"),
        "business_status": restaurant.get("business_status"),
        "availability_note": (
            "Opening hours and seating are not confirmed; check with the restaurant, "
            "including any dietary needs."
        ),
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


def _check_budget(tour_plan: list, hotel: dict, profile: TripProfile) -> tuple:
    """
    Estimated trip cost, and whether the stay fits the budget.

    The budget is USD per room, per night (IMPORT_RULES.csv "Budget"), so the
    stay is compared per room-night; activities are costed per person across
    the party and reported in the total but never counted against the
    lodging budget. Returns (total_cost_estimate, stay_within_budget).
    """
    nightly = _estimate_hotel_cost(hotel)
    lodging_total = nightly * profile.nights * profile.rooms
    activities_total = sum(
        activity.get("activity_cost", 0) or 0
        for day in tour_plan
        for activity in day.get("activities", [])
    ) * profile.party_size
    within_budget = profile.budget_open_ended or nightly <= profile.budget_per_night
    return lodging_total + activities_total, within_budget


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
            price_status="ESTIMATED",
            availability_status="NOT_CHECKED",
        ),
        "tour_plan": tour_plan,
        "total_cost_estimate": round(total_cost, 2),
        "packing_tips": packing_tips,
        "travel_tips": travel_tips,
        "source": source,
    }

# ==================== DESTINATION SUGGESTION FLOW ====================


def _load_session_intake(intake: dict) -> TravelIntakeRequest:
    """Re-read an intake stored in a session (see STORED_INTAKE_CONTEXT)."""
    return TravelIntakeRequest.model_validate(intake, context=STORED_INTAKE_CONTEXT)


def _validation_error(error: ValidationError) -> HTTPException:
    import json
    return HTTPException(status_code=422, detail=json.loads(error.json(include_url=False)))


def _match_intake(request: TravelIntakeRequest, exclude_ids: frozenset = frozenset()) -> tuple:
    """
    Run the deterministic destination matcher and build the response body.
    Returns (response, destination_ids_shown). No LLM call happens here; the
    only network calls are the departure geocode and coordinate lookups for
    catalog rows without coordinates (both skipped when the guest is open to
    anywhere) and the per-suggestion photo lookup. None of them receives any
    intake field besides the departure place name.
    """
    profile = build_trip_profile(request)
    origin = None
    if profile.travel_distance != "anywhere":
        origin = resolve_origin(
            request.departure_location,
            request.departure_latitude,
            request.departure_longitude,
            request.departure_country,
        )
    looked_up = (
        lookup_missing_coordinates(load_destination_candidates())
        if origin is not None and origin.has_coordinates
        else {}
    )
    result = rank_destinations(profile, origin, looked_up)
    selected = select_diverse(result.ranked, exclude_ids=set(exclude_ids))
    suggestions = [
        _enrich_destination_media(build_suggestion(item, profile, origin))
        for item in selected
    ]

    if not result.ranked:
        match_status = "no_valid_result"
    elif not suggestions:
        match_status = "exhausted"
    else:
        match_status = "matched"

    response = {
        "match_status": match_status,
        "suggested_cities": suggestions,
        "no_valid_result": build_no_valid_result(result, profile) if match_status == "no_valid_result" else None,
        "clarifications": profile.clarifications,
        "guest_context": profile.guest_context(),
        "eligible_count": len(result.ranked),
        "excluded_count": len(result.exclusions),
        "excluded_by_reason": result.excluded_by_reason(),
        "total_candidate_count": result.total_candidate_count,
        "data_gaps": result.data_gaps,
        "origin": origin.to_dict() if origin is not None else {"status": "NOT_NEEDED"},
        "estimate_status": (
            "Suggestions are editorial candidates. No live price, availability or route has been checked."
        ),
        "generated_at_utc": _utc_now(),
        "intake_form": {"form_id": INTAKE_FORM_ID, "form_version": INTAKE_FORM_VERSION},
        "catalog_version": get_catalog_version(),
        "intake_mapping_version": INTAKE_MAPPING_VERSION,
        "scoring_version": SCORING_VERSION,
    }
    return response, [suggestion["destination_id"] for suggestion in suggestions]


def _suggestion_payload(session) -> dict:
    return {
        "session_id": session.session_id,
        "match_status": session.response.get("match_status"),
        "suggested_cities": session.suggested_cities,
        "response": session.response,
    }


@router.post("/get_suggested_city")


async def get_suggested_city(request: TravelIntakeRequest):
    """
    GENERATE: rank the destination catalog against the 11-step Velari intake
    and return 2-3 diverse candidate destinations, each with match reasons
    tied to the guest's answers, tradeoffs, unresolved facts and verification
    status. When no destination survives the hard constraints, returns
    match_status "no_valid_result" naming the blocking constraints instead of
    relaxing them. See API_CITY_FLOW_DOCS.md.
    """
    try:
        response, shown_ids = _match_intake(request)
        session = CitySessionStore.create(
            intake=request.model_dump(mode="json"),
            suggested_cities=response["suggested_cities"],
            response=response,
            shown_destination_ids=shown_ids,
        )
        return _suggestion_payload(session)
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


# Party fields depend on travel_party; when a refinement changes the party
# type without resending them, the stored counts must not carry over.
_PARTY_DEPENDENT_FIELDS = ("party_adults", "party_children", "party_rooms", "party_child_ages")


def _apply_intake_updates(stored: dict, updates: dict) -> TravelIntakeRequest:
    merged = {**stored, **updates}
    if "travel_party" in updates and updates["travel_party"] != stored.get("travel_party"):
        for field_name in _PARTY_DEPENDENT_FIELDS:
            if field_name not in updates:
                merged.pop(field_name, None)
    # Refined answers are a new submission, so the normal date rules apply.
    return TravelIntakeRequest.model_validate(merged)


@router.post("/regenerate_suggested_city")


async def regenerate_suggested_city(regenerate_data: RegenerateInputData):
    """
    REGENERATE: without intake_updates, re-rank the same answers and show the
    next best destinations not yet shown in this session. With intake_updates
    (e.g. a constraint the guest agreed to flex), re-validate the refined
    answers and rank from scratch.
    """
    session = CitySessionStore.get(regenerate_data.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    try:
        if regenerate_data.intake_updates:
            request = _apply_intake_updates(session.intake, regenerate_data.intake_updates)
            exclude_ids = frozenset()
            updated_intake = request.model_dump(mode="json")
        else:
            request = _load_session_intake(session.intake)
            exclude_ids = frozenset(session.shown_destination_ids)
            updated_intake = None
    except ValidationError as error:
        raise _validation_error(error) from error

    try:
        response, new_ids = _match_intake(request, exclude_ids)
        if response["match_status"] == "exhausted":
            raise HTTPException(
                status_code=404,
                detail=(
                    "No further destinations are available for these answers. "
                    "Send intake_updates to refine the answers and see different options."
                ),
            )
        shown_ids = new_ids if updated_intake is not None else [*session.shown_destination_ids, *new_ids]
        updated_session = CitySessionStore.update_response(
            session_id=regenerate_data.session_id,
            response=response,
            update_field_name="suggested_cities",
            user_instruction=regenerate_data.user_instruction or "",
            shown_destination_ids=shown_ids,
            intake=updated_intake,
        )
        if updated_session is None:
            raise HTTPException(status_code=404, detail="Session not found.")
        return _suggestion_payload(updated_session)
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error)) from error

# ==================== ACTIVITY/TOUR PLAN FLOW ====================


def _resolve_selected_destination(city_session, request_data: TourPlanRequestData) -> Destination:
    """
    Only a destination suggested in this session can be planned: anything else
    was never matched against the guest's answers, or was excluded by them
    (IMPORT_RULES.csv "Candidate versus recommendation").
    """
    shown_ids = city_session.shown_destination_ids
    if request_data.destination_id:
        destination = get_destination(request_data.destination_id)
        if destination is None:
            raise HTTPException(
                status_code=404,
                detail=f"No catalog destination found for destination_id '{request_data.destination_id}'.",
            )
        if destination.destination_id not in shown_ids:
            raise HTTPException(
                status_code=400,
                detail=f"destination_id '{request_data.destination_id}' was not suggested in this session.",
            )
        return destination

    wanted = request_data.selected_city.strip().lower()
    if not wanted:
        raise HTTPException(status_code=400, detail="Provide destination_id (preferred) or selected_city.")
    shown = [get_destination(destination_id) for destination_id in shown_ids]
    for destination in shown:
        if destination is not None and destination.destination.lower() == wanted:
            return destination
    options = ", ".join(destination.destination for destination in shown if destination is not None)
    raise HTTPException(
        status_code=400,
        detail=f"'{request_data.selected_city}' was not suggested in this session. Choose one of: {options or 'none'}.",
    )


def _location_for(destination: Optional[Destination], fallback: str) -> str:
    """Destination plus its catalog country, so map searches stay in the right country."""
    if destination is None:
        return fallback
    return f"{destination.destination}, {destination.country}"


def _cheaper_stay_within_budget(
    location: str, profile: TripProfile, profile_search_query: str, tour_plan: list
) -> Optional[tuple]:
    """Cheapest-first search for a stay whose estimated nightly cost fits the budget."""
    try:
        hotels = get_google_hotels_sorted_by_rating.invoke({
            "location_name": location,
            "search_query": profile_search_query,
        })
    except Exception:
        return None
    if not hotels or "error" in hotels[0]:
        return None
    candidates = sorted(
        (
            _complete_hotel_values(hotel, location, profile.budget_per_night, profile_search_query)
            for hotel in hotels
        ),
        key=_estimate_hotel_cost,
    )
    for candidate in candidates:
        total_cost, within_budget = _check_budget(tour_plan, candidate, profile)
        if within_budget:
            return candidate, total_cost
    return None


def _budget_check(hotel: dict, profile: TripProfile, within_budget: bool) -> dict:
    """Surface the stay/budget comparison instead of relaxing the budget silently."""
    return {
        "budget_per_night_usd": profile.budget_per_night,
        "budget_open_ended": profile.budget_open_ended,
        "rooms": profile.rooms,
        "estimated_stay_nightly_usd": _estimate_hotel_cost(hotel),
        "stay_within_budget": within_budget,
        "status": "ESTIMATED",
        "note": (
            "Estimated from map data, not a live rate. Confirm the final payable amount for your "
            "dates, party and rooms with the booking provider before booking."
            if within_budget
            else "No stay found at or below your nightly budget in the map search; the stay shown is "
            "above it (estimated). Your budget was not changed."
        ),
    }


def _hotel_from_stay(stay) -> dict:
    return stay.model_dump() if hasattr(stay, "model_dump") else dict(stay)


@router.post("/get_tour_plan")


async def get_tour_plan(request_data: TourPlanRequestData):
    """
    GENERATE: First time -> create a day-wise plan for a destination suggested
    in this session. Two-step pattern:
    1. LLM proposes activity names, descriptions, areas, times, costs
    2. Tool enrichment: stay search, place/address/photos enrichment,
       distance calculation, budget check (all prices estimated)
    """
    city_session = CitySessionStore.get(request_data.session_id)
    if city_session is None:
        raise HTTPException(status_code=404, detail="City session not found.")
    destination = _resolve_selected_destination(city_session, request_data)
    city_name = destination.destination
    try:
        activity_session = ActivitySessionStore.get_by_city(
            session_id=request_data.session_id,
            city_name=city_name,
        )
        if activity_session is not None:
            return {
                "activity_session_id": activity_session.activity_session_id,
                "destination_id": activity_session.destination_id,
                "city": activity_session.city,
                "stay": activity_session.stay,
                "tour_plan": activity_session.tour_plan,
                "total_cost_estimate": activity_session.total_cost_estimate,
                "budget_check": activity_session.response.get("budget_check"),
                "packing_tips": activity_session.packing_tips,
                "travel_tips": activity_session.travel_tips,
                "source": "cached",
            }
        profile = build_trip_profile(_load_session_intake(city_session.intake))
        location = _location_for(destination, city_name)

        # === STEP 1: LLM proposes activities (names, descriptions, areas, times, costs only) ===
        prompt = PromptGenerator.gen_tour_plan_prompt(profile, city_name, destination)
        response = _parse_ai_response(get_ai_response(prompt))
        llm_tour_plan = response.get("tour_plan", [])
        packing_tips = response.get("packing_tips", "")
        travel_tips = response.get("travel_tips", "")

        # === STEP 2a: Tool - Find stay ===
        profile_search_query = _build_profile_search_context(profile)
        hotel = _find_hotel(location, profile.budget_per_night, profile.budget_open_ended, profile_search_query)
        hotel = _complete_hotel_values(hotel, location, profile.budget_per_night, profile_search_query)
        hotel["photos"] = _clean_photos(hotel.get("photos", []))
        hotel_address = hotel.get("address", f"City Center, {location}")

        # === STEP 2b: Tool - Enrich activities with real places, addresses & photos ===
        enriched_plan = _enrich_activities(llm_tour_plan, location, profile_search_query=profile_search_query)
        enriched_plan = _add_daily_meals(enriched_plan, hotel_address, location)
        # === STEP 2c: Tool - Calculate real distances ===
        enriched_plan = _calculate_distances(enriched_plan, hotel_address)

        # === STEP 3: Budget check (stay per room-night vs. the guest's budget) ===
        total_cost, within_budget = _check_budget(enriched_plan, hotel, profile)
        if not within_budget:
            cheaper = _cheaper_stay_within_budget(location, profile, profile_search_query, enriched_plan)
            if cheaper is not None:
                hotel, total_cost = cheaper
                within_budget = True
                hotel["photos"] = _clean_photos(hotel.get("photos", []))
                hotel_address = hotel.get("address", f"City Center, {location}")
                enriched_plan = _calculate_distances(enriched_plan, hotel_address)

        response["total_cost_estimate"] = round(total_cost, 2)
        response["budget_check"] = _budget_check(hotel, profile, within_budget)

        # === STEP 4: Store in session ===
        activity_session = ActivitySessionStore.create(
            parent_session_id=request_data.session_id,
            city_name=city_name,
            tour_plan=enriched_plan,
            response=response,
            stay_data=hotel,
            total_cost_estimate=round(total_cost, 2),
            packing_tips=packing_tips,
            travel_tips=travel_tips,
            destination_id=destination.destination_id,
        )
        return {
            **_build_final_response(
                hotel=hotel,
                tour_plan=enriched_plan,
                total_cost=total_cost,
                packing_tips=packing_tips,
                travel_tips=travel_tips,
                activity_session_id=activity_session.activity_session_id,
                city_name=city_name,
                source="generated",
            ),
            "destination_id": destination.destination_id,
            "budget_check": response["budget_check"],
        }
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error)) from error

@router.post("/regenerate_tour_plan")


async def regenerate_tour_plan(regenerate_data: RegenerateActivityInputData):
    """
    REGENERATE: different day-wise activities (same destination, new plan).
    Same two-step pattern as generate:
    1. LLM proposes new activity names/descriptions/areas/times/costs
    2. Tool enrichment: place/address/photos lookup, distance recalculation, budget re-check
    """
    activity_session = ActivitySessionStore.get(regenerate_data.activity_session_id)
    if activity_session is None:
        raise HTTPException(status_code=404, detail="Activity session not found.")
    city_session = CitySessionStore.get(activity_session.parent_session_id)
    if city_session is None:
        raise HTTPException(status_code=404, detail="Parent city session not found.")
    try:
        profile = build_trip_profile(_load_session_intake(city_session.intake))
        destination = get_destination(activity_session.destination_id) if activity_session.destination_id else None
        city_name = activity_session.city
        location = _location_for(destination, city_name)

        # === STEP 1: LLM proposes new activities ===
        prompt = PromptGenerator.regenerate_tour_plan_prompt(
            profile=profile,
            city_name=city_name,
            current_tour_plan=activity_session.tour_plan,
            day_to_regenerate=regenerate_data.day_to_regenerate,
            user_instruction=regenerate_data.user_instruction,
            destination=destination,
        )
        generated_response = _parse_ai_response(get_ai_response(prompt))
        llm_tour_plan = generated_response.get("tour_plan", [])

        # === STEP 2: Tool enrichment ===
        profile_search_query = _build_profile_search_context(profile)
        if activity_session.stay:
            hotel = _hotel_from_stay(activity_session.stay)
        else:
            hotel = _find_hotel(location, profile.budget_per_night, profile.budget_open_ended, profile_search_query)
        hotel = _complete_hotel_values(hotel, location, profile.budget_per_night, profile_search_query)
        hotel["photos"] = _clean_photos(hotel.get("photos", []))
        hotel_address = hotel.get("address", f"City Center, {location}")
        enriched_plan = _enrich_activities(
            llm_tour_plan,
            location,
            existing_plan=activity_session.tour_plan,
            day_to_regenerate=regenerate_data.day_to_regenerate,
            profile_search_query=profile_search_query,
        )
        enriched_plan = _add_daily_meals(enriched_plan, hotel_address, location)
        enriched_plan = _calculate_distances(enriched_plan, hotel_address)

        def full_plan(plan: list) -> list:
            if regenerate_data.day_to_regenerate is None:
                return plan
            return _merge_tour_plan_days(activity_session.tour_plan, plan)

        full_tour_plan = full_plan(enriched_plan)
        total_cost, within_budget = _check_budget(full_tour_plan, hotel, profile)
        if not within_budget:
            cheaper = _cheaper_stay_within_budget(location, profile, profile_search_query, full_tour_plan)
            if cheaper is not None:
                hotel, total_cost = cheaper
                within_budget = True
                hotel["photos"] = _clean_photos(hotel.get("photos", []))
                hotel_address = hotel.get("address", f"City Center, {location}")
                enriched_plan = _calculate_distances(enriched_plan, hotel_address)
                full_tour_plan = full_plan(enriched_plan)

        generated_response["total_cost_estimate"] = round(total_cost, 2)
        generated_response["tour_plan"] = full_tour_plan
        response = _merge_regenerated_field(
            previous_response=activity_session.response,
            generated_response=generated_response,
            update_field_name="tour_plan",
        )
        response["total_cost_estimate"] = round(total_cost, 2)
        response["budget_check"] = _budget_check(hotel, profile, within_budget)
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
        return {
            **_build_final_response(
                hotel=hotel,
                tour_plan=full_tour_plan,
                total_cost=total_cost,
                packing_tips=activity_session.packing_tips,
                travel_tips=activity_session.travel_tips,
                activity_session_id=updated_session.activity_session_id,
                city_name=city_name,
                source="regenerated",
            ),
            "destination_id": activity_session.destination_id,
            "budget_check": response["budget_check"],
        }
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error)) from error

# ==================== SESSION MANAGEMENT ====================

@router.get("/session/{session_id}")


async def get_session_details(session_id: str):
    """Get full session details: intake answers, suggestions, and regeneration history."""
    session = CitySessionStore.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    return {
        "session_id": session.session_id,
        "intake": session.intake,
        "match_status": session.response.get("match_status"),
        "suggested_cities": [city.model_dump() for city in session.suggested_cities],
        "shown_destination_ids": session.shown_destination_ids,
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
        "destination_id": session.destination_id,
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
