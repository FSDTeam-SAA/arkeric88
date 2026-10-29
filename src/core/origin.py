"""
Resolve the guest's departure point for the travel-distance check.

Coordinates sent by a places-autocomplete widget are used as-is. Otherwise
departure_location is geocoded with the existing get_cityinfo tool: first as
a city, then as any place (an airport such as "AUS" or "JFK airport"). Per
IMPORT_RULES.csv "Freshness and privacy", the outcome and a timestamp are
recorded, and any lookup failure is marked UNVERIFIED (never guessed).

A failed lookup never blocks the guest (QUALITY_AND_RELEASE_GATES.csv
"Origin safeguards"): the guest's own words are kept, and when they name a
country ("Austin, TX, USA") that country's world region is still known, so a
"Nearby" search can stay in the right part of the world without claiming a
distance. No departure at all ("Decide later") is NOT_PROVIDED.

Only the departure text is sent to the lookup -- no other intake field.
"""

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Optional

from src.core.geography import region_for_country
from src.tools.tools import get_cityinfo

GUEST_SUPPLIED, GEOCODED, UNVERIFIED, NOT_PROVIDED = "GUEST_SUPPLIED", "GEOCODED", "UNVERIFIED", "NOT_PROVIDED"


@dataclass
class Origin:
    status: str  # GUEST_SUPPLIED | GEOCODED | UNVERIFIED | NOT_PROVIDED
    latitude: Optional[float]
    longitude: Optional[float]
    country: Optional[str]
    region: Optional[str]
    checked_at_utc: str
    detail: str = ""
    # ISO 3166-1 alpha-2, when known (public-holiday lookups for date suggestions).
    country_code: Optional[str] = None
    # What the guest typed, and the place it was matched to (if any), kept apart.
    input_text: Optional[str] = None
    normalized_name: Optional[str] = None

    @property
    def has_coordinates(self) -> bool:
        return self.latitude is not None and self.longitude is not None

    @property
    def provided(self) -> bool:
        return self.status != NOT_PROVIDED

    def to_dict(self) -> dict:
        return asdict(self)


def _iso_code(country: Optional[str]) -> Optional[str]:
    """A two-letter country value sent by an autocomplete widget is already an ISO code."""
    value = (country or "").strip()
    return value.upper() if len(value) == 2 and value.isalpha() else None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def country_from_text(text: Optional[str]) -> Optional[str]:
    """The country named at the end of "City, State, Country", when it is a known country."""
    parts = [part.strip() for part in (text or "").split(",") if part.strip()]
    if len(parts) < 2:
        return None
    return parts[-1] if region_for_country(parts[-1]) else None


def _lookup(departure_location: str) -> dict:
    """City first, then any place (airports); network failures are never fatal."""
    result: dict = {}
    for args in ({"city_name": departure_location}, {"city_name": departure_location, "any_place": True}):
        try:
            result = get_cityinfo.invoke(args)
        except Exception as error:  # Network/credential failure -- never fatal.
            result = {"error": str(error)}
        if isinstance(result, dict) and "error" not in result and result.get("lat") is not None and result.get("lng") is not None:
            return result
    return result if isinstance(result, dict) else {"error": "Unexpected lookup result."}


def not_provided() -> Origin:
    return Origin(
        status=NOT_PROVIDED,
        latitude=None,
        longitude=None,
        country=None,
        region=None,
        checked_at_utc=_now(),
        detail="No departure location yet, so travel distance and travel times were not checked.",
    )


def resolve_origin(
    departure_location: Optional[str],
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    country: Optional[str] = None,
) -> Origin:
    if not (departure_location or "").strip():
        return not_provided()

    if latitude is not None and longitude is not None:
        return Origin(
            status=GUEST_SUPPLIED,
            latitude=latitude,
            longitude=longitude,
            country=country,
            region=region_for_country(country or ""),
            checked_at_utc=_now(),
            country_code=_iso_code(country),
            input_text=departure_location,
            normalized_name=departure_location,
        )

    result = _lookup(departure_location)
    lookup_lat, lookup_lng = result.get("lat"), result.get("lng")
    if "error" in result or lookup_lat is None or lookup_lng is None:
        fallback_country = country or country_from_text(departure_location)
        return Origin(
            status=UNVERIFIED,
            latitude=None,
            longitude=None,
            country=fallback_country,
            region=region_for_country(fallback_country or ""),
            checked_at_utc=_now(),
            detail="Departure location could not be matched on the map; travel distance was not measured.",
            country_code=_iso_code(country),
            input_text=departure_location,
        )

    lookup_country = result.get("country")
    if lookup_country in (None, "", "N/A"):
        lookup_country = country or country_from_text(departure_location)
    return Origin(
        status=GEOCODED,
        latitude=float(lookup_lat),
        longitude=float(lookup_lng),
        country=lookup_country,
        region=region_for_country(lookup_country or ""),
        checked_at_utc=_now(),
        country_code=result.get("country_code") or _iso_code(country),
        input_text=departure_location,
        normalized_name=result.get("formatted_address") or result.get("city_name"),
    )
