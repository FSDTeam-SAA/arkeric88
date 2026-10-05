"""
Request schema for the Velari emotional travel intake (formId
"velari_emotional_travel_intake", version 1, 11 steps).

Field names match the form's `key` values one-to-one, so the frontend can
submit its form state directly. The form's per-option restriction severity
fields (`restriction_severity_{selected_option}`) are accepted either as those
flat keys or as a single `restriction_severity` object.

Validation mirrors the form: selection limits, exclusive options, conditional
"requiredWhenVisible" fields, party defaults, date ordering and the budget
slider range. Conditional fields that are not visible for the given answers
are dropped rather than rejected, so stale form state never leaks into
matching (and private free text that no longer applies is not stored).
"""

from datetime import date
from enum import Enum
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, model_validator

INTAKE_FORM_ID = "velari_emotional_travel_intake"
INTAKE_FORM_VERSION = 1

# Validation context flag for re-reading an intake already stored in a
# session: its check-in date was valid when submitted and must not start
# failing just because that day has since passed.
STORED_INTAKE_CONTEXT = {"stored_intake": True}

BUDGET_MINIMUM = 100
BUDGET_MAXIMUM = 7000  # The slider's top value, shown as "$7,000+".
MAX_TRIP_NIGHTS = 90


class RecentFeeling(str, Enum):
    stretched_thin = "stretched_thin"
    stuck_in_routine = "stuck_in_routine"
    disconnected = "disconnected"
    curious = "curious"
    energized = "energized"
    turning_point = "turning_point"
    content_ready = "content_ready"
    something_else = "something_else"


class TripGoal(str, Enum):
    restoration = "restoration"
    connection = "connection"
    discovery = "discovery"
    adventure = "adventure"
    inspiration = "inspiration"
    celebration = "celebration"
    reflection = "reflection"
    growth = "growth"


class TripPrompt(str, Enum):
    need_a_break = "need_a_break"
    time_with_someone = "time_with_someone"
    celebrating = "celebrating"
    curious_to_explore = "curious_to_explore"
    ready_for_change = "ready_for_change"
    change_of_scenery = "change_of_scenery"
    no_particular_reason = "no_particular_reason"
    something_else = "something_else"


class PreferredMoment(str, Enum):
    food_drinks = "food_drinks"
    art_history_culture = "art_history_culture"
    nature_wildlife = "nature_wildlife"
    beaches_water = "beaches_water"
    movement_adventure = "movement_adventure"
    quiet_privacy = "quiet_privacy"
    meeting_people = "meeting_people"
    spa_wellness = "spa_wellness"
    music_nightlife = "music_nightlife"
    learning_making = "learning_making"


class PreferredEnvironment(str, Enum):
    coast = "coast"
    mountains = "mountains"
    forest_jungle = "forest_jungle"
    desert = "desert"
    countryside = "countryside"
    small_town = "small_town"
    vibrant_city = "vibrant_city"
    surprise_me = "surprise_me"


class TripPace(str, Enum):
    mostly_open = "mostly_open"
    one_highlight = "one_highlight"
    balanced = "balanced"
    full_days = "full_days"


class TravelParty(str, Enum):
    solo = "solo"
    couple = "couple"
    group = "group"
    family = "family"


class ActivityRestriction(str, Enum):
    mobility_accessibility = "mobility_accessibility"
    food_dietary = "food_dietary"
    no_long_drives = "no_long_drives"
    no_intense_activity = "no_intense_activity"
    no_water_activities = "no_water_activities"
    avoid_extreme_heat = "avoid_extreme_heat"
    avoid_cold_weather = "avoid_cold_weather"
    other = "other"


class RestrictionSeverity(str, Enum):
    must_avoid = "must_avoid"
    prefer_avoid = "prefer_avoid"


class TravelDistance(str, Enum):
    nearby = "nearby"
    manageable_flight = "manageable_flight"
    anywhere = "anywhere"


class TravelTiming(str, Enum):
    exact_dates = "exact_dates"
    flexible = "flexible"
    month_season = "month_season"


class TravelPeriod(str, Enum):
    spring = "spring"
    summer = "summer"
    autumn = "autumn"
    winter = "winter"
    january = "january"
    february = "february"
    march = "march"
    april = "april"
    may = "may"
    june = "june"
    july = "july"
    august = "august"
    september = "september"
    october = "october"
    november = "november"
    december = "december"


# Party defaults from the form's travel_party options.
PARTY_DEFAULTS = {
    TravelParty.solo: {"party_adults": 1, "party_children": 0, "party_rooms": 1},
    TravelParty.couple: {"party_adults": 2, "party_children": 0, "party_rooms": 1},
    TravelParty.group: {"party_adults": 1, "party_children": 0, "party_rooms": 1},
    TravelParty.family: {"party_adults": 1, "party_children": 0, "party_rooms": 1},
}

_SEVERITY_KEY_PREFIX = "restriction_severity_"


def _unique(values: list) -> bool:
    return len(set(values)) == len(values)


def _blank(value: Optional[str]) -> bool:
    return value is None or not value.strip()


class TravelIntakeRequest(BaseModel):
    """POST /get_suggested_city request body -- the 11-step Velari intake."""

    model_config = ConfigDict(extra="forbid")

    form_id: Literal["velari_emotional_travel_intake"] = INTAKE_FORM_ID
    form_version: Literal[1] = INTAKE_FORM_VERSION

    # Step 1 -- context only, never scored.
    recent_feelings: List[RecentFeeling] = Field(min_length=1, max_length=2)
    recent_feelings_other: Optional[str] = Field(default=None, max_length=500)

    # Step 2 -- the primary emotional signal.
    trip_goals: List[TripGoal] = Field(min_length=1, max_length=2)

    # Step 3
    trip_prompt: TripPrompt
    trip_prompt_other: Optional[str] = Field(default=None, max_length=500)

    # Step 4
    preferred_moments: List[PreferredMoment] = Field(min_length=1, max_length=3)

    # Step 5
    preferred_environments: List[PreferredEnvironment] = Field(min_length=1, max_length=2)

    # Step 6
    trip_pace: TripPace

    # Step 7. party_child_ages is not on the form yet; live rates need it
    # (IMPORT_RULES.csv "Exact dates"), so it is accepted when available.
    travel_party: TravelParty
    party_adults: Optional[int] = Field(default=None, ge=1, le=30)
    party_children: Optional[int] = Field(default=None, ge=0, le=20)
    party_rooms: Optional[int] = Field(default=None, ge=1, le=20)
    party_child_ages: Optional[List[int]] = None

    # Step 8 -- optional.
    activity_restrictions: List[ActivityRestriction] = Field(default_factory=list)
    restriction_severity: Dict[ActivityRestriction, RestrictionSeverity] = Field(default_factory=dict)
    restriction_notes: Optional[str] = Field(default=None, max_length=1000)

    # Step 9. Coordinates/country are optional extras a places-autocomplete
    # widget can send; without them the backend geocodes departure_location.
    departure_location: str = Field(min_length=1, max_length=200)
    departure_latitude: Optional[float] = Field(default=None, ge=-90, le=90)
    departure_longitude: Optional[float] = Field(default=None, ge=-180, le=180)
    departure_country: Optional[str] = Field(default=None, max_length=100)
    travel_distance: TravelDistance

    # Step 10
    travel_timing: TravelTiming
    check_in_date: Optional[date] = None
    check_out_date: Optional[date] = None
    travel_period: Optional[TravelPeriod] = None
    trip_nights: Optional[int] = Field(default=None, ge=1, le=MAX_TRIP_NIGHTS)

    # Step 11 -- USD per room, per night, for the listed guests.
    currency: Literal["USD"] = "USD"
    budget_per_night: float = Field(ge=BUDGET_MINIMUM, le=BUDGET_MAXIMUM)

    @model_validator(mode="before")
    @classmethod
    def _collect_flat_severity_keys(cls, data):
        """Fold the form's `restriction_severity_{option}` keys into one dict."""
        if not isinstance(data, dict):
            return data
        flat_keys = [key for key in data if key.startswith(_SEVERITY_KEY_PREFIX)]
        if not flat_keys:
            return data
        data = dict(data)
        severity = dict(data.get("restriction_severity") or {})
        for key in flat_keys:
            severity[key[len(_SEVERITY_KEY_PREFIX):]] = data.pop(key)
        data["restriction_severity"] = severity
        return data

    @model_validator(mode="after")
    def _validate_form_rules(self, info: ValidationInfo):
        stored = bool((info.context or {}).get("stored_intake"))
        self._validate_selections()
        self._validate_party()
        self._validate_restrictions()
        self._validate_departure()
        self._validate_timing(allow_past_check_in=stored)
        return self

    def _validate_selections(self) -> None:
        for field_name in ("recent_feelings", "trip_goals", "preferred_moments", "preferred_environments"):
            if not _unique(getattr(self, field_name)):
                raise ValueError(f"{field_name} must not contain duplicates.")

        if RecentFeeling.something_else in self.recent_feelings:
            if _blank(self.recent_feelings_other):
                raise ValueError("recent_feelings_other is required when recent_feelings includes 'something_else'.")
        else:
            self.recent_feelings_other = None

        if self.trip_prompt == TripPrompt.something_else:
            if _blank(self.trip_prompt_other):
                raise ValueError("trip_prompt_other is required when trip_prompt is 'something_else'.")
        else:
            self.trip_prompt_other = None

        if PreferredEnvironment.surprise_me in self.preferred_environments and len(self.preferred_environments) > 1:
            raise ValueError("'surprise_me' cannot be combined with another preferred environment.")

    def _validate_party(self) -> None:
        defaults = PARTY_DEFAULTS[self.travel_party]
        if self.travel_party == TravelParty.solo:
            # The party fields are hidden for solo travel; the answer fixes them.
            for field_name, expected in defaults.items():
                supplied = getattr(self, field_name)
                if supplied is not None and supplied != expected:
                    raise ValueError(f"A solo trip must have {field_name}={expected}.")
        for field_name, default in defaults.items():
            if getattr(self, field_name) is None:
                setattr(self, field_name, default)

        if self.party_rooms > self.party_adults + self.party_children:
            raise ValueError("party_rooms cannot exceed the number of travelers.")
        if self.party_child_ages is not None:
            if len(self.party_child_ages) != self.party_children:
                raise ValueError("party_child_ages must list one age per child.")
            if any(age < 0 or age > 17 for age in self.party_child_ages):
                raise ValueError("party_child_ages must be between 0 and 17.")

    def _validate_restrictions(self) -> None:
        if not _unique(self.activity_restrictions):
            raise ValueError("activity_restrictions must not contain duplicates.")
        missing = [
            restriction.value
            for restriction in self.activity_restrictions
            if restriction not in self.restriction_severity
        ]
        if missing:
            raise ValueError(
                "Choose how important each selected restriction is (must_avoid or prefer_avoid): "
                + ", ".join(missing)
            )
        self.restriction_severity = {
            restriction: self.restriction_severity[restriction]
            for restriction in self.activity_restrictions
        }
        if _blank(self.restriction_notes):
            self.restriction_notes = None

    def _validate_departure(self) -> None:
        if _blank(self.departure_location):
            raise ValueError("departure_location is required.")
        self.departure_location = self.departure_location.strip()
        if (self.departure_latitude is None) != (self.departure_longitude is None):
            raise ValueError("Send departure_latitude and departure_longitude together.")

    def _validate_timing(self, allow_past_check_in: bool = False) -> None:
        if self.travel_timing == TravelTiming.exact_dates:
            if self.check_in_date is None or self.check_out_date is None:
                raise ValueError("check_in_date and check_out_date are required for exact dates.")
            if self.check_out_date <= self.check_in_date:
                raise ValueError("check_out_date must be after check_in_date.")
            if self.check_in_date < date.today() and not allow_past_check_in:
                raise ValueError("check_in_date cannot be in the past.")
            date_nights = (self.check_out_date - self.check_in_date).days
            if date_nights > MAX_TRIP_NIGHTS:
                raise ValueError(f"A stay cannot be longer than {MAX_TRIP_NIGHTS} nights.")
            if self.trip_nights is None:
                self.trip_nights = date_nights
            elif self.trip_nights != date_nights:
                raise ValueError(
                    f"trip_nights ({self.trip_nights}) does not match the selected dates ({date_nights} nights)."
                )
        else:
            self.check_in_date = None
            self.check_out_date = None

        if self.travel_timing == TravelTiming.month_season:
            if self.travel_period is None:
                raise ValueError("travel_period is required when travel_timing is 'month_season'.")
        else:
            self.travel_period = None

        if self.trip_nights is None:
            raise ValueError("trip_nights is required.")

    @property
    def budget_open_ended(self) -> bool:
        """True when the guest chose the slider's "$7,000+" top value."""
        return self.budget_per_night >= BUDGET_MAXIMUM


class DateRecommendationRequest(TravelIntakeRequest):
    """
    POST /recommend_travel_dates request body: the guest's answers to steps
    1-9 plus the step-10 timing choice (flexible, or a month/season) and the
    number of nights. The budget (step 11) is not needed yet. A date range
    and the chosen destination are optional and make the suggestion sharper.
    """

    budget_per_night: Optional[float] = Field(default=None, ge=BUDGET_MINIMUM, le=BUDGET_MAXIMUM)
    earliest_check_in: Optional[date] = None
    latest_check_out: Optional[date] = None
    destination_id: Optional[str] = Field(default=None, max_length=50)

    @model_validator(mode="after")
    def _validate_date_request(self):
        if self.travel_timing == TravelTiming.exact_dates:
            raise ValueError("The guest already chose exact dates; date suggestions are for flexible or month/season timing.")
        if self.earliest_check_in and self.earliest_check_in < date.today():
            raise ValueError("earliest_check_in cannot be in the past.")
        if self.earliest_check_in and self.latest_check_out and self.latest_check_out <= self.earliest_check_in:
            raise ValueError("latest_check_out must be after earliest_check_in.")
        return self

    @property
    def budget_open_ended(self) -> bool:
        return bool(self.budget_per_night) and self.budget_per_night >= BUDGET_MAXIMUM
