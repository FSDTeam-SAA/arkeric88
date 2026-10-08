"""
Build a normalized TripProfile from a validated TravelIntakeRequest.

This is a deterministic translation (table lookups in intake_mappings.py, no
LLM) used by both destination matching and the itinerary step, so the two
always read the intake the same way. The guest's private free text
(recent_feelings_other, trip_prompt_other) is deliberately NOT copied onto the
profile: everything downstream of this module -- matching, prompts, tool
queries -- only ever sees option codes and labels.
"""

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Dict, List, Optional

from app.schemas.intake_schema import TravelIntakeRequest
from src.core.intake_mappings import (
    ENVIRONMENT_LABELS,
    ITINERARY_LEVEL_RESTRICTIONS,
    PREFERRED_MOMENT_LABELS,
    RECENT_FEELING_LABELS,
    RESTRICTION_LABELS,
    SEASON_PERIODS,
    TRAVEL_PERIOD_LABELS,
    TRAVEL_PERIOD_TO_MONTHS,
    TRIP_GOAL_DEFINITIONS,
    TRIP_GOAL_LABELS,
    TRIP_PACE_LABELS,
    TRIP_PACE_TO_CATALOG_PACE,
    TRIP_PROMPT_LABELS,
    TRAVEL_PARTY_LABELS,
)

# A standard room sleeps two adults; above that the guest should confirm the
# room count (INTAKE_MAPPING.csv nightly_budget: "Confirm rooms if party
# needs more than one").
ADULTS_PER_STANDARD_ROOM = 2
GUESTS_PER_STANDARD_ROOM = 4


@dataclass
class TripProfile:
    goals: List[str]
    moments: List[str]
    environments: List[str]
    surprise_me: bool
    pace: str
    catalog_pace: str

    travel_party: str
    adults: int
    children: int
    rooms: int
    child_ages: Optional[List[int]]
    guest_nationality: Optional[str]
    hotel_room_occupancies: Optional[List[dict]]

    # restriction code -> "must_avoid" | "prefer_avoid"
    restrictions: Dict[str, str]
    restriction_notes: Optional[str]

    departure_location: str
    travel_distance: str

    travel_timing: str
    check_in_date: Optional[date]
    check_out_date: Optional[date]
    travel_period: Optional[str]
    nights: int
    travel_months: List[int]

    budget_per_night: float
    budget_open_ended: bool

    recent_feelings: List[str]
    trip_prompt: str
    has_private_context: bool

    clarifications: List[dict] = field(default_factory=list)

    # ---------- derived views ----------

    @property
    def party_size(self) -> int:
        return self.adults + self.children

    @property
    def must_avoid(self) -> List[str]:
        return [code for code, severity in self.restrictions.items() if severity == "must_avoid"]

    @property
    def prefer_avoid(self) -> List[str]:
        return [code for code, severity in self.restrictions.items() if severity == "prefer_avoid"]

    @property
    def has_exact_dates(self) -> bool:
        return self.travel_timing == "exact_dates"

    @property
    def goal_labels(self) -> List[str]:
        return [TRIP_GOAL_LABELS[goal] for goal in self.goals]

    @property
    def moment_labels(self) -> List[str]:
        return [PREFERRED_MOMENT_LABELS[moment] for moment in self.moments]

    @property
    def environment_labels(self) -> List[str]:
        return [ENVIRONMENT_LABELS[environment] for environment in self.environments]

    def restriction_phrases(self) -> List[str]:
        phrases = []
        for code, severity in self.restrictions.items():
            verb = "Must avoid" if severity == "must_avoid" else "Prefer to avoid"
            phrases.append(f"{verb}: {RESTRICTION_LABELS[code]}")
        return phrases

    def timing_phrase(self) -> str:
        if self.has_exact_dates:
            return f"{self.check_in_date.isoformat()} to {self.check_out_date.isoformat()} ({self.nights} nights)"
        if self.travel_period:
            return f"{TRAVEL_PERIOD_LABELS[self.travel_period]}, {self.nights} nights (dates not fixed)"
        return f"Flexible dates, {self.nights} nights"

    def party_phrase(self) -> str:
        parts = [f"{self.adults} adult{'s' if self.adults != 1 else ''}"]
        if self.children:
            ages = f" (ages {', '.join(str(age) for age in self.child_ages)})" if self.child_ages else ""
            parts.append(f"{self.children} child{'ren' if self.children != 1 else ''}{ages}")
        parts.append(f"{self.rooms} room{'s' if self.rooms != 1 else ''}")
        return f"{TRAVEL_PARTY_LABELS[self.travel_party]}: " + ", ".join(parts)

    def guest_context(self) -> dict:
        """Guest-facing reflection of their own answers (labels only, no conclusions)."""
        return {
            "recent_feelings": [RECENT_FEELING_LABELS[code] for code in self.recent_feelings],
            "trip_goals": [
                {"code": goal, "label": TRIP_GOAL_LABELS[goal], "definition": TRIP_GOAL_DEFINITIONS[goal]}
                for goal in self.goals
            ],
            "trip_prompt": TRIP_PROMPT_LABELS[self.trip_prompt],
            "preferred_moments": self.moment_labels,
            "preferred_environments": self.environment_labels,
            "trip_pace": TRIP_PACE_LABELS[self.pace],
            "party": self.party_phrase(),
            "restrictions": self.restriction_phrases(),
            "timing": self.timing_phrase(),
            "budget_per_night_usd": self.budget_per_night,
            "budget_open_ended": self.budget_open_ended,
        }


def _months_between(check_in: date, check_out: date) -> List[int]:
    months: List[int] = []
    day = check_in
    while day < check_out:
        if day.month not in months:
            months.append(day.month)
        day += timedelta(days=1)
    return months


def _clarifications(request: TravelIntakeRequest, restrictions: Dict[str, str]) -> List[dict]:
    """Questions to put back to the guest (INTAKE_MAPPING.csv guest-facing handling)."""
    questions: List[dict] = []

    if restrictions.get("other") == "must_avoid" and not request.restriction_notes:
        questions.append({
            "field": "restriction_notes",
            "blocking": False,
            "question": "You marked an 'Other' restriction as something you must avoid. What should we plan around?",
        })

    for code in sorted(ITINERARY_LEVEL_RESTRICTIONS & set(restrictions)):
        if not request.restriction_notes:
            questions.append({
                "field": "restriction_notes",
                "blocking": False,
                "question": f"Tell us more about your {RESTRICTION_LABELS[code].lower()} so we can check restaurants and stays.",
            })

    if (
        request.party_rooms == 1
        and (request.party_adults > ADULTS_PER_STANDARD_ROOM or request.party_adults + request.party_children > GUESTS_PER_STANDARD_ROOM)
    ):
        questions.append({
            "field": "party_rooms",
            "blocking": False,
            "question": (
                f"{request.party_adults + request.party_children} guests in one room may exceed standard room "
                "occupancy. How many rooms do you need? Your budget applies per room, per night."
            ),
        })

    if request.party_children and request.party_child_ages is None:
        questions.append({
            "field": "party_child_ages",
            "blocking": False,
            "question": "How old are the children? Live room rates and some activities depend on ages.",
        })

    if request.trip_prompt.value == "something_else":
        questions.append({
            "field": "trip_prompt",
            "blocking": False,
            "question": "Is there a date or occasion we should plan the trip around?",
        })

    return questions


def build_trip_profile(request: TravelIntakeRequest) -> TripProfile:
    restrictions = {
        restriction.value: request.restriction_severity[restriction].value
        for restriction in request.activity_restrictions
    }
    environments = [environment.value for environment in request.preferred_environments]

    if request.check_in_date and request.check_out_date:
        travel_months = _months_between(request.check_in_date, request.check_out_date)
    elif request.travel_period:
        travel_months = list(TRAVEL_PERIOD_TO_MONTHS[request.travel_period.value])
    else:
        travel_months = []

    return TripProfile(
        goals=[goal.value for goal in request.trip_goals],
        moments=[moment.value for moment in request.preferred_moments],
        environments=environments,
        surprise_me="surprise_me" in environments,
        pace=request.trip_pace.value,
        catalog_pace=TRIP_PACE_TO_CATALOG_PACE[request.trip_pace.value],
        travel_party=request.travel_party.value,
        adults=request.party_adults,
        children=request.party_children,
        rooms=request.party_rooms,
        child_ages=list(request.party_child_ages) if request.party_child_ages is not None else None,
        guest_nationality=request.guest_nationality,
        hotel_room_occupancies=list(request.hotel_room_occupancies) if request.hotel_room_occupancies is not None else None,
        restrictions=restrictions,
        restriction_notes=request.restriction_notes,
        departure_location=request.departure_location,
        travel_distance=request.travel_distance.value,
        travel_timing=request.travel_timing.value,
        check_in_date=request.check_in_date,
        check_out_date=request.check_out_date,
        travel_period=request.travel_period.value if request.travel_period else None,
        nights=request.trip_nights,
        travel_months=travel_months,
        # Not asked yet when dates are suggested at step 10 (DateRecommendationRequest).
        budget_per_night=float(request.budget_per_night or 0),
        budget_open_ended=request.budget_open_ended,
        recent_feelings=[feeling.value for feeling in request.recent_feelings],
        trip_prompt=request.trip_prompt.value,
        has_private_context=bool(request.recent_feelings_other or request.trip_prompt_other),
        clarifications=_clarifications(request, restrictions),
    )


def season_months_are_assumed(profile: TripProfile) -> bool:
    """True when months came from a season name (Northern-hemisphere assumption)."""
    return profile.travel_period in SEASON_PERIODS
