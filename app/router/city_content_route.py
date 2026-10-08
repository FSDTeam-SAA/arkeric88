from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import ValidationError

from src.core.prompt_templete import PromptGenerator
from src.service.chat_services import get_ai_response
from app.schemas.city_body import (
    RegenerateInputData,
    RegenerateActivityInputData,
    HotelPrebookInput,
    HotelRateRefreshInput,
    TourPlanRequestData,
)
from app.schemas.intake_schema import (
    INTAKE_FORM_ID,
    INTAKE_FORM_VERSION,
    STORED_INTAKE_CONTEXT,
    TravelIntakeRequest,
)
from src.core.data_processor import ProcessData
from src.session.city_session_store import CitySessionStore, ActivitySessionStore
from src.tools.tools import (
    compute_drive_route,
    get_cityinfo,
    get_detailed_tourist_places,
    get_google_hotels_sorted_by_rating,
    get_nearby_restaurants,
)
from src.core.image_registry import image_registry
from src.core.destination_catalog import (
    Destination,
    get_catalog_version,
    get_destination,
    load_destination_candidates,
)
from src.core.destination_places import lookup_destination_place, lookup_missing_coordinates, lookup_name
from src.core.destination_matching import (
    build_no_valid_result,
    build_suggestion,
    rank_destinations,
    select_diverse,
)
from src.core.feeling_block import assess_feeling_block, revision_note
from src.core.geography import address_in_country, same_country
from src.core.guest_text import clean as guest_clean, dedupe, is_generic_reason, price_indication
from src.core.itinerary_geo import (
    EXPERIENCE,
    MEAL,
    add_free_time,
    apply_transfer_day,
    describe_meal,
    describe_stay,
    enforce_day,
    is_transfer_day,
    normalize_stops,
    stop_for_day,
    transfer_item,
)
from src.core.itinerary_pricing import build_price_breakdown, meal_cost
from src.core.itinerary_validation import booking_status, validate_itinerary
from src.core.viator_match import attach_viator_products
from src.core.hotel_rates import (
    HotelRateRequest,
    HotelProviderError,
    build_occupancies,
    enrich_hotel_with_live_rates,
    get_liteapi_provider,
    request_from_profile,
)
from src.core.travel_time import (
    FALLBACK_ROAD_FACTOR,
    FALLBACK_SPEED_KMH,
    MAX_LEG_MINUTES,
    SOURCE_ESTIMATE,
    TravelTimes,
    point_of,
    straight_line_km,
)
from src.core.intake_mappings import (
    INTAKE_MAPPING_VERSION,
    SCORING_VERSION,
    TRIP_GOAL_FEELING_WORDS,
    TRIP_PACE_LABELS,
)
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


def _bias_args(bias: Optional[dict]) -> dict:
    """Location-bias arguments for a map search, only when a point is known."""
    if not bias or bias.get("latitude") is None or bias.get("longitude") is None:
        return {}
    return {"latitude": bias["latitude"], "longitude": bias["longitude"], "radius_m": bias.get("radius_m")}


def _find_hotel(
    location: str,
    nightly_budget: float,
    budget_open_ended: bool,
    profile_search_query: str = "",
    bias: Optional[dict] = None,
) -> dict:
    """
    Pick a stay from a live Google Places search: the best-rated result whose
    estimated nightly cost fits the per-room budget, else the cheapest result.
    Places has no bookable rates, so every price here is an estimate and
    availability is never claimed (IMPORT_RULES.csv "Booking search and
    availability"). No historical property sheet is used. `bias` keeps the
    search around the stop's base area.
    """
    try:
        hotels = get_google_hotels_sorted_by_rating.invoke({
            "location_name": location,
            "search_query": profile_search_query or None,
            **_bias_args(bias),
        })
        if not hotels or "error" in hotels[0]:
            return _fallback_hotel(location)
        eligible = hotels if budget_open_ended else [hotel for hotel in hotels if _estimate_hotel_cost(hotel) <= nightly_budget]
        selected = eligible[0] if eligible else min(hotels, key=_estimate_hotel_cost)
        # Retain only profile-filtered, geographically biased Google results
        # as controlled live-rate fallbacks.  They are never LLM inventions
        # and the original selected property is excluded before retrying.
        selected = dict(selected)
        selected["_rate_fallback_candidates"] = [
            dict(hotel) for hotel in eligible if _place_key(hotel) != _place_key(selected)
        ]
        return selected
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
        "PRICE_LEVEL_VERY_EXPENSIVE": 400,
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
        # No real stay was found: never shown as a recommendation, never priced.
        "is_fallback": True,
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
    # A price inferred from rating and budget is not real pricing data: the
    # breakdown withholds its total instead of presenting the guess.
    completed.setdefault(
        "price_inferred",
        (not price_level or price_level == "NOT_AVAILABLE") and not completed.get("average_nightly_price"),
    )
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
    "Opening hours don't confirm tickets or availability, so please confirm time-sensitive experiences with the operator."
)
_UNVERIFIED_PLACE_NOTE = "We couldn't confirm this place on the map; please check the details before you go."


def _is_open_business(place: dict) -> bool:
    return place.get("business_status") not in _CLOSED_BUSINESS_STATUSES


def _usable_place(place: dict, location: str) -> bool:
    """
    Open, and inside the destination's country. Text search can return a
    same-sounding place abroad (e.g. a Serbian fortress for "fortress walls
    viewpoint in Cartagena, Colombia"), so the address must name the country
    when the location carries one ("City, Country").
    """
    if not _is_open_business(place):
        return False
    if "," not in (location or ""):
        return True
    return address_in_country(place.get("address"), location.rsplit(",", 1)[1])


def _fetch_attraction_dataset(city_name: str, profile_search_query: str = "", bias: Optional[dict] = None) -> list[dict]:
    """Fetch all relevant attractions once per stop, biased towards the stop's base."""
    try:
        results = get_detailed_tourist_places.invoke({
            "location_name": city_name,
            "search_query": profile_search_query or None,
            **_bias_args(bias),
        })
        if not results or _is_tool_error_list(results):
            return []
        return [place for place in results if _usable_place(place, city_name)]
    except Exception:
        return []


def _activity_is_meal(activity: dict) -> bool:
    if activity.get("item_type"):
        return activity["item_type"] == MEAL
    # Raw model drafts carry no item_type; meals are recognised by name.
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
    bias: Optional[dict] = None,
    attraction_dataset: Optional[list] = None,
    used_place_keys: Optional[set] = None,
) -> list:
    """
    Match each activity to a real place: first from one shared, profile-led
    dataset, then with a targeted search for that activity. Matched places keep
    their coordinates so travel times can be measured. Activities with no
    genuine match keep their planned name and are marked unverified.
    """
    if attraction_dataset is None:
        attraction_dataset = _fetch_attraction_dataset(city_name, profile_search_query, bias)
    if used_place_keys is None:
        used_place_keys = _seed_used_place_keys(existing_plan, day_to_regenerate)
    # "Ubud, Indonesia" -> {"ubud", "indonesia"}: sharing only these is not a match.
    location_tokens = frozenset(_normalize_place_text(city_name).split())
    enriched_plan = []
    for day in tour_plan:
        enriched_day = {
            "day": day.get("day"),
            "stop": day.get("stop", 1),
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
            coords = {}
            availability_note = _UNVERIFIED_PLACE_NOTE
            best_match = _find_best_match(
                attraction_dataset,
                activity_name,
                activity_location,
                used_place_keys=used_place_keys,
                ignore_tokens=location_tokens,
            )
            if best_match is None and activity_name:
                best_match = _find_best_match(
                    _search_activity_place(activity_name, city_name, bias),
                    activity_name,
                    activity_location,
                    used_place_keys=used_place_keys,
                    ignore_tokens=location_tokens,
                )
            if best_match:
                verified_name = best_match.get("name", activity_name)
                verified_address = best_match.get("address", "N/A")
                verified_photos = _clean_photos(best_match.get("photos", []))
                place_id = best_match.get("place_id")
                business_status = best_match.get("business_status")
                coords = best_match.get("coords") or {}
                # Shown once for the whole itinerary instead of on every activity.
                availability_note = ""
                match_key = _place_key(best_match)
                if match_key:
                    used_place_keys.add(match_key)
            enriched_activity = {
                "item_type": EXPERIENCE,
                "activity_name": verified_name,
                "activity_description": activity.get("activity_description", ""),
                "activity_location": activity.get("activity_location", ""),
                "activity_address": verified_address,
                "activity_image": verified_photos,
                "activity_time": activity.get("activity_time", ""),
                "activity_cost": activity.get("activity_cost", 0),
                "why_selected": str(activity.get("why_selected") or "").strip(),
                "latitude": coords.get("lat"),
                "longitude": coords.get("lng"),
                "distance_from_previous_km": None,
                "place_id": place_id,
                "business_status": business_status,
                "availability_note": availability_note,
            }
            enriched_day["activities"].append(enriched_activity)
        enriched_plan.append(enriched_day)
    return enriched_plan


# Words that never make a place "the same" as an activity on their own.
_GENERIC_PLACE_TOKENS = {
    "a", "an", "and", "at", "by", "for", "from", "in", "of", "on", "the", "to", "with",
    "visit", "tour", "trip", "experience", "session", "time", "morning", "afternoon",
    "evening", "day", "local", "quiet", "gentle", "guided",
}


# A place whose name says it is somewhere to stay is not an activity venue,
# unless the activity itself is about that kind of place.
_LODGING_TOKENS = {
    "villa", "villas", "hotel", "hotels", "resort", "hostel", "apartment", "apartments",
    "homestay", "guesthouse", "bnb", "suites", "bungalow", "bungalows", "1br", "2br",
}


def _meaningful_tokens(text: str, ignore_tokens: frozenset = frozenset()) -> set:
    return {
        token for token in _normalize_place_text(text).split()
        if token not in _GENERIC_PLACE_TOKENS and token not in ignore_tokens and len(token) > 1
    }


def _find_best_match(
    results: list,
    activity_name: str,
    location_hint: str = "",
    used_place_keys: set[str] | None = None,
    ignore_tokens: frozenset = frozenset(),
) -> dict | None:
    """
    Best place for an activity, preferring places not used yet. A place only
    counts as a match when its name shares a meaningful word with the
    activity (destination names and filler words don't count); otherwise
    None is returned, so an activity is never renamed to an unrelated place.
    """
    if not results:
        return None
    used_place_keys = used_place_keys or set()
    activity_lower = _normalize_place_text(activity_name)
    location_lower = _normalize_place_text(location_hint)
    activity_tokens = _meaningful_tokens(activity_name, ignore_tokens)
    # One shared word is enough for a one-word activity ("Museum"), but a
    # multi-word activity needs two: "Tirta Empul water temple" must not
    # match "Tirta Ayu Spa" on "tirta" alone.
    required_overlap = min(2, len(activity_tokens))
    scored = []
    for place in results:
        key = _place_key(place)
        name = _normalize_place_text(place.get("name", ""))
        address = _normalize_place_text(place.get("address", ""))
        if (set(name.split()) & _LODGING_TOKENS) and not (activity_tokens & _LODGING_TOKENS):
            continue  # A rental or hotel listing, not the venue for this activity.
        overlap = len(activity_tokens & _meaningful_tokens(name, ignore_tokens))
        if activity_lower == name:
            score = 100
        elif overlap and overlap >= required_overlap:
            score = overlap * 6
            if activity_lower and activity_lower in name:
                score += 50
            elif name and name in activity_lower:
                score += 35
        else:
            continue  # Unrelated place: never a match.
        if location_lower and (location_lower in address or location_lower in name):
            score += 20
        scored.append((key in used_place_keys, score, place))
    unused_matches = [item for item in scored if not item[0]]
    candidates = unused_matches or scored
    candidates.sort(key=lambda item: item[1], reverse=True)
    return candidates[0][2] if candidates else None


def _search_activity_place(activity_name: str, location: str, bias: Optional[dict] = None) -> list:
    """Targeted map search for one activity when the shared dataset has no match."""
    try:
        results = get_detailed_tourist_places.invoke({
            "location_name": location,
            "search_query": activity_name,
            **_bias_args(bias),
        })
    except Exception:
        return []
    if not results or _is_tool_error_list(results):
        return []
    return [place for place in results if _usable_place(place, location)]


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


def _choose_anchor(activities: list, meal_name: str, hotel_address: str, city_name: str, hotel_point=None) -> dict:
    """
    Where a meal should be: near the first activity for breakfast, the one
    closest to noon for lunch, the last one for dinner -- or the hotel.
    Returns {"text", "point", "name"} so the search can be biased to the spot.
    """
    real_activities = [activity for activity in activities if not _activity_is_meal(activity)]
    hotel = {"text": hotel_address or city_name, "point": hotel_point, "name": "your hotel"}
    if not real_activities:
        return hotel
    if meal_name == "Breakfast":
        anchor = real_activities[0]
    elif meal_name == "Dinner":
        anchor = real_activities[-1]
    else:
        anchor = min(
            real_activities,
            key=lambda activity: abs((_start_minutes(activity.get("activity_time", "")) or 720) - 720),
        )
    text = (
        anchor.get("activity_address")
        if anchor.get("activity_address") and anchor.get("activity_address") != "N/A"
        else anchor.get("activity_location")
        or hotel_address
        or city_name
    )
    return {"text": text, "point": point_of(anchor) or hotel_point, "name": anchor.get("activity_name") or "your hotel"}


# Search radius around the meal's anchor, and the dining price levels that
# suit each lodging budget (pricier places are ranked last, not removed).
MEAL_SEARCH_RADIUS_M = 3000.0
_HIGH_DINING_LEVELS = {"PRICE_LEVEL_EXPENSIVE", "PRICE_LEVEL_VERY_EXPENSIVE"}
_DIETARY_TERMS = (
    "vegetarian", "vegan", "gluten-free", "gluten free", "halal", "kosher",
    "dairy-free", "dairy free", "nut-free", "nut free", "pescatarian",
)


def _dining_context(profile: TripProfile) -> dict:
    """
    Dining search terms from the guest's answers: named dietary needs (only
    recognised keywords from the restriction notes, never the free text
    itself), food interest, party and budget.
    """
    dietary = []
    if "food_dietary" in profile.restrictions and profile.restriction_notes:
        notes = profile.restriction_notes.lower()
        dietary = list(dict.fromkeys(term.replace(" ", "-") for term in _DIETARY_TERMS if term in notes))
    terms = [*dietary]
    if "food_drinks" in profile.moments:
        terms.append("local favourite")
    if profile.children:
        terms.append("family-friendly")
    budget_minded = not profile.budget_open_ended and profile.budget_per_night <= 150
    if budget_minded:
        terms.append("casual")
    return {"query": " ".join(terms), "dietary": dietary, "budget_minded": budget_minded}


def _find_restaurant_for_meal(
    anchor_location: str,
    meal_name: str,
    used_restaurant_keys: set[str],
    city_name: str,
    anchor_point=None,
    dining: Optional[dict] = None,
) -> Optional[dict]:
    """Best unused, open restaurant near the anchor, or None when the search finds nothing usable."""
    dining = dining or {}
    search_location = anchor_location or city_name
    if "," in city_name and not address_in_country(search_location, city_name.rsplit(",", 1)[1]):
        # An anchor like "Old City walls" alone can match anywhere in the world;
        # keep the search inside the destination.
        search_location = f"{search_location}, {city_name}"
    args = {"location_name": search_location, "meal_type": meal_name.lower()}
    if dining.get("query"):
        args["search_query"] = dining["query"]
    if anchor_point is not None:
        args.update(_bias_args({"latitude": anchor_point[0], "longitude": anchor_point[1], "radius_m": MEAL_SEARCH_RADIUS_M}))
    try:
        restaurants = get_nearby_restaurants.invoke(args)
    except Exception:
        return None
    if not restaurants or _is_tool_error_list(restaurants):
        return None
    restaurants = [restaurant for restaurant in restaurants if _usable_place(restaurant, city_name)]
    if dining.get("budget_minded"):
        # Stable sort: keeps Google's rating order within each price group.
        restaurants.sort(key=lambda restaurant: restaurant.get("price_level") in _HIGH_DINING_LEVELS)
    for restaurant in restaurants:
        key = _place_key(restaurant)
        if key and key not in used_restaurant_keys:
            used_restaurant_keys.add(key)
            return restaurant
    if restaurants:
        restaurant = restaurants[0]
        key = _place_key(restaurant)
        if key:
            used_restaurant_keys.add(key)
        return restaurant
    return None


def _meal_activity(meal_name: str, restaurant: Optional[dict], anchor: dict) -> dict:
    """A meal at a real restaurant, or an intentionally open meal slot near the anchor."""
    time_window, _base_cost = _MEAL_SCHEDULE[meal_name]
    if restaurant is None:
        return {
            "item_type": MEAL,
            "meal": meal_name,
            "open_slot": True,
            "activity_name": f"{meal_name} near {anchor['name']}",
            "activity_description": f"Left open so you can choose somewhere you like near {anchor['name']}.",
            "activity_location": anchor["text"],
            "activity_address": "N/A",
            "activity_image": [],
            "activity_time": time_window,
            "activity_cost": meal_cost(meal_name),
            "why_selected": (
                f"We didn't find a restaurant we could confirm near {anchor['name']}, "
                f"so your {meal_name.lower()} is left flexible."
            ),
            "latitude": None,
            "longitude": None,
            "distance_from_previous_km": None,
            "place_id": None,
            "business_status": None,
            "availability_note": "",
        }
    restaurant_name = restaurant.get("name") or f"{meal_name} stop"
    coords = restaurant.get("coords") or {}
    price_level = restaurant.get("price_level")
    return {
        "item_type": MEAL,
        "meal": meal_name,
        "open_slot": False,
        "activity_name": f"{meal_name} at {restaurant_name}",
        "activity_description": f"{meal_name} at {restaurant_name}.",
        "activity_location": restaurant_name,
        "restaurant_name": restaurant_name,
        "activity_address": restaurant.get("address") or anchor["text"] or "N/A",
        "activity_image": _clean_photos(restaurant.get("photos", [])),
        "activity_time": time_window,
        "activity_cost": meal_cost(meal_name, price_level),
        "rating": restaurant.get("rating") or None,
        # Guests see the label only; the raw Google code is used for the cost above and dropped.
        "price_indication": price_indication(price_level),
        "why_selected": "",
        "latitude": coords.get("lat"),
        "longitude": coords.get("lng"),
        "distance_from_previous_km": None,
        "place_id": restaurant.get("place_id"),
        "business_status": restaurant.get("business_status"),
        "availability_note": "",
    }


def _time_window(time_range: str) -> tuple | None:
    """(start, end) in minutes for "HH:MM AM - HH:MM PM", or None if it can't be read."""
    times = re.findall(r"\d{1,2}:\d{2}\s*[AP]M", time_range or "", re.IGNORECASE)
    if len(times) < 2:
        return None
    start, end = _start_minutes(times[0]), _start_minutes(times[1])
    if start is None or end is None or end <= start:
        return None
    return start, end


def _overlaps_an_activity(meal_window: str, activities: list) -> bool:
    meal = _time_window(meal_window)
    if meal is None:
        return False
    for activity in activities:
        window = _time_window(activity.get("activity_time", ""))
        if window is not None and window[0] < meal[1] and meal[0] < window[1]:
            return True
    return False


def _add_daily_meals(
    tour_plan: list,
    hotel_address: str,
    city_name: str,
    hotel_point=None,
    dining: Optional[dict] = None,
    used_restaurant_keys: Optional[set] = None,
) -> list:
    used_restaurant_keys = set() if used_restaurant_keys is None else used_restaurant_keys
    for day in tour_plan:
        activities = [activity for activity in day.get("activities", []) if not _activity_is_meal(activity)]
        meal_activities = []
        for meal_name, (meal_window, _cost) in _MEAL_SCHEDULE.items():
            if _overlaps_an_activity(meal_window, activities):
                continue  # An activity already fills this time; don't double-book the guest.
            anchor = _choose_anchor(activities, meal_name, hotel_address, city_name, hotel_point)
            restaurant = _find_restaurant_for_meal(
                anchor["text"], meal_name, used_restaurant_keys, city_name, anchor["point"], dining,
            )
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


# ==================== ITINERARY BUILD: STOPS, GEOGRAPHY, VALIDATION ====================
#
# Order of work for every itinerary (client brief, Priorities 1-5):
#   1. Stops: the model proposes one base area (or, for long trips that need
#      another region, a few stops); normalize_stops enforces the rules.
#   2. A real stay per stop; its coordinates are the stop's base.
#   3. Experiences and restaurants are searched around the base and keep
#      their coordinates.
#   4. Every item is checked against MAX_LEG_MINUTES from the base and the
#      previous item (Google Routes); too-far items are replaced or removed.
#      A new stop starts with a transfer and a lighter day.
#   5. Specific reasons, intentional free time, a price breakdown from the
#      visible items, then the final validation and booking status.

# Hotel search radius around a stop's base area, and the radius for its experiences.
STAY_SEARCH_RADIUS_M = 15000.0
STOP_SEARCH_RADIUS_M = 30000.0
# A replacement further than this in a straight line cannot be within the
# travel limit by road, so no route is requested for it.
_REPLACEMENT_MAX_KM = MAX_LEG_MINUTES / 60 * FALLBACK_SPEED_KMH / FALLBACK_ROAD_FACTOR


def _base_names(destination: Optional[Destination]) -> set:
    if destination is None:
        return set()
    return {destination.destination.lower(), lookup_name(destination.destination).lower()}


def _stop_location(base_area: str, destination: Optional[Destination], city_name: str) -> str:
    """Map-search location for a stop: "Base area, Destination, Country"."""
    if destination is None:
        return base_area if base_area.lower() == city_name.lower() else f"{base_area}, {city_name}"
    if base_area.lower() in _base_names(destination):
        return f"{destination.destination}, {destination.country}"
    return f"{base_area}, {destination.destination}, {destination.country}"


def _area_point(base_area: str, destination: Optional[Destination]) -> Optional[tuple]:
    """Coordinates of a base area, country-checked; the catalog point when the base is the destination itself."""
    if (
        destination is not None
        and base_area.lower() in _base_names(destination)
        and destination.latitude is not None
        and destination.longitude is not None
    ):
        return destination.latitude, destination.longitude
    country = destination.country if destination else None
    try:
        result = get_cityinfo.invoke({"city_name": base_area, "region_hint": country})
    except Exception:
        return None
    if not isinstance(result, dict) or "error" in result or result.get("lat") is None or result.get("lng") is None:
        return None
    if country and not same_country(result.get("country"), country):
        return None
    return float(result["lat"]), float(result["lng"])


def _nightly_usd(hotel: dict) -> Optional[float]:
    """Estimated nightly rate from real map data, or None when there is no basis for one."""
    if hotel.get("is_fallback") or hotel.get("price_inferred"):
        return None
    return float(_estimate_hotel_cost(hotel))


def _plan_stops(
    raw_stops,
    profile: TripProfile,
    destination: Optional[Destination],
    city_name: str,
    profile_search_query: str,
    hotel_cache: dict,
) -> list:
    """Normalize the model's stops and give each one a real stay; the stay's location is the base."""
    default_area = lookup_name(destination.destination) if destination else city_name
    stops = normalize_stops(raw_stops, profile.nights, default_area)
    for stop in stops:
        key = stop["base_area"].lower()
        if key not in hotel_cache:
            area_point = _area_point(stop["base_area"], destination)
            location = _stop_location(stop["base_area"], destination, city_name)
            bias = (
                {"latitude": area_point[0], "longitude": area_point[1], "radius_m": STAY_SEARCH_RADIUS_M}
                if area_point else None
            )
            hotel = _find_hotel(location, profile.budget_per_night, profile.budget_open_ended, profile_search_query, bias)
            hotel = _complete_hotel_values(hotel, location, profile.budget_per_night, profile_search_query)
            hotel["photos"] = _clean_photos(hotel.get("photos", []))
            hotel_cache[key] = (hotel, area_point, location)
        hotel, area_point, location = hotel_cache[key]
        hotel = dict(hotel)
        stay_point = point_of(hotel)
        base_point = stay_point or area_point
        stop.update(
            location=location,
            hotel=hotel,
            stay_found=not hotel.get("is_fallback"),
            nightly_usd=_nightly_usd(hotel),
            base={
                "latitude": base_point[0] if base_point else None,
                "longitude": base_point[1] if base_point else None,
                "source": "stay" if stay_point else ("base_area" if area_point else None),
            },
        )
    return stops


def _enrich_stops_with_live_rates(stops: list, profile: TripProfile, request: Optional[HotelRateRequest] = None) -> list:
    """Enrich Google-selected stays without replacing their emotional/geographic fit."""
    try:
        request = request or request_from_profile(profile)
    except ValueError as error:
        for stop in stops:
            stop["hotel"].setdefault("live_rate_status", "NOT_REQUESTED")
            stop["hotel"].setdefault("live_rate_note", str(error))
        return stops
    provider = get_liteapi_provider()
    for stop in stops:
        hotel = enrich_hotel_with_live_rates(stop["hotel"], request, provider)
        if hotel.get("live_rate_status") in {"UNAVAILABLE", "UNMATCHED"}:
            attempted = {_place_key(hotel)}
            for candidate in hotel.get("_rate_fallback_candidates", []):
                candidate_key = _place_key(candidate)
                if not candidate_key or candidate_key in attempted:
                    continue
                attempted.add(candidate_key)
                fallback = enrich_hotel_with_live_rates(candidate, request, provider)
                if fallback.get("price_status") == "LIVE":
                    fallback["rate_fallback_from"] = hotel.get("name")
                    fallback["rate_fallback_reason"] = (
                        "The first emotionally matched stay was not available for these dates; "
                        "this nearby, profile-filtered alternative has live availability."
                    )
                    hotel = fallback
                    break
        stop["hotel"] = hotel
        if hotel.get("price_status") == "LIVE" and hotel.get("live_total") is not None:
            stop["nightly_usd"] = hotel.get("nightly_usd")
            stop["live_total"] = hotel["live_total"]
    return stops


def _fallback_experience_reason(item: dict, profile: TripProfile) -> str:
    """A reason tied to the guest's own answers when the model gave none worth showing."""
    feeling = TRIP_GOAL_FEELING_WORDS[profile.goals[0]].lower()
    moment = profile.moment_labels[0].lower() if profile.moment_labels else None
    enjoy = f", with time for the {moment} you said you enjoy" if moment else ", at a pace that suits you"
    return f"{item.get('activity_name', 'This experience')} gives you room to feel {feeling}{enjoy}."


def _replacement_finder(stop: dict, dataset: list, used_place_keys: set, used_restaurant_keys: set,
                        dining: dict, profile: TripProfile):
    """Closer alternatives for items over the travel limit (see itinerary_geo.enforce_day)."""
    feeling = TRIP_GOAL_FEELING_WORDS[profile.goals[0]].lower()

    def experience(item: dict, base: tuple, previous: tuple) -> Optional[dict]:
        for place in dataset:
            point = point_of(place)
            key = _place_key(place)
            if point is None or (key and key in used_place_keys):
                continue
            if straight_line_km(base, point) > _REPLACEMENT_MAX_KM or straight_line_km(previous, point) > _REPLACEMENT_MAX_KM:
                continue
            if key:
                used_place_keys.add(key)
            name = place.get("name", "")
            return {
                "item_type": EXPERIENCE,
                "activity_name": name,
                "activity_description": f"An unhurried visit to {name}, close to your base in {stop['base_area']}.",
                "activity_location": stop["base_area"],
                "activity_address": place.get("address", "N/A"),
                "activity_image": _clean_photos(place.get("photos", [])),
                "activity_time": item.get("activity_time", ""),
                "activity_cost": item.get("activity_cost", 0),
                "why_selected": (
                    f"Swapped in for {item.get('activity_name', 'the original plan')}, which is too far from your "
                    f"base, and it still gives you room to feel {feeling}."
                ),
                "latitude": point[0],
                "longitude": point[1],
                "distance_from_previous_km": None,
                "place_id": place.get("place_id"),
                "business_status": place.get("business_status"),
                "availability_note": "",
            }
        return None

    def meal(item: dict, base: tuple, previous: tuple) -> Optional[dict]:
        meal_name = item.get("meal") or "Lunch"
        anchor = {"text": stop["location"], "point": previous, "name": stop["base_area"]}
        restaurant = _find_restaurant_for_meal(
            stop["location"], meal_name, used_restaurant_keys, stop["location"], previous, dining,
        )
        point = point_of(restaurant) if restaurant else None
        if point is not None and straight_line_km(previous, point) > _REPLACEMENT_MAX_KM:
            restaurant = None
        return _meal_activity(meal_name, restaurant, anchor)

    return lambda item, base, previous: meal(item, base, previous) if item.get("item_type") == MEAL \
        else experience(item, base, previous)


def _describe_items(day: dict, profile: TripProfile, dining: dict) -> None:
    """Every recommendation gets a specific, guest-facing reason; text is cleaned of backend markers."""
    activities = day.get("activities", [])
    for index, item in enumerate(activities):
        kind = item.get("item_type", EXPERIENCE)
        item["activity_description"] = guest_clean(item.get("activity_description"))
        if kind == EXPERIENCE:
            reason = guest_clean(item.get("why_selected"))
            item["why_selected"] = _fallback_experience_reason(item, profile) if is_generic_reason(reason) else reason
        elif kind == MEAL and not item.get("open_slot"):
            next_name = next(
                (later.get("activity_name") for later in activities[index + 1:] if later.get("item_type") == EXPERIENCE),
                None,
            )
            item["why_selected"] = describe_meal(item, item.get("meal") or "Lunch", next_name, dining.get("dietary", []))


def _seed_used_restaurant_keys(existing_plan: Optional[list], exclude_day: Optional[int] = None) -> set:
    """Restaurants already on the other days, so a regenerated day doesn't repeat them."""
    keys = set()
    for day in _dump_tour_plan(existing_plan or []):
        if exclude_day is not None and day.get("day") == exclude_day:
            continue
        for item in day.get("activities", []):
            if item.get("item_type") == MEAL and item.get("restaurant_name"):
                key = _place_key({"name": item["restaurant_name"], "address": item.get("activity_address", "")})
                if key:
                    keys.add(key)
    return keys


def _build_days(
    llm_days: list,
    stops: list,
    profile: TripProfile,
    profile_search_query: str,
    travel: TravelTimes,
    existing_plan: Optional[list] = None,
    day_to_regenerate: Optional[int] = None,
) -> tuple:
    """Build the given days stop by stop. Returns (days, adjustments)."""
    dining = _dining_context(profile)
    used_place_keys = _seed_used_place_keys(existing_plan, day_to_regenerate)
    used_restaurant_keys = _seed_used_restaurant_keys(existing_plan, day_to_regenerate)
    pace_phrase = TRIP_PACE_LABELS[profile.pace].lower()
    valid_days = [day for day in llm_days or [] if isinstance(day, dict) and str(day.get("day", "")).isdigit()]
    built, adjustments = [], []
    for stop in stops:
        stop_days = [
            {**day, "day": int(day["day"]), "stop": stop["stop"]}
            for day in valid_days
            if stop_for_day(stops, int(day["day"]))["stop"] == stop["stop"]
        ]
        if not stop_days:
            continue
        base = point_of(stop["base"])
        bias = {"latitude": base[0], "longitude": base[1], "radius_m": STOP_SEARCH_RADIUS_M} if base else None
        dataset = _fetch_attraction_dataset(stop["location"], profile_search_query, bias)
        days = _enrich_activities(
            stop_days, stop["location"], profile_search_query=profile_search_query, bias=bias,
            attraction_dataset=dataset, used_place_keys=used_place_keys,
        )
        replace = _replacement_finder(stop, dataset, used_place_keys, used_restaurant_keys, dining, profile)
        # Experiences first, so meals are anchored to the experiences that stay in the plan.
        for day in days:
            adjustments.extend(enforce_day(day, stop, travel, replace))
        days = _add_daily_meals(
            days, stop["hotel"].get("address") or stop["location"], stop["location"], base, dining, used_restaurant_keys,
        )
        for day in days:
            day["day_type"] = "standard"
            if is_transfer_day(stops, day["day"]):
                previous_stop = stops[stop["stop"] - 2]
                adjustments.extend(apply_transfer_day(day, transfer_item(previous_stop, stop, compute_drive_route)))
            # Final pass over the day as it now stands: meals checked and every travel time re-chained.
            adjustments.extend(enforce_day(day, stop, travel, replace))
            _describe_items(day, profile, dining)
            add_free_time(day, pace_phrase)
            built.append(day)
    built.sort(key=lambda day: day["day"])
    return built, adjustments


def _travel_dates(profile: TripProfile) -> Optional[dict]:
    """Day number -> calendar date, when the guest has exact dates."""
    if not profile.has_exact_dates:
        return None
    return {day: profile.check_in_date + timedelta(days=day - 1) for day in range(1, profile.nights + 1)}


def _destination_verified(destination: Optional[Destination]) -> bool:
    if destination is None:
        return False
    return destination.candidate_status != "CANDIDATE_ONLY" and "REQUIRED" not in destination.source_review_status


_FERRY_NOTE = "Your transfer includes a ferry crossing: check sailing times and book ahead in busy seasons."
_ESTIMATED_TRAVEL_NOTE = "Some travel times are estimates, so allow a little extra time between plans."


def _finalize_itinerary(stops: list, days: list, profile: TripProfile, destination: Optional[Destination],
                        adjustments: list) -> dict:
    """Stay reasons, price breakdown, final validation, booking status and guest notes (each once)."""
    for stop in stops:
        stop["hotel"]["why_selected"] = describe_stay(stop, days)
    pricing = build_price_breakdown(stops, days, profile.party_size, profile.rooms, profile.nights)
    validation = validate_itinerary(stops, days, pricing)
    booking = booking_status(validation, stops, days, _destination_verified(destination))
    items = [item for day in days for item in day.get("activities", [])]
    # Only changes to planned experiences are worth a guest note; restaurant swaps are ours to make.
    notes = [adjustment["guest_note"] for adjustment in adjustments if adjustment.get("guest_note")]
    if any(item.get("includes_ferry") for item in items):
        notes.append(_FERRY_NOTE)
    if any(item.get("item_type", EXPERIENCE) == EXPERIENCE and item.get("place_id") for item in items):
        notes.append(_PLACE_AVAILABILITY_NOTE)
    if any(item.get("travel_time_source") == SOURCE_ESTIMATE for item in items):
        notes.append(_ESTIMATED_TRAVEL_NOTE)
    return {
        "price_breakdown": pricing,
        "total_cost_estimate": pricing["total"],
        "validation": validation,
        "booking_status": booking,
        "adjustments": adjustments,
        "guest_notes": dedupe(notes),
    }


def _stays_within_budget(stops: list, profile: TripProfile) -> tuple:
    """(most expensive stay, whether every stay fits the per-room nightly budget)."""
    priciest = max((stop["hotel"] for stop in stops), key=_estimate_hotel_cost)
    within = profile.budget_open_ended or _estimate_hotel_cost(priciest) <= profile.budget_per_night
    return priciest, within


# ==================== GUEST-FACING ITINERARY ====================

_STAY_ESTIMATE_NOTE = "Rates are estimates until they are confirmed with a booking provider for your dates."


def _guest_stay(stop: dict) -> dict:
    """The stay as a guest sees it: no internal codes, no invented facilities, estimates labelled once."""
    hotel = stop.get("hotel") or {}
    label = None if hotel.get("price_inferred") or hotel.get("is_fallback") else price_indication(hotel.get("price_level"))
    nightly = stop.get("nightly_usd")
    website = hotel.get("website") or ""
    return {
        "name": guest_clean(hotel.get("name", "N/A")),
        "address": guest_clean(hotel.get("address", "N/A")),
        "rating": float(hotel.get("rating") or 0.0),
        "price_level": label or "",
        "price_indication": label,
        "photos": _clean_photos(hotel.get("photos", [])),
        "coords": hotel.get("coords"),
        "average_nightly_price": (
            f"{hotel.get('live_currency', 'USD')} {nightly:,.2f} per room, per night"
            if hotel.get("price_status") == "LIVE" and nightly is not None
            else (f"${nightly:,.0f} per night (estimate)" if nightly is not None else "")
        ),
        "budget_tier": "",
        "facilities": [],
        "website": "" if website == "Not available" else website,
        "estimate_note": "" if hotel.get("price_status") == "LIVE" else _STAY_ESTIMATE_NOTE,
        "price_status": hotel.get("price_status", "ESTIMATED"),
        "availability_status": hotel.get("availability_status", "NOT_CHECKED"),
        "live_rate_status": hotel.get("live_rate_status", "NOT_REQUESTED"),
        "provider": (hotel.get("live_rates") or {}).get("provider"),
        "total_price": hotel.get("live_total"),
        "currency": hotel.get("live_currency"),
        "room_offers": (hotel.get("live_rates") or {}).get("offers", []),
        "included_taxes_and_fees": ((hotel.get("live_rates") or {}).get("offers") or [{}])[0].get("included_taxes_and_fees", []),
        "payable_at_property": ((hotel.get("live_rates") or {}).get("offers") or [{}])[0].get("payable_at_property", []),
        "fee_disclosure": ((hotel.get("live_rates") or {}).get("offers") or [{}])[0].get("fee_disclosure", ""),
        "why_selected": hotel.get("why_selected", ""),
        "base_area": stop.get("base_area", ""),
        "nights": stop.get("nights"),
    }


def _guest_stops(stops: list) -> list:
    return [
        {
            "stop": stop["stop"],
            "base_area": stop["base_area"],
            "nights": stop["nights"],
            "first_day": stop["first_day"],
            "last_day": stop["last_day"],
            "stay": _guest_stay(stop),
        }
        for stop in stops
    ]


def _itinerary_payload(
    activity_session_id: str,
    destination_id: Optional[str],
    city_name: str,
    stops: list,
    tour_plan: list,
    response: dict,
    packing_tips: str,
    travel_tips: str,
    source: str,
) -> dict:
    """
    Final response. The feeling block sits right after the destination. Guests
    see: feeling_block, stops (each with its stay), tour_plan, price_breakdown,
    booking_status.guest_label and guest_notes. `validation` and
    `adjustments` explain what was checked and changed.
    """
    guest_stops = _guest_stops(stops)
    return {
        "activity_session_id": activity_session_id,
        "destination_id": destination_id,
        "city": city_name,
        "feeling_block": response.get("feeling_block"),
        "booking_status": response.get("booking_status"),
        "stay": guest_stops[0]["stay"] if guest_stops else None,
        "stops": guest_stops,
        "tour_plan": tour_plan,
        "price_breakdown": response.get("price_breakdown"),
        "total_cost_estimate": response.get("total_cost_estimate"),
        "guest_notes": response.get("guest_notes", []),
        "adjustments": response.get("adjustments", []),
        "validation": response.get("validation"),
        "budget_check": response.get("budget_check"),
        "packing_tips": guest_clean(packing_tips),
        "travel_tips": guest_clean(travel_tips),
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


def _stops_from_session(activity_session) -> list:
    """Stops for sessions saved before multi-stop itineraries: one stop around the saved stay."""
    stay = activity_session.stay.model_dump() if activity_session.stay is not None else _fallback_hotel(activity_session.city)
    point = point_of(stay)
    nights = max(len(activity_session.tour_plan), 1)
    return [{
        "stop": 1,
        "base_area": activity_session.city,
        "nights": nights,
        "first_day": 1,
        "last_day": nights,
        "location": activity_session.city,
        "hotel": stay,
        "stay_found": not stay.get("is_fallback"),
        "nightly_usd": None,
        "base": {
            "latitude": point[0] if point else None,
            "longitude": point[1] if point else None,
            "source": "stay" if point else None,
        },
    }]


def _session_payload(activity_session, source: str) -> dict:
    response = activity_session.response or {}
    return _itinerary_payload(
        activity_session.activity_session_id,
        activity_session.destination_id,
        activity_session.city,
        response.get("stops") or _stops_from_session(activity_session),
        _dump_tour_plan(activity_session.tour_plan),
        response,
        activity_session.packing_tips,
        activity_session.travel_tips,
        source,
    )


def _plan_experiences(tour_plan: list) -> list:
    """The itinerary's actual experiences (no meals, transfers or open time) for the feeling block."""
    return [
        {
            "day": day.get("day"),
            "activity_name": activity.get("activity_name", ""),
            "activity_description": activity.get("activity_description", ""),
        }
        for day in _dump_tour_plan(tour_plan)
        for activity in day.get("activities", [])
        if activity.get("item_type", EXPERIENCE) == EXPERIENCE and not _activity_is_meal(activity)
    ]


def _feeling_block_for(profile: TripProfile, destination: Optional[Destination], city_name: str, tour_plan: list) -> dict:
    return assess_feeling_block(
        profile,
        city_name,
        destination.country if destination else "",
        _plan_experiences(tour_plan),
        get_ai_response,
    )


@router.post("/get_tour_plan")


async def get_tour_plan(request_data: TourPlanRequestData):
    """
    GENERATE: First time -> create a day-wise plan for a destination suggested
    in this session:
    1. LLM proposes the base area(s) and activity names, descriptions, areas,
       times, costs and reasons
    2. A real stay per base (estimated prices only); its location is the base
    3. Experiences and restaurants matched to real places around the base
    4. Travel times from coordinates: anything over the limit is replaced or
       removed; extra stops get a transfer and a lighter day
    5. "Designed to help you feel" block, checked against the actual
       experiences; revised once if the plan does not support the feeling
    6. Price breakdown, final validation and booking status
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
            return _session_payload(activity_session, "cached")
        profile = build_trip_profile(_load_session_intake(city_session.intake))
        profile_search_query = _build_profile_search_context(profile)
        travel = TravelTimes()
        hotel_cache: dict = {}

        def draft_itinerary(revision: str = "") -> tuple:
            prompt = PromptGenerator.gen_tour_plan_prompt(profile, city_name, destination, revision)
            draft = _parse_ai_response(get_ai_response(prompt))
            stops = _plan_stops(draft.get("stops"), profile, destination, city_name, profile_search_query, hotel_cache)
            stops = _enrich_stops_with_live_rates(stops, profile)
            days, adjustments = _build_days(draft.get("tour_plan", []), stops, profile, profile_search_query, travel)
            return draft, stops, days, adjustments

        response, stops, enriched_plan, adjustments = draft_itinerary()

        # The feeling behind the journey -- revise once if the plan doesn't support it.
        feeling_block = _feeling_block_for(profile, destination, city_name, enriched_plan)
        if feeling_block["alignment"]["status"] == "mismatch":
            first_reason = feeling_block["alignment"]["detail"]
            revised = draft_itinerary(revision_note(feeling_block))
            revised_block = _feeling_block_for(profile, destination, city_name, revised[2])
            if revised_block["alignment"]["status"] == "aligned":
                (response, stops, enriched_plan, adjustments), feeling_block = revised, revised_block
                feeling_block["alignment"]["status"] = "revised"
                feeling_block["alignment"]["detail"] = (
                    f"The first draft did not support the chosen feeling ({first_reason}), so the itinerary was revised."
                )
            else:
                feeling_block["alignment"]["detail"] = (
                    f"{first_reason} A revised draft also could not support it, so this is flagged instead of "
                    "presented as a match."
                )

        adjustments = [*adjustments, *attach_viator_products(enriched_plan, stops, _travel_dates(profile))]
        final = _finalize_itinerary(stops, enriched_plan, profile, destination, adjustments)
        priciest, within_budget = _stays_within_budget(stops, profile)
        response.update(
            stops=stops,
            feeling_block=feeling_block,
            budget_check=_budget_check(priciest, profile, within_budget),
            tour_plan=enriched_plan,
            **final,
        )
        activity_session = ActivitySessionStore.create(
            parent_session_id=request_data.session_id,
            city_name=city_name,
            tour_plan=enriched_plan,
            response=response,
            stay_data=_guest_stay(stops[0]),
            total_cost_estimate=final["total_cost_estimate"],
            packing_tips=response.get("packing_tips", ""),
            travel_tips=response.get("travel_tips", ""),
            destination_id=destination.destination_id,
        )
        return _session_payload(activity_session, "generated")
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


@router.post("/activity_session/{activity_session_id}/hotel-rates")
async def refresh_hotel_rates(activity_session_id: str, rate_input: HotelRateRefreshInput):
    """Fetch current supplier offers for existing Google-selected itinerary stays."""
    activity_session = ActivitySessionStore.get(activity_session_id)
    if activity_session is None:
        raise HTTPException(status_code=404, detail="Activity session not found.")
    city_session = CitySessionStore.get(activity_session.parent_session_id)
    if city_session is None:
        raise HTTPException(status_code=404, detail="Parent city session not found.")
    profile = build_trip_profile(_load_session_intake(city_session.intake))
    if not profile.has_exact_dates:
        raise HTTPException(status_code=422, detail="Exact check-in and check-out dates are required for live hotel rates.")
    try:
        occupancies = build_occupancies(
            adults=profile.adults, children=profile.children, rooms=profile.rooms,
            child_ages=profile.child_ages, room_occupancies=rate_input.room_occupancies,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    request = HotelRateRequest(
        checkin=profile.check_in_date.isoformat(), checkout=profile.check_out_date.isoformat(),
        guest_nationality=rate_input.guest_nationality.upper(), currency=rate_input.currency.upper(),
        occupancies=occupancies, nights=profile.nights,
    )
    stops = activity_session.response.get("stops") or _stops_from_session(activity_session)
    stops = _enrich_stops_with_live_rates(stops, profile, request)
    destination = get_destination(activity_session.destination_id) if activity_session.destination_id else None
    days = _dump_tour_plan(activity_session.tour_plan)
    final = _finalize_itinerary(stops, days, profile, destination, activity_session.response.get("adjustments", []))
    response = {**activity_session.response, "stops": stops, "tour_plan": days, **final}
    updated = ActivitySessionStore.update_response(
        activity_session_id, response, stay_data=_guest_stay(stops[0]), total_cost_estimate=final["total_cost_estimate"],
    )
    return _session_payload(updated, "live_rates_refreshed")


@router.post("/activity_session/{activity_session_id}/hotel-prebook")
async def prebook_hotel_offer(activity_session_id: str, prebook_input: HotelPrebookInput):
    """Revalidate one displayed offer.  This endpoint never takes payment or creates a booking."""
    activity_session = ActivitySessionStore.get(activity_session_id)
    if activity_session is None:
        raise HTTPException(status_code=404, detail="Activity session not found.")
    stops = activity_session.response.get("stops") or []
    known_offer_ids = {
        offer.get("offer_id")
        for stop in stops for offer in ((stop.get("hotel") or {}).get("live_rates") or {}).get("offers", [])
    }
    if prebook_input.offer_id not in known_offer_ids:
        raise HTTPException(status_code=404, detail="Offer is not part of this itinerary's current live rates.")
    provider = get_liteapi_provider()
    if provider is None:
        raise HTTPException(status_code=503, detail="Live hotel provider is not configured.")
    try:
        return {"provider": provider.name, "booking_enabled": False, "prebook": provider.prebook(prebook_input.offer_id)}
    except HotelProviderError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error

@router.post("/regenerate_tour_plan")


async def regenerate_tour_plan(regenerate_data: RegenerateActivityInputData):
    """
    REGENERATE: different activities for the whole plan or one day, keeping
    the same stays (bases). The new days go through the same matching,
    travel-time enforcement, pricing and validation as a new itinerary. The
    guest asked for these changes, so the plan is not re-planned for the
    feeling automatically; if it no longer supports it, the block flags it.
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
        stops = activity_session.response.get("stops") or _stops_from_session(activity_session)
        day_to_regenerate = regenerate_data.day_to_regenerate

        prompt = PromptGenerator.regenerate_tour_plan_prompt(
            profile=profile,
            city_name=city_name,
            current_tour_plan=activity_session.tour_plan,
            day_to_regenerate=day_to_regenerate,
            user_instruction=regenerate_data.user_instruction,
            destination=destination,
            stops=stops,
        )
        generated_response = _parse_ai_response(get_ai_response(prompt))
        llm_days = generated_response.get("tour_plan", [])
        if day_to_regenerate is not None:
            llm_days = [day for day in llm_days if isinstance(day, dict) and day.get("day") == day_to_regenerate]

        existing_plan = _dump_tour_plan(activity_session.tour_plan)
        new_days, new_adjustments = _build_days(
            llm_days, stops, profile, _build_profile_search_context(profile), TravelTimes(),
            existing_plan=existing_plan, day_to_regenerate=day_to_regenerate,
        )
        new_adjustments = [*new_adjustments, *attach_viator_products(new_days, stops, _travel_dates(profile))]
        if day_to_regenerate is None:
            full_tour_plan, adjustments = new_days, new_adjustments
        else:
            full_tour_plan = _merge_tour_plan_days(existing_plan, new_days)
            kept = [item for item in activity_session.response.get("adjustments", []) if item.get("day") != day_to_regenerate]
            adjustments = [*kept, *new_adjustments]

        final = _finalize_itinerary(stops, full_tour_plan, profile, destination, adjustments)
        priciest, within_budget = _stays_within_budget(stops, profile)
        generated_response["tour_plan"] = full_tour_plan
        response = _merge_regenerated_field(
            previous_response=activity_session.response,
            generated_response=generated_response,
            update_field_name="tour_plan",
        )
        response.update(
            stops=stops,
            feeling_block=_feeling_block_for(profile, destination, city_name, full_tour_plan),
            budget_check=_budget_check(priciest, profile, within_budget),
            **final,
        )
        updated_session = ActivitySessionStore.update_response(
            activity_session_id=regenerate_data.activity_session_id,
            response=response,
            day_to_regenerate=day_to_regenerate,
            user_instruction=regenerate_data.user_instruction or "",
            stay_data=_guest_stay(stops[0]),
            total_cost_estimate=final["total_cost_estimate"],
        )
        if updated_session is None:
            raise HTTPException(status_code=404, detail="Activity session not found.")
        return _session_payload(updated_session, "regenerated")
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
        **_session_payload(session, "saved"),
        "parent_session_id": session.parent_session_id,
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
