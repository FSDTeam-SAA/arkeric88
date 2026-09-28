"""Shared payload builders for the Velari intake tests."""

from datetime import date, timedelta
from typing import Optional


def build_intake(**overrides) -> dict:
    """A valid 11-step intake payload; override any form key."""
    payload = {
        "recent_feelings": ["stretched_thin"],
        "trip_goals": ["restoration", "reflection"],
        "trip_prompt": "need_a_break",
        "preferred_moments": ["quiet_privacy", "spa_wellness", "nature_wildlife"],
        "preferred_environments": ["coast", "mountains"],
        "trip_pace": "one_highlight",
        "travel_party": "solo",
        "departure_location": "Lisbon",
        "travel_distance": "anywhere",
        "travel_timing": "flexible",
        "trip_nights": 5,
        "budget_per_night": 400,
    }
    payload.update(overrides)
    return payload


def exact_dates(start_in_days: int = 60, nights: int = 5, start: Optional[date] = None) -> dict:
    """Overrides for an exact-dates intake."""
    check_in = start or (date.today() + timedelta(days=start_in_days))
    return {
        "travel_timing": "exact_dates",
        "check_in_date": check_in.isoformat(),
        "check_out_date": (check_in + timedelta(days=nights)).isoformat(),
        "trip_nights": nights,
    }


def with_restriction(payload: dict, restriction: str, severity: str) -> dict:
    """Add one restriction using the form's flat severity key."""
    payload = dict(payload)
    payload["activity_restrictions"] = [*payload.get("activity_restrictions", []), restriction]
    payload[f"restriction_severity_{restriction}"] = severity
    return payload
