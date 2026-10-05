"""
POST /recommend_travel_dates -- specific check-in/check-out dates for a guest
whose timing is flexible or a month/season (intake step 10).

The frontend sends the answers to steps 1-9 plus the step-10 choice and the
number of nights. The dates are scored on recorded climate, crowd periods,
day-of-week shape and timing (see src/core/date_recommendation.py), and the
reasons are explained in guest language. Nothing here checks availability or
prices; the response says so.
"""

from datetime import date
from functools import lru_cache
from typing import Dict, List, Optional, Tuple

import holidays
from fastapi import APIRouter, HTTPException

from app.schemas.intake_schema import DateRecommendationRequest
from src.core.date_recommendation import (
    DateContext,
    recommend_dates,
    resolve_window,
    write_summary,
)
from src.core.destination_catalog import get_destination
from src.core.destination_places import lookup_destination_place
from src.core.geography import is_southern_hemisphere
from src.core.origin import resolve_origin
from src.core.trip_profile import build_trip_profile
from src.service.chat_services import get_ai_response
from src.tools.climate import monthly_climate

router = APIRouter()


@lru_cache(maxsize=64)
def _holidays_for(country_code: str, years: Tuple[int, ...]) -> Dict[date, str]:
    country = holidays.country_holidays(country_code, years=list(years))
    result = dict(country)
    # Many holidays are set per province or state (e.g. Canadian Thanksgiving). Without the
    # guest's region, count a holiday when at least half of the regions observe it.
    regions = tuple(getattr(country, "subdivisions", ()) or ())
    counts: Dict[date, int] = {}
    names: Dict[date, str] = {}
    for region in regions:
        try:
            regional = holidays.country_holidays(country_code, subdiv=region, years=list(years))
        except Exception:
            continue
        for day, name in regional.items():
            counts[day] = counts.get(day, 0) + 1
            names.setdefault(day, name)
    for day, count in counts.items():
        if count >= len(regions) / 2:
            result.setdefault(day, names[day])
    return result


def national_holidays(country_code: str, years: List[int]) -> Dict[date, str]:
    """Public holidays in the guest's departure country (python-holidays), including widely observed regional ones."""
    return dict(_holidays_for(country_code.upper(), tuple(sorted(years))))


def _destination_point(destination) -> Optional[Tuple[float, float]]:
    if destination.latitude is not None and destination.longitude is not None:
        return destination.latitude, destination.longitude
    place = lookup_destination_place(destination)
    if place["latitude"] is not None and place["longitude"] is not None:
        return place["latitude"], place["longitude"]
    return None


@router.post("/recommend_travel_dates")


async def recommend_travel_dates(request: DateRecommendationRequest):
    destination = None
    if request.destination_id:
        destination = get_destination(request.destination_id)
        if destination is None:
            raise HTTPException(status_code=404, detail=f"No catalog destination found for '{request.destination_id}'.")
    try:
        profile = build_trip_profile(request)
        origin = resolve_origin(
            request.departure_location, request.departure_latitude, request.departure_longitude, request.departure_country,
        )

        # Where the weather matters: the chosen destination, or home on a nearby trip.
        point, place_label = None, None
        if destination is not None:
            point, place_label = _destination_point(destination), destination.destination
        elif profile.travel_distance == "nearby" and origin.has_coordinates:
            point, place_label = (origin.latitude, origin.longitude), f"the {request.departure_location} area"
        climate = monthly_climate(*point) if point else None
        if climate and "error" in climate:
            climate = None

        latitude = point[0] if point else origin.latitude
        southern = latitude < 0 if latitude is not None else is_southern_hemisphere(origin.country or "")
        window = resolve_window(profile, date.today(), request.earliest_check_in, request.latest_check_out, southern)
        context = DateContext(
            today=date.today(),
            southern=southern,
            latitude=latitude,
            climate=climate,
            place_label=place_label if climate else None,
            country_code=origin.country_code,
            holiday_lookup=national_holidays,
        )
        result = recommend_dates(profile, window, context)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error)) from error

    summary, summary_source = write_summary(result, profile, get_ai_response)
    return {
        **result,
        "summary": summary,
        "summary_source": summary_source,
        "destination_id": request.destination_id,
        "origin": origin.to_dict(),
    }
