"""
Resolve the guest's departure point for the travel-distance check.

Coordinates sent by a places-autocomplete widget are used as-is. Otherwise
departure_location is geocoded with the existing get_cityinfo tool. Per
IMPORT_RULES.csv "Freshness and privacy", the outcome and a timestamp are
recorded, and any lookup failure is marked UNVERIFIED (never guessed).
Only the departure text is sent to the lookup -- no other intake field.
"""

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Optional

from src.core.geography import region_for_country
from src.tools.tools import get_cityinfo


@dataclass
class Origin:
    status: str  # "GUEST_SUPPLIED" | "GEOCODED" | "UNVERIFIED"
    latitude: Optional[float]
    longitude: Optional[float]
    country: Optional[str]
    region: Optional[str]
    checked_at_utc: str
    detail: str = ""
    # ISO 3166-1 alpha-2, when known (public-holiday lookups for date suggestions).
    country_code: Optional[str] = None

    @property
    def has_coordinates(self) -> bool:
        return self.latitude is not None and self.longitude is not None

    def to_dict(self) -> dict:
        return asdict(self)


def _iso_code(country: Optional[str]) -> Optional[str]:
    """A two-letter country value sent by an autocomplete widget is already an ISO code."""
    value = (country or "").strip()
    return value.upper() if len(value) == 2 and value.isalpha() else None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def resolve_origin(
    departure_location: str,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    country: Optional[str] = None,
) -> Origin:
    if latitude is not None and longitude is not None:
        return Origin(
            status="GUEST_SUPPLIED",
            latitude=latitude,
            longitude=longitude,
            country=country,
            region=region_for_country(country or ""),
            checked_at_utc=_now(),
            country_code=_iso_code(country),
        )

    try:
        result = get_cityinfo.invoke({"city_name": departure_location})
    except Exception as error:  # Network/credential failure -- never fatal.
        result = {"error": str(error)}

    lookup_lat = result.get("lat") if isinstance(result, dict) else None
    lookup_lng = result.get("lng") if isinstance(result, dict) else None
    if not isinstance(result, dict) or "error" in result or lookup_lat is None or lookup_lng is None:
        return Origin(
            status="UNVERIFIED",
            latitude=None,
            longitude=None,
            country=country,
            region=region_for_country(country or ""),
            checked_at_utc=_now(),
            detail="Departure location could not be geocoded; travel distance was not checked.",
        )

    lookup_country = result.get("country")
    if lookup_country in (None, "", "N/A"):
        lookup_country = country
    return Origin(
        status="GEOCODED",
        latitude=float(lookup_lat),
        longitude=float(lookup_lng),
        country=lookup_country,
        region=region_for_country(lookup_country or ""),
        checked_at_utc=_now(),
        country_code=result.get("country_code") or _iso_code(country),
    )
