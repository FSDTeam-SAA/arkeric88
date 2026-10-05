"""
Travel times between itinerary points (stays, experiences, restaurants).

Every itinerary leg is measured from coordinates with the Google Routes
matrix (driving, traffic-unaware). When a route cannot be fetched, the leg
falls back to a straight-line estimate that is clearly marked as such, so the
validation step can tell a measured time from a guess. Results are cached
per point pair for the life of one TravelTimes object (one request).
"""

from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from src.core.geography import great_circle_km
from src.tools.tools import MAX_MATRIX_SIDE, compute_route_matrix

Point = Tuple[float, float]

# Default limit from the client brief: 60 minutes each way from the day's
# base or the previous stop, unless the itinerary includes an overnight move.
MAX_LEG_MINUTES = 60

# Straight-line fallback: roads are rarely straight, and local driving is slow.
FALLBACK_ROAD_FACTOR = 1.4
FALLBACK_SPEED_KMH = 45.0

SOURCE_ROUTES = "google_routes"
SOURCE_ESTIMATE = "straight_line_estimate"
SOURCE_NO_ROUTE = "no_drivable_route"


@dataclass(frozen=True)
class Leg:
    minutes: Optional[int]
    km: Optional[float]
    source: str

    @property
    def measured(self) -> bool:
        return self.source == SOURCE_ROUTES

    def within(self, max_minutes: int) -> bool:
        """A leg with no drivable route is never within the limit."""
        return self.minutes is not None and self.minutes <= max_minutes

    def to_dict(self) -> dict:
        return {"minutes": self.minutes, "km": self.km, "source": self.source}


def point_of(item: dict) -> Optional[Point]:
    """(lat, lng) of an itinerary item, stay or place dict, if it has one."""
    if not isinstance(item, dict):
        return None
    latitude, longitude = item.get("latitude"), item.get("longitude")
    if latitude is None or longitude is None:
        coords = item.get("coords") or {}
        latitude, longitude = coords.get("lat"), coords.get("lng")
    if latitude is None or longitude is None:
        return None
    try:
        return float(latitude), float(longitude)
    except (TypeError, ValueError):
        return None


def estimate_leg(origin: Point, destination: Point) -> Leg:
    km = great_circle_km(origin[0], origin[1], destination[0], destination[1]) * FALLBACK_ROAD_FACTOR
    return Leg(minutes=round(km / FALLBACK_SPEED_KMH * 60), km=round(km, 1), source=SOURCE_ESTIMATE)


def straight_line_km(origin: Point, destination: Point) -> float:
    return great_circle_km(origin[0], origin[1], destination[0], destination[1])


def _key(point: Point) -> Tuple[float, float]:
    return round(point[0], 5), round(point[1], 5)


def _chunks(points: List[Point], size: int) -> Iterable[List[Point]]:
    for start in range(0, len(points), size):
        yield points[start:start + size]


class TravelTimes:
    """Cached driving times between coordinates for one itinerary build."""

    def __init__(self, matrix_fn: Callable[[List[Point], List[Point]], dict] = None):
        self._matrix_fn = matrix_fn or compute_route_matrix
        self._cache: Dict[Tuple[Tuple[float, float], Tuple[float, float]], Leg] = {}
        self.route_calls = 0

    def prefetch(self, origins: List[Point], destinations: List[Point]) -> None:
        """Measure every origin -> destination pair not already cached, in as few calls as possible."""
        origins = list(dict.fromkeys(_key(point) for point in origins))
        destinations = list(dict.fromkeys(_key(point) for point in destinations))
        for origin_chunk in _chunks(origins, MAX_MATRIX_SIDE):
            for destination_chunk in _chunks(destinations, MAX_MATRIX_SIDE):
                missing = [
                    (origin, destination)
                    for origin in origin_chunk
                    for destination in destination_chunk
                    if origin != destination and (origin, destination) not in self._cache
                ]
                if not missing:
                    continue
                self._fetch(origin_chunk, destination_chunk)

    def _fetch(self, origins: List[Point], destinations: List[Point]) -> None:
        self.route_calls += 1
        try:
            result = self._matrix_fn(origins, destinations) or {}
        except Exception as error:  # Network failure is never fatal: fall back to estimates.
            result = {"error": str(error)}
        if "error" not in result:
            for element in result.get("elements", []):
                try:
                    origin = origins[element["origin"]]
                    destination = destinations[element["destination"]]
                except (KeyError, IndexError, TypeError):
                    continue
                if element.get("route_found"):
                    self._cache[(origin, destination)] = Leg(element.get("minutes"), element.get("km"), SOURCE_ROUTES)
                else:
                    # Google answered but found no drivable route (e.g. water with no ferry).
                    self._cache[(origin, destination)] = Leg(None, None, SOURCE_NO_ROUTE)
        # Anything still unmeasured (API error, missing element) gets a labelled estimate
        # now, so later lookups do not retry the same failed request pair by pair.
        for origin in origins:
            for destination in destinations:
                if origin != destination and (origin, destination) not in self._cache:
                    self._cache[(origin, destination)] = estimate_leg(origin, destination)

    def leg(self, origin: Point, destination: Point) -> Leg:
        origin, destination = _key(origin), _key(destination)
        if origin == destination:
            return Leg(0, 0.0, SOURCE_ROUTES)
        cached = self._cache.get((origin, destination))
        if cached is None:
            self.prefetch([origin], [destination])
            cached = self._cache.get((origin, destination))
        if cached is None:
            cached = estimate_leg(origin, destination)
            self._cache[(origin, destination)] = cached
        return cached
