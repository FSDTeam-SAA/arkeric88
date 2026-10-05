"""
Final itinerary validation (client brief, requirements 1 and 10).

Runs after recommendations are selected and before an itinerary is shown or
saved. It is a fixed checklist over the finished itinerary -- no model call:

- every stop has a located base and a stay found on the map;
- the stay sits where most of that stop's experiences are;
- every experience and restaurant is located and within the travel limit of
  the base and the previous stop (transfers between stops are exempt);
- every stop after the first starts with a transfer;
- every recommendation has a specific, guest-facing reason;
- no backend wording reaches guest-facing text;
- the price total could be calculated.

`booking_status` is separate: an itinerary is only "ready to book" when it
passed validation AND its stays, experiences and sources are verified.
"""

from datetime import datetime, timezone
from typing import List, Optional

from src.core.guest_text import has_internal_wording, is_generic_reason
from src.core.itinerary_geo import EXPERIENCE, FREE_TIME, MEAL, TRANSFER
from src.core.travel_time import MAX_LEG_MINUTES, SOURCE_ESTIMATE, SOURCE_NO_ROUTE, point_of

ERROR, WARNING = "error", "warning"
# The stay should be the natural base: at least this share of the stop's
# located experiences must be within the travel limit of it.
MIN_SHARE_NEAR_STAY = 0.5

READY_LABEL = "Ready to book"
NOT_READY_LABEL = "Prices and availability will be confirmed before booking"


def _issue(code: str, severity: str, detail: str, day: Optional[int] = None, item: Optional[str] = None) -> dict:
    return {"code": code, "severity": severity, "detail": detail, "day": day, "item": item}


def _stop_days(stop: dict, days: List[dict]) -> List[dict]:
    return [day for day in days if stop["first_day"] <= day.get("day", 0) <= stop["last_day"]]


def _check_stop(stop: dict, days: List[dict], max_minutes: int) -> List[dict]:
    issues = []
    area = stop["base_area"]
    if point_of(stop.get("base") or {}) is None:
        issues.append(_issue("STOP_BASE_UNLOCATED", ERROR, f"The base for {area} could not be located, so travel times were not checked."))
    if not stop.get("stay_found"):
        issues.append(_issue("STAY_NOT_FOUND", ERROR, f"No real stay was found on the map for {area}."))

    times = [
        item.get("travel_minutes_from_base")
        for day in _stop_days(stop, days)
        for item in day.get("activities", [])
        if item.get("item_type", EXPERIENCE) == EXPERIENCE and point_of(item) is not None
    ]
    measured = [minutes for minutes in times if minutes is not None]
    if measured and sum(minutes <= max_minutes for minutes in measured) / len(measured) < MIN_SHARE_NEAR_STAY:
        issues.append(_issue("STAY_NOT_CENTRAL", ERROR, f"The stay in {area} is not near most of the planned experiences."))

    if stop["stop"] > 1:
        first_day = next((day for day in days if day.get("day") == stop["first_day"]), None)
        if first_day is None or not any(item.get("item_type") == TRANSFER for item in first_day.get("activities", [])):
            issues.append(_issue("TRANSFER_MISSING", ERROR, f"Day {stop['first_day']} moves to {area} without a transfer.", stop["first_day"]))
    return issues


def _check_item(item: dict, day: int, max_minutes: int) -> List[dict]:
    issues = []
    name = item.get("activity_name", "")
    kind = item.get("item_type", EXPERIENCE)
    # Names come from Google and may legitimately contain "|"; only text we write is checked.
    guest_text = " ".join(str(item.get(field) or "") for field in ("activity_description", "why_selected"))
    if has_internal_wording(guest_text):
        issues.append(_issue("INTERNAL_WORDING", ERROR, "Guest-facing text contains internal wording.", day, name))
    if kind in (TRANSFER, FREE_TIME):
        return issues

    if is_generic_reason(item.get("why_selected")):
        issues.append(_issue("REASON_MISSING", WARNING, "No specific reason is given for this recommendation.", day, name))
    if point_of(item) is None:
        if not item.get("open_slot"):
            issues.append(_issue("ITEM_UNLOCATED", WARNING, "Not located on the map, so travel time was not checked.", day, name))
        return issues
    if item.get("travel_time_source") == SOURCE_NO_ROUTE:
        issues.append(_issue("NO_DRIVABLE_ROUTE", ERROR, "No drivable route from the base or previous stop.", day, name))
    for field, label in (("travel_minutes_from_base", "the base"), ("travel_minutes_from_previous", "the previous stop")):
        minutes = item.get(field)
        if minutes is not None and minutes > max_minutes:
            issues.append(_issue("LEG_OVER_LIMIT", ERROR, f"{minutes} minutes from {label}, over the {max_minutes}-minute limit.", day, name))
    return issues


def validate_itinerary(
    stops: List[dict],
    days: List[dict],
    price_breakdown: dict,
    max_minutes: int = MAX_LEG_MINUTES,
) -> dict:
    issues: List[dict] = []
    for stop in stops:
        issues.extend(_check_stop(stop, days, max_minutes))
    estimated_legs = 0
    for day in days:
        for item in day.get("activities", []):
            issues.extend(_check_item(item, day.get("day"), max_minutes))
            estimated_legs += item.get("travel_time_source") == SOURCE_ESTIMATE
    if estimated_legs:
        issues.append(_issue(
            "TRAVEL_TIME_ESTIMATED", WARNING,
            f"{estimated_legs} travel time(s) are straight-line estimates because live routing was unavailable.",
        ))
    if price_breakdown.get("total") is None:
        issues.append(_issue("PRICING_INCOMPLETE", WARNING, price_breakdown.get("total_withheld_reason") or "The total could not be calculated."))

    errors = sum(issue["severity"] == ERROR for issue in issues)
    warnings = len(issues) - errors
    return {
        "status": "failed" if errors else ("passed_with_warnings" if warnings else "passed"),
        "display_ready": errors == 0,
        "max_leg_minutes": max_minutes,
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "error_count": errors,
        "warning_count": warnings,
        "issues": issues,
    }


def booking_status(validation: dict, stops: List[dict], days: List[dict], destination_verified: bool) -> dict:
    """Bookable only when every check passed and every component is verified."""
    reasons = []
    if validation["status"] != "passed":
        reasons.append("Not every itinerary check passed.")
    if not destination_verified:
        reasons.append("Destination details have not been verified against a destination-specific source.")
    if any((stop.get("hotel") or {}).get("price_status") != "LIVE" for stop in stops):
        reasons.append("Room rates and availability have not been confirmed with a booking provider.")
    items = [item for day in days for item in day.get("activities", [])]
    if any(item.get("item_type", EXPERIENCE) in (EXPERIENCE, MEAL) and not item.get("place_id") and not item.get("open_slot") for item in items):
        reasons.append("Some recommendations are not confirmed on the map.")
    if any(item.get("item_type", EXPERIENCE) == EXPERIENCE and item.get("availability_status") != "CONFIRMED" for item in items):
        reasons.append("Experience availability has not been confirmed with the operators.")
    ready = not reasons
    return {
        "ready_to_book": ready,
        "guest_label": READY_LABEL if ready else NOT_READY_LABEL,
        "reasons": reasons,
    }
