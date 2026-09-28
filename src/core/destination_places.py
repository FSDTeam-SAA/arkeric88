"""
Map lookups for catalog destinations (photos, and coordinates for the 28
editorial rows that have none).

One get_cityinfo lookup per destination, shared by photo enrichment and the
travel-distance check, and cached for the life of the process. Only
deterministic outcomes are cached (MATCHED / COUNTRY_MISMATCH); a failed
call is retried next time and reported as UNVERIFIED. The catalog's own
country is the hard constraint: a same-named place in another country never
contributes photos or coordinates. Only the destination name and country are
ever sent to the lookup.
"""

import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Lock
from typing import Dict, Iterable, Tuple

from src.core.destination_catalog import Destination
from src.core.geography import same_country
from src.tools.tools import get_cityinfo

_LOOKUP_WORKERS = 8
_cache: Dict[str, dict] = {}
_cache_lock = Lock()


def lookup_name(destination_name: str) -> str:
    """"Tucson & Sonoran Desert" -> "Tucson": the lookup expects a locality."""
    return re.split(r"\s+&\s+", destination_name)[0].strip()


def _lookup(destination: Destination) -> dict:
    query = lookup_name(destination.destination)
    checked_at = datetime.now(timezone.utc).isoformat()
    try:
        result = get_cityinfo.invoke({"city_name": query, "region_hint": destination.country})
    except Exception as error:  # Network/credential failure is never fatal.
        result = {"error": str(error)}

    place = {
        "query": query,
        "outcome": "UNVERIFIED",
        "checked_at_utc": checked_at,
        "photos": [],
        "latitude": None,
        "longitude": None,
    }
    if not isinstance(result, dict) or "error" in result:
        return place
    if not same_country(result.get("country"), destination.country):
        place["outcome"] = "COUNTRY_MISMATCH"
        return place
    place["outcome"] = "MATCHED"
    place["photos"] = [photo for photo in result.get("photos", []) if isinstance(photo, str)]
    if result.get("lat") is not None and result.get("lng") is not None:
        place["latitude"] = float(result["lat"])
        place["longitude"] = float(result["lng"])
    return place


def lookup_destination_place(destination: Destination) -> dict:
    with _cache_lock:
        cached = _cache.get(destination.destination_id)
    if cached is not None:
        return dict(cached)
    place = _lookup(destination)
    if place["outcome"] != "UNVERIFIED":
        with _cache_lock:
            _cache[destination.destination_id] = dict(place)
    return place


def lookup_missing_coordinates(destinations: Iterable[Destination]) -> Dict[str, Tuple[float, float]]:
    """Coordinates for destinations the catalog has none for, via cached map lookups."""
    missing = [
        destination for destination in destinations
        if destination.latitude is None or destination.longitude is None
    ]
    if not missing:
        return {}
    with ThreadPoolExecutor(max_workers=_LOOKUP_WORKERS) as pool:
        places = list(pool.map(lookup_destination_place, missing))
    return {
        destination.destination_id: (place["latitude"], place["longitude"])
        for destination, place in zip(missing, places)
        if place["latitude"] is not None and place["longitude"] is not None
    }


def clear_place_cache() -> None:
    """For tests."""
    with _cache_lock:
        _cache.clear()

