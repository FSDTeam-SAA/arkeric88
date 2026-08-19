import requests
from langchain_core.tools import tool
from src.config.config_env import settings
from src.core.image_registry import image_registry


def _build_profile_search_query(
    location_name: str,
    search_query: str | None,
    fallback_subject: str,
) -> str:
    """Combine profile intent with geography without forcing a generic query."""
    intent = " ".join((search_query or "").split()).strip()
    location = " ".join((location_name or "").split()).strip()
    if intent:
        return f"{intent} in {location}" if location else intent
    return f"{fallback_subject} in {location}" if location else fallback_subject


def _extract_photo_urls(place: dict, api_key: str, max_photos: int = 4) -> list[str]:
    """Return up to `max_photos` direct image URLs for a place, if any exist."""
    photos = place.get("photos", [])
    urls = []
    for photo in photos[:max_photos]:
        resource_name = photo.get("name")
        if resource_name:
            urls.append(
                f"https://places.googleapis.com/v1/{resource_name}/media"
                f"?key={api_key}&maxHeightPx=400"
            )
    return urls


def _extract_photo_ids(place: dict, api_key: str, max_photos: int = 4) -> list[str]:
    """Return compact image IDs for place photos to reduce LLM/tool payload size."""
    return image_registry.register_many(_extract_photo_urls(place, api_key, max_photos))


@tool
def get_cityinfo(city_name: str, region_hint: str | None = None) -> dict:
    """Look up a city by name, using a region hint to disambiguate matching names.
    Returns a dict with: city_name, country, lat, lng, and photos
    (up to 4 compact image IDs). Resolve image IDs to URLs before returning
    the final API response.
    """
    api_key = settings.google_api_key
    url = "https://places.googleapis.com/v1/places:searchText"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": (
            "places.displayName,"
            "places.formattedAddress,"
            "places.addressComponents,"
            "places.location,"
            "places.photos"
        ),
    }
    payload = {
        "textQuery": f"{city_name}, {region_hint}" if region_hint else city_name,
        "includedType": "locality",
        "languageCode": "en",
    }
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=20)
        if response.status_code != 200:
            return {"error": f"{response.status_code}: {response.text}"}
        places = response.json().get("places", [])
        if not places:
            return {"error": f"No city found for '{city_name}'."}
        place = places[0]
        country = "N/A"
        for component in place.get("addressComponents", []):
            if "country" in component.get("types", []):
                country = component.get("longText", "N/A")
                break
        location = place.get("location", {})
        data = {
            "city_name": place.get("displayName", {}).get("text", city_name),
            "country": country,
            "lat": location.get("latitude"),
            "lng": location.get("longitude"),
            "photos": _extract_photo_ids(place, api_key) or ["No photos available"],
        }
        # print("=" * 100)
        # print(" " * 40, "City Data Information")
        # print("=" * 100)
        # print(data)
        return data
    except Exception as e:
        return {"error": str(e)}


@tool
def get_detailed_tourist_places(
    location_name: str,
    search_query: str | None = None,
) -> list[dict]:
    """Search for real activities using profile intent plus a location.

    `search_query` should describe positive desired qualities derived from the full
    traveler profile. Do not include avoided activities; filter those after retrieval.
    Returns a list of dicts, each with: name, address, phone, coords, photos
    (up to 4 compact image IDs), and available_time (weekday opening hours).
    Resolve image IDs to URLs before returning the final API response.
    """
    api_key = settings.google_api_key
    url = "https://places.googleapis.com/v1/places:searchText"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": (
            "places.displayName,"
            "places.formattedAddress,"
            "places.nationalPhoneNumber,"
            "places.location,"
            "places.regularOpeningHours,"
            "places.photos"
        ),
    }
    payload = {
        "textQuery": _build_profile_search_query(
            location_name, search_query, "wellness and restorative experiences"
        ),
        "languageCode": "en",
        "pageSize": 20,
    }
    if not search_query:
        payload["includedType"] = "tourist_attraction"
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=20)
        if response.status_code != 200:
            return [{"error": f"{response.status_code}: {response.text}"}]
        places = response.json().get("places", [])
        results = []
        for place in places:
            coords = place.get("location", {})
            weekday_descriptions = place.get("regularOpeningHours", {}).get(
                "weekdayDescriptions", []
            )
            results.append(
                {
                    "name": place.get("displayName", {}).get("text", "N/A"),
                    "address": place.get("formattedAddress", "No address available"),
                    "phone": place.get("nationalPhoneNumber", "No phone number listed"),
                    "photos": _extract_photo_ids(place, api_key) or ["No photos available"],
                    "coords": {
                        "lat": coords.get("latitude"),
                        "lng": coords.get("longitude"),
                    },
                    "available_time": weekday_descriptions
                    or ["Hours not available or open 24/7"],
                }
            )
        # print("=" * 100)
        # print(" " * 40, "Details Tour Plan")
        # print("=" * 100)
        # print(results)
        return results
    except Exception as e:
        return [{"error": str(e)}]



@tool
def get_google_hotels_sorted_by_rating(
    location_name: str,
    search_query: str | None = None,
) -> list[dict]:
    """Search for stays using profile intent plus a location, sorted by rating.

    `search_query` may include emotional tone, structure, environment, wellness
    modalities, facilities, and budget tier. It must not contain avoided activities.
    Returns a list of dicts, each with: name, rating, phone, price_level,
    address, photos (up to 4 compact image IDs), and coords (lat/lng).
    Resolve image IDs to URLs before returning the final API response.
    """
    api_key = settings.google_api_key
    url = "https://places.googleapis.com/v1/places:searchText"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": (
            "places.displayName,"
            "places.formattedAddress,"
            "places.priceLevel,"
            "places.location,"
            "places.rating,"
            "places.nationalPhoneNumber,"
            "places.photos"
        ),
    }
    payload = {
        "textQuery": _build_profile_search_query(
            location_name, search_query, "wellness retreats and resorts"
        ),
        "includedType": "hotel",
        "languageCode": "en",
        "pageSize": 10,
    }
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=20)
        if response.status_code != 200:
            return [{"error": f"{response.status_code}: {response.text}"}]
        places = response.json().get("places", [])
        if not places:
            return []
        hotel_list = []
        for place in places:
            coords = place.get("location", {})
            hotel_list.append(
                {
                    "name": place.get("displayName", {}).get("text", "N/A"),
                    "rating": place.get("rating", 0.0),
                    "phone": place.get("nationalPhoneNumber", "No phone number listed"),
                    "price_level": place.get("priceLevel", "NOT_AVAILABLE"),
                    "address": place.get("formattedAddress", "No address listed"),
                    "photos": _extract_photo_ids(place, api_key) or ["No photo available"],
                    "coords": {
                        "lat": coords.get("latitude"),
                        "lng": coords.get("longitude"),
                    },
                }
            )
        hotel_list.sort(key=lambda h: h["rating"], reverse=True)
        # print("=" * 100)
        # print(" " * 40, "Hotel Information")
        # print("=" * 100)
        # print(hotel_list)
        return hotel_list
    except Exception as e:
        return [{"error": str(e)}]

@tool
def get_google_hotels_by_facilities(
    location_name: str,
    facilities: list[str] | None = None,
    search_query: str | None = None,
) -> list[dict]:
    """Search hotels by profile intent and concrete facilities."""

    api_key = settings.google_api_key
    url = "https://places.googleapis.com/v1/places:searchText"

    desired_terms = [search_query.strip()] if search_query and search_query.strip() else []
    desired_terms.extend(str(facility).strip() for facility in facilities or [] if str(facility).strip())
    query_text = _build_profile_search_query(
        location_name,
        " ".join(desired_terms),
        "wellness retreats and resorts",
    )

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": (
            "places.displayName,"
            "places.formattedAddress,"
            "places.priceLevel,"
            "places.location,"
            "places.rating,"
            "places.nationalPhoneNumber,"
            "places.photos,"
            "places.editorialSummary"  # Added to fetch a description of the place
        ),
    }

    payload = {
        "textQuery": query_text,
        "includedType": "hotel",
        "pageSize": 10,
    }

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=20)

        if response.status_code != 200:
            return [{"error": f"{response.status_code}: {response.text}"}]

        places = response.json().get("places", [])
        if not places:
            return []

        hotel_list = []
        for place in places:
            coords = place.get("location", {})

            # Safely get the editorial summary if it exists
            summary = place.get("editorialSummary", {}).get("text", "No summary available")

            hotel_list.append(
                {
                    "name": place.get("displayName", {}).get("text", "N/A"),
                    "rating": place.get("rating", 0.0),
                    "summary": summary,
                    "phone": place.get("nationalPhoneNumber", "No phone number listed"),
                    "price_level": place.get("priceLevel", "NOT_AVAILABLE"),
                    "address": place.get("formattedAddress", "No address listed"),
                    "photos": _extract_photo_ids(place, api_key) or ["No photo available"],
                    "coords": {
                        "lat": coords.get("latitude"),
                        "lng": coords.get("longitude"),
                    },
                }
            )

        # 2. We DO NOT sort by rating here anymore.
        # The list remains in the order Google returned it, which is optimized for relevance to the requested facilities.

        print("=" * 100)
        print(" " * 40, f"Hotels in {location_name} with {facilities}")
        print("=" * 100)

        return hotel_list

    except Exception as e:
        return [{"error": str(e)}]


@tool
def get_nearby_restaurants(
    location_name: str,
    meal_type: str = "meal",
    search_query: str | None = None,
) -> list[dict]:
    """Search for restaurants near a route anchor with optional profile intent.

    `search_query` can describe positive dining needs such as calm, healthy,
    vegetarian, or accessible; it is combined with meal type and location.
    Returns restaurant dicts with: name, address, rating, price_level, phone,
    coords, photos (compact image IDs), and meal_type. Resolve image IDs to
    URLs before returning the final API response.
    """
    api_key = settings.google_api_key
    url = "https://places.googleapis.com/v1/places:searchText"
    meal_query = meal_type.lower().strip() or "meal"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": (
            "places.displayName,"
            "places.formattedAddress,"
            "places.priceLevel,"
            "places.location,"
            "places.rating,"
            "places.nationalPhoneNumber,"
            "places.photos"
        ),
    }
    dining_intent = " ".join(
        term for term in [search_query or "", meal_query, "restaurants near"] if term
    )
    payload = {
        "textQuery": _build_profile_search_query(location_name, dining_intent, "restaurants"),
        "includedType": "restaurant",
        "languageCode": "en",
        "pageSize": 8,
    }
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=20)
        if response.status_code != 200:
            return [{"error": f"{response.status_code}: {response.text}"}]
        restaurants = []
        for place in response.json().get("places", []):
            coords = place.get("location", {})
            restaurants.append(
                {
                    "name": place.get("displayName", {}).get("text", "N/A"),
                    "address": place.get("formattedAddress", "No address listed"),
                    "rating": place.get("rating", 0.0),
                    "phone": place.get("nationalPhoneNumber", "No phone number listed"),
                    "price_level": place.get("priceLevel", "NOT_AVAILABLE"),
                    "photos": _extract_photo_ids(place, api_key) or ["No photo available"],
                    "coords": {
                        "lat": coords.get("latitude"),
                        "lng": coords.get("longitude"),
                    },
                    "meal_type": meal_type,
                }
            )
        restaurants.sort(key=lambda restaurant: restaurant.get("rating", 0.0), reverse=True)
        return restaurants
    except Exception as e:
        return [{"error": str(e)}]


@tool
def calculate_distance_routes_api(origin_address: str, destination_address: str) -> dict:
    """Calculate driving distance and duration between two addresses.
        Returns a dict with: origin, destination, distance_km, duration_hours,
        and duration_minutes.
    """
    api_key = settings.google_api_key
    if not api_key:
        return {"error": "Missing Google Maps API key."}
    url = "https://routes.googleapis.com/directions/v2:computeRoutes"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": "routes.legs.distanceMeters,routes.legs.duration",
    }
    payload = {
        "origin": {"address": origin_address},
        "destination": {"address": destination_address},
        "travelMode": "DRIVE",
        "routingPreference": "TRAFFIC_AWARE",
    }
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=20)
        if response.status_code != 200:
            return {"error": f"HTTP {response.status_code}: {response.text}"}
        data = response.json()
        leg = data["routes"][0]["legs"][0]
        distance_km = leg["distanceMeters"] / 1000
        duration_seconds = int(leg["duration"].rstrip("s"))
        return {
            "origin": origin_address,
            "destination": destination_address,
            "distance_km": round(distance_km, 2),
            "duration_hours": round(duration_seconds / 3600, 2),
            "duration_minutes": round(duration_seconds / 60),
        }
    except (KeyError, IndexError):
        return {"error": "Could not parse route details.", "raw": data}
    except Exception as e:
        return {"error": str(e)}