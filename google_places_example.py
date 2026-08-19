import os
import json
import requests


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


def get_detailed_tourist_places(location_name: str, api_key: str) -> list[dict]:
    """Return a list of tourist places with name, phone, photos, and opening hours."""
    url = "https://places.googleapis.com/v1/places:searchText"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": (
            "places.displayName,"
            "places.formattedAddress,"
            "places.nationalPhoneNumber,"
            "places.regularOpeningHours,"
            "places.photos"
        ),
    }
    payload = {
        "textQuery": f"tourist attractions in {location_name}",
        "includedType": "tourist_attraction",
        "languageCode": "en",
    }

    try:
        response = requests.post(url, headers=headers, json=payload)
        if response.status_code != 200:
            return [{"error": f"{response.status_code}: {response.text}"}]

        places = response.json().get("places", [])
        results = []

        for place in places:
            weekday_descriptions = place.get("regularOpeningHours", {}).get(
                "weekdayDescriptions", []
            )
            results.append(
                {
                    "name": place.get("displayName", {}).get("text", "N/A"),
                    "address": place.get("formattedAddress", "No address available"),
                    "phone": place.get("nationalPhoneNumber", "No phone number listed"),
                    "photos": _extract_photo_urls(place, api_key) or ["No photos available"],
                    "available_time": weekday_descriptions
                    or ["Hours not available or open 24/7"],
                }
            )

        return results

    except Exception as e:
        return [{"error": str(e)}]


def get_it_companies(location_name: str, api_key: str) -> list[dict]:
    """Return a list of IT/software companies with name, address, phone, photos, hours."""
    url = "https://places.googleapis.com/v1/places:searchText"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": (
            "places.displayName,"
            "places.formattedAddress,"
            "places.nationalPhoneNumber,"
            "places.regularOpeningHours,"
            "places.photos"
        ),
    }
    payload = {
        "textQuery": f"top software IT companies in {location_name}",
        "languageCode": "en",
    }

    try:
        response = requests.post(url, headers=headers, json=payload)
        if response.status_code != 200:
            return [{"error": f"{response.status_code}: {response.text}"}]

        places = response.json().get("places", [])
        results = []

        for place in places:
            weekday_descriptions = place.get("regularOpeningHours", {}).get(
                "weekdayDescriptions", []
            )
            results.append(
                {
                    "name": place.get("displayName", {}).get("text", "N/A"),
                    "address": place.get("formattedAddress", "No address listed"),
                    "phone": place.get("nationalPhoneNumber", "No phone number listed"),
                    "photos": _extract_photo_urls(place, api_key) or ["No photos available"],
                    "office_hours": weekday_descriptions or ["Hours not listed"],
                }
            )

        return results

    except Exception as e:
        return [{"error": str(e)}]


def get_google_hotels_sorted_by_rating(location_name: str, api_key: str) -> list[dict]:
    """Return hotels/resorts sorted by rating (highest first)."""
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
        "textQuery": f"resorts in {location_name}",
        "includedType": "hotel",
        "pageSize": 10,
    }

    try:
        response = requests.post(url, headers=headers, json=payload)
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
                    "photos": _extract_photo_urls(place, api_key) or ["No photo available"],
                    "coords": {
                        "lat": coords.get("latitude"),
                        "lng": coords.get("longitude"),
                    },
                }
            )

        hotel_list.sort(key=lambda h: h["rating"], reverse=True)
        return hotel_list

    except Exception as e:
        return [{"error": str(e)}]


def calculate_distance_routes_api(
    origin_address: str, destination_address: str, api_key: str
) -> dict:
    """Return distance (km) and duration (hours) between two addresses."""
    if not api_key:
        return {"error": "Missing Google Maps API key."}

    url = "https://routes.googleapis.com/v2:computeRoutes"
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
        response = requests.post(url, json=payload, headers=headers)
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


if __name__ == "__main__":
    API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "")

    hotels = get_google_hotels_sorted_by_rating("hostels near Mohakhali, Dacca", API_KEY)
    print(json.dumps(hotels, indent=2))
# --- EXECUTION ---
if __name__ == "__main__":
    API_KEY = "AIzaSyBHOYbBE7kFT21wtxFtKwZ6cqgt-yJXGzM" 
    
    ORIGIN = "Dhaka, Bangladesh"
    DESTINATION = "Chittagong, Bangladesh"
