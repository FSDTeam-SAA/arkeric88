"""
Geographic coherence for itineraries (client brief, Priority 1).

- Every itinerary has one primary base per stop: the stay's location.
- Every experience and restaurant must be within MAX_LEG_MINUTES of the day's
  base and of the previous stop, measured from coordinates. An item that is
  too far is replaced with a nearby alternative or removed, never kept.
- Activities in another region become a multi-stop itinerary: a stay per
  region, a transfer leg between stops and a lighter transfer day. Only
  trips long enough for that get more than one stop.

Network calls are injected (TravelTimes, a replacement finder, a route
function) so this module stays testable without Google.
"""

import re
from typing import Callable, List, Optional, Tuple

from src.core.guest_text import minutes_phrase, natural_list
from src.core.travel_time import (
    MAX_LEG_MINUTES,
    SOURCE_ESTIMATE,
    SOURCE_ROUTES,
    Leg,
    Point,
    TravelTimes,
    estimate_leg,
    point_of,
)

MIN_NIGHTS_FOR_SECOND_STOP = 4
MIN_NIGHTS_FOR_THIRD_STOP = 8
MIN_NIGHTS_PER_STOP = 2
TRANSFER_DAY_MAX_EXPERIENCES = 1
TRANSFER_START = 9 * 60 + 30
# Realistic buffers on top of the driving time: loading, breaks, check-in.
TRANSFER_ROAD_BUFFER_RATIO = 0.15
TRANSFER_MIN_BUFFER_MINUTES = 15
FERRY_BUFFER_MINUTES = 45
FREE_TIME_MIN_GAP_MINUTES = 150
DAY_START, DAY_END = 9 * 60, 19 * 60

EXPERIENCE, MEAL, TRANSFER, FREE_TIME = "experience", "meal", "transfer", "free_time"
HOTEL_LABEL = "your hotel"

ReplaceFn = Callable[[dict, Point, Point], Optional[dict]]
RouteFn = Callable[[Point, Point], dict]


# ==================== STOPS ====================

def max_stops_for(nights: int) -> int:
    if nights >= MIN_NIGHTS_FOR_THIRD_STOP:
        return 3
    if nights >= MIN_NIGHTS_FOR_SECOND_STOP:
        return 2
    return 1


def _single_stop(area: str, nights: int) -> List[dict]:
    return [{"stop": 1, "base_area": area, "nights": nights, "first_day": 1, "last_day": max(nights, 1)}]


def normalize_stops(raw_stops, nights: int, default_area: str) -> List[dict]:
    """
    Turn the model's proposed stops into a valid plan: at most max_stops_for(nights)
    stops, each of at least MIN_NIGHTS_PER_STOP nights, nights summing to the trip,
    consecutive days per stop. Anything unusable collapses to one stop.
    """
    nights = max(int(nights or 1), 1)
    stops: List[dict] = []
    for raw in raw_stops or []:
        if not isinstance(raw, dict):
            continue
        area = str(raw.get("base_area") or "").strip()
        try:
            stop_nights = int(raw.get("nights") or 0)
        except (TypeError, ValueError):
            stop_nights = 0
        if not area or stop_nights <= 0:
            continue
        if stops and stops[-1]["base_area"].lower() == area.lower():
            stops[-1]["nights"] += stop_nights
            continue
        stops.append({"base_area": area, "nights": stop_nights})

    if not stops:
        return _single_stop(default_area, nights)

    # Short stops are folded into the previous stop (or the next one for the
    # first) before the stop limit applies, so a short middle stop never
    # pushes a real region out of the plan.
    merged: List[dict] = []
    for stop in stops:
        if stop["nights"] < MIN_NIGHTS_PER_STOP and merged:
            merged[-1]["nights"] += stop["nights"]
        else:
            merged.append(dict(stop))
    if len(merged) > 1 and merged[0]["nights"] < MIN_NIGHTS_PER_STOP:
        merged[1]["nights"] += merged.pop(0)["nights"]
    merged = merged[:max_stops_for(nights)]

    difference = nights - sum(stop["nights"] for stop in merged)
    merged[-1]["nights"] += difference
    if merged[-1]["nights"] < MIN_NIGHTS_PER_STOP and len(merged) > 1:
        merged[-2]["nights"] += merged.pop()["nights"]
    if len(merged) == 1 or any(stop["nights"] <= 0 for stop in merged):
        return _single_stop(merged[0]["base_area"], nights)

    day = 1
    for number, stop in enumerate(merged, start=1):
        stop.update(stop=number, first_day=day, last_day=day + stop["nights"] - 1)
        day += stop["nights"]
    return merged


def stop_for_day(stops: List[dict], day: int) -> dict:
    for stop in stops:
        if stop["first_day"] <= day <= stop["last_day"]:
            return stop
    return stops[-1]


def is_transfer_day(stops: List[dict], day: int) -> bool:
    stop = stop_for_day(stops, day)
    return stop["stop"] > 1 and day == stop["first_day"]


# ==================== TIME HELPERS ====================

_CLOCK = re.compile(r"(\d{1,2}):(\d{2})\s*([AP]M)?", re.IGNORECASE)


def _clock_minutes(match) -> int:
    hour, minute, suffix = int(match.group(1)), int(match.group(2)), (match.group(3) or "").upper()
    if suffix == "PM" and hour != 12:
        hour += 12
    elif suffix == "AM" and hour == 12:
        hour = 0
    return hour * 60 + minute


def time_window(text: str) -> Optional[Tuple[int, int]]:
    times = list(_CLOCK.finditer(text or ""))
    if len(times) < 2:
        return None
    start, end = _clock_minutes(times[0]), _clock_minutes(times[1])
    return (start, end) if end > start else None


def start_minutes(text: str) -> Optional[int]:
    match = _CLOCK.search(text or "")
    return _clock_minutes(match) if match else None


def clock(minutes: int) -> str:
    minutes = max(0, min(int(minutes), 23 * 60 + 59))
    hour, minute = divmod(minutes, 60)
    suffix = "AM" if hour < 12 else "PM"
    return f"{(hour % 12) or 12:02d}:{minute:02d} {suffix}"


# ==================== DISTANCE ENFORCEMENT ====================

def _annotate(item: dict, from_base: Optional[Leg], from_previous: Optional[Leg], previous_name: str) -> dict:
    item["travel_minutes_from_previous"] = from_previous.minutes if from_previous else None
    item["distance_from_previous_km"] = from_previous.km if from_previous else None
    item["travel_from"] = previous_name if from_previous else None
    item["travel_minutes_from_base"] = from_base.minutes if from_base else None
    # Measured only when every leg came from Google Routes; otherwise the weakest source wins.
    sources = {leg.source for leg in (from_base, from_previous) if leg is not None}
    unmeasured = sorted(sources - {SOURCE_ROUTES})
    item["travel_time_source"] = unmeasured[0] if unmeasured else (SOURCE_ROUTES if sources else None)
    return item


def _worst(*legs: Leg) -> Optional[int]:
    if any(leg.minutes is None for leg in legs):
        return None
    return max(leg.minutes for leg in legs)


def enforce_day(
    day: dict,
    stop: dict,
    travel: TravelTimes,
    replace: ReplaceFn,
    max_minutes: int = MAX_LEG_MINUTES,
) -> List[dict]:
    """
    Keep only items within `max_minutes` of the stop's base and of the previous
    item; replace or remove the rest. Annotates every kept item with its travel
    time and returns the adjustments made (for one guest note each).
    """
    base = point_of(stop.get("base") or {})
    items = day.get("activities", [])
    if base is None:
        for item in items:
            if item.get("item_type") not in (TRANSFER, FREE_TIME):
                _annotate(item, None, None, HOTEL_LABEL)
        return []

    located = [point for point in (point_of(item) for item in items) if point is not None]
    if located:
        # One batched request: base -> items, item -> item and item -> base (the drive back).
        travel.prefetch([base, *located], [base, *located])

    adjustments: List[dict] = []
    kept: List[dict] = []
    previous_point, previous_name = base, HOTEL_LABEL
    for item in items:
        if item.get("item_type") in (TRANSFER, FREE_TIME):
            kept.append(item)
            continue
        point = point_of(item)
        if point is None:
            # Not located on the map: kept, but validation flags it and it is never bookable.
            kept.append(_annotate(item, None, None, previous_name))
            continue
        from_base, from_previous = travel.leg(base, point), travel.leg(previous_point, point)
        if not (from_base.within(max_minutes) and from_previous.within(max_minutes)):
            original_name = item.get("activity_name", "")
            guest_facing = item.get("item_type", EXPERIENCE) == EXPERIENCE
            too_far = _worst(from_base, from_previous)
            replacement = replace(item, base, previous_point)
            replacement_point = point_of(replacement) if replacement else None
            if replacement is not None and replacement_point is None:
                # A flexible placeholder (e.g. an open meal slot) with no fixed location.
                adjustments.append(_adjustment("replaced", day, stop, original_name, too_far, replacement, guest_facing))
                kept.append(_annotate(replacement, None, None, previous_name))
                continue
            if replacement is not None:
                new_base, new_previous = travel.leg(base, replacement_point), travel.leg(previous_point, replacement_point)
                if new_base.within(max_minutes) and new_previous.within(max_minutes):
                    adjustments.append(_adjustment("replaced", day, stop, original_name, too_far, replacement, guest_facing))
                    item, point, from_base, from_previous = replacement, replacement_point, new_base, new_previous
                else:
                    replacement = None
            if replacement is None:
                adjustments.append(_adjustment("removed", day, stop, original_name, too_far, None, guest_facing))
                continue
        kept.append(_annotate(item, from_base, from_previous, previous_name))
        # Travel is described from the place itself: "Harbour Table", not "Lunch at Harbour Table".
        previous_point, previous_name = point, item.get("restaurant_name") or item.get("activity_name", "")
    if previous_point != base:
        # The drive back to the hotel at the end of the day, for transport pricing.
        last = next(item for item in reversed(kept) if point_of(item) == previous_point)
        last["return_to_base_km"] = travel.leg(previous_point, base).km
    day["activities"] = kept
    return adjustments


def _adjustment(kind: str, day: dict, stop: dict, original: str, minutes: Optional[int], replacement: Optional[dict],
                guest_facing: bool = True) -> dict:
    distance = (
        f"{minutes_phrase(minutes)} from your hotel in {stop['base_area']}"
        if minutes is not None
        else f"not reachable by road from your hotel in {stop['base_area']}"
    )
    if kind == "replaced":
        new_name = replacement.get("activity_name", "a closer option")
        guest_note = f"Day {day.get('day')}: we swapped {original} for {new_name}, because {original} is {distance}."
    else:
        guest_note = f"Day {day.get('day')}: we left out {original}, because it is {distance}."
    return {
        "type": kind,
        "day": day.get("day"),
        "stop": stop.get("stop"),
        "item": original,
        "replacement": replacement.get("activity_name") if replacement else None,
        "travel_minutes": minutes,
        # Restaurant swaps are ours to make; only changes to planned experiences get a guest note.
        "guest_note": guest_note if guest_facing else None,
    }


# ==================== TRANSFERS ====================

def transfer_item(from_stop: dict, to_stop: dict, route_fn: RouteFn) -> dict:
    """The overnight move between two stops, with realistic buffers."""
    origin, destination = point_of(from_stop.get("base") or {}), point_of(to_stop.get("base") or {})
    minutes = km = None
    includes_ferry = False
    source = None
    if origin and destination:
        try:
            route = route_fn(origin, destination) or {}
        except Exception as error:
            route = {"error": str(error)}
        if "error" not in route and route.get("minutes") is not None:
            minutes, km, includes_ferry, source = route["minutes"], route.get("km"), bool(route.get("includes_ferry")), SOURCE_ROUTES
        else:
            estimate = estimate_leg(origin, destination)
            minutes, km, source = estimate.minutes, estimate.km, SOURCE_ESTIMATE
    buffer = 0
    if minutes is not None:
        buffer = max(TRANSFER_MIN_BUFFER_MINUTES, round(minutes * TRANSFER_ROAD_BUFFER_RATIO))
        if includes_ferry:
            buffer += FERRY_BUFFER_MINUTES
    total = minutes + buffer if minutes is not None else None
    route_text = f"{minutes_phrase(minutes)} by road" if minutes is not None else "a road transfer"
    ferry_text = ", including a ferry crossing" if includes_ferry else ""
    return {
        "item_type": TRANSFER,
        "activity_name": f"Transfer from {from_stop['base_area']} to {to_stop['base_area']}",
        "activity_description": (
            f"{route_text[:1].upper()}{route_text[1:]}{ferry_text}, with time built in for breaks and check-in. "
            f"Check out in the morning and settle into your new base in {to_stop['base_area']}."
        ),
        "activity_location": to_stop["base_area"],
        "activity_address": "N/A",
        "activity_image": [],
        "activity_time": f"{clock(TRANSFER_START)} - {clock(TRANSFER_START + (total or 180))}",
        "activity_cost": 0.0,
        "why_selected": (
            f"The experiences planned around {to_stop['base_area']} are too far from {from_stop['base_area']} "
            "for day trips, so the trip moves base with one overnight transfer instead."
        ),
        "transfer_from": from_stop["base_area"],
        "transfer_minutes": minutes,
        "transfer_buffer_minutes": buffer,
        "transfer_km": km,
        "includes_ferry": includes_ferry,
        "travel_time_source": source,
        "latitude": None,
        "longitude": None,
    }


def _departure_meal(meal: dict, origin: str, destination: str) -> dict:
    """A meal before checkout, left open near the hotel the guest is leaving."""
    name = meal.get("meal") or "Breakfast"
    return {
        **meal,
        "activity_name": f"{name} before you leave {origin}",
        "activity_description": f"{name} at or near your hotel in {origin} before checking out.",
        "activity_location": origin,
        "activity_address": "N/A",
        "activity_image": [],
        "restaurant_name": None,
        "rating": None,
        "price_indication": None,
        "place_id": None,
        "business_status": None,
        "open_slot": True,
        "latitude": None,
        "longitude": None,
        "travel_minutes_from_previous": None,
        "travel_minutes_from_base": None,
        "distance_from_previous_km": None,
        "travel_from": None,
        "travel_time_source": None,
        "return_to_base_km": None,
        "why_selected": f"An easy start in {origin} before the drive to {destination}.",
    }


def apply_transfer_day(day: dict, transfer: dict, max_experiences: int = TRANSFER_DAY_MAX_EXPERIENCES) -> List[dict]:
    """
    Put the transfer first, drop anything scheduled during it, and keep the
    rest of the day light. Returns adjustments for what was moved out.
    """
    window = time_window(transfer["activity_time"]) or (TRANSFER_START, TRANSFER_START + 180)
    origin = transfer.get("transfer_from") or "your first base"
    adjustments, kept, experiences = [], [transfer], 0
    for item in day.get("activities", []):
        item_window = time_window(item.get("activity_time", ""))
        before_departure = item_window is not None and item_window[1] <= window[0]
        if before_departure and item.get("item_type") == MEAL:
            # The guest is still at the previous base: no restaurant near the new one.
            kept.append(_departure_meal(item, origin, transfer.get("activity_location", "")))
            continue
        overlaps = item_window is not None and item_window[0] < window[1] and window[0] < item_window[1]
        is_experience = item.get("item_type", EXPERIENCE) == EXPERIENCE
        if before_departure and is_experience:
            overlaps = True  # Nothing is planned at the new base before the guest arrives.
        if overlaps or (is_experience and experiences >= max_experiences):
            if is_experience:
                adjustments.append({
                    "type": "removed",
                    "day": day.get("day"),
                    "item": item.get("activity_name", ""),
                    "replacement": None,
                    "travel_minutes": None,
                    "guest_note": (
                        f"Day {day.get('day')}: we kept your travel day light and left out "
                        f"{item.get('activity_name', '')}."
                    ),
                })
            continue
        experiences += is_experience
        kept.append(item)
    # Chronological: breakfast before checkout comes ahead of the transfer.
    day["activities"] = sorted(kept, key=lambda item: start_minutes(item.get("activity_time", "")) or 24 * 60)
    day["day_type"] = "transfer"
    return adjustments


# ==================== GUEST REASONS ====================

def describe_stay(stop: dict, days: List[dict], max_minutes: int = MAX_LEG_MINUTES) -> str:
    """Why this stay is the base: how central it is to the planned experiences."""
    nights = stop["nights"]
    hotel = stop.get("hotel") or {}
    times = [
        item["travel_minutes_from_base"]
        for day in days if stop["first_day"] <= day.get("day", 0) <= stop["last_day"]
        for item in day.get("activities", [])
        if item.get("item_type") == EXPERIENCE and item.get("travel_minutes_from_base") is not None
    ]
    rating = f", rated {float(hotel['rating']):.1f} on Google" if hotel.get("rating") else ""
    reason = f"Your base for {nights} night{'s' if nights != 1 else ''} in {stop['base_area']}{rating}."
    if times:
        within = sum(minutes <= max_minutes for minutes in times)
        average = round(sum(times) / len(times))
        planned = f"{len(times)} planned experience{'s' if len(times) != 1 else ''}"
        reason += (
            f" All {planned} are within an hour of it" if within == len(times) and len(times) > 1
            else f" {within} of your {planned} {'is' if within == 1 else 'are'} within an hour of it"
        ) + f", about {average} minutes away on average."
    return reason


def describe_meal(item: dict, meal: str, next_name: Optional[str], dietary_terms: List[str]) -> str:
    """A specific reason for a restaurant, from its rating and real travel time."""
    name = item.get("restaurant_name") or item.get("activity_location") or "This restaurant"
    facts = []
    if item.get("rating"):
        facts.append(f"rated {float(item['rating']):.1f} on Google")
    if item.get("travel_minutes_from_previous") is not None and item.get("travel_from"):
        facts.append(f"{minutes_phrase(item['travel_minutes_from_previous'])} from {item['travel_from']}")
    lead = f"{name} is {natural_list(facts)}" if facts else f"{name} keeps your {meal.lower()} close to your plans"
    if meal == "Breakfast":
        purpose = f"an easy start before {next_name}" if next_name else "an easy, unhurried start to the day"
    elif meal == "Lunch":
        purpose = f"a natural pause before {next_name}" if next_name else "a relaxed midday break"
    else:
        purpose = "a relaxed close to the day without another long drive"
    reason = f"{lead}, making it {purpose}."
    if dietary_terms:
        reason += f" Ask about {natural_list(dietary_terms, 'or')} options when you book."
    return reason


def add_free_time(day: dict, pace_phrase: str) -> None:
    """Describe long unscheduled gaps on purpose instead of leaving them blank."""
    timed = sorted(
        (
            (window, item)
            for item in day.get("activities", [])
            for window in [time_window(item.get("activity_time", ""))]
            if window is not None
        ),
        key=lambda pair: pair[0][0],
    )
    additions = []
    for (first_window, first), (second_window, _second) in zip(timed, timed[1:]):
        gap_start, gap_end = max(first_window[1], DAY_START), min(second_window[0], DAY_END)
        if gap_end - gap_start >= FREE_TIME_MIN_GAP_MINUTES:
            near = (
                f"you arrive in {first.get('activity_location')}" if first.get("item_type") == TRANSFER
                else first.get("restaurant_name") or first.get("activity_name", "")
            )
            additions.append({
                "item_type": FREE_TIME,
                "activity_name": "Open time",
                "activity_description": (
                    f"Unscheduled time after {near}: rest at your hotel, wander nearby or return to a favourite spot."
                ),
                "activity_location": first.get("activity_location", ""),
                "activity_address": "N/A",
                "activity_image": [],
                "activity_time": f"{clock(gap_start)} - {clock(gap_end)}",
                "activity_cost": 0.0,
                "why_selected": f"Left open on purpose, to match your preferred pace: {pace_phrase}.",
                "latitude": None,
                "longitude": None,
            })
    if additions:
        day["activities"] = sorted(
            [*day["activities"], *additions],
            key=lambda item: start_minutes(item.get("activity_time", "")) or 24 * 60,
        )
