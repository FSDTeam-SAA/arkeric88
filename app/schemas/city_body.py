from datetime import date
from pydantic import BaseModel, model_validator
from typing import List, Optional


# ==================== QUESTION & ANSWER SCHEMA ====================

class QuestionAnswers(BaseModel):
    """User's answers to tour preference questions."""
    # How are you feeling today?
    todays_feeling: str

    # What kind of experience is calling to you?
    experience_kind: str

    # What's your energy like for this trip?
    energy_level: str

    # How do you want to experience this journey?
    travel_style: str

    # How would you like your trip to be organized?
    trip_organization: str

    # Are there any activities you'd like to avoid or can't do?
    activity_restrictions: list[str]

    # Which word best describes the season you're in right now?
    life_season: str

    # What environment speaks to your soul?
    preferred_environments: list[str]

    # Your birthdate. Optional because the v2 15-question flow (see
    # src/core/legacy_profile_adapter.py) never collects age; it is still
    # accepted for any client still sending the original 12-question payload.
    birthdate: Optional[date] = None

    # Preferred geography from question 12 (for example: Europe or Asia).
    preferred_region: Optional[str] = None

    # Question 10 is a per-person, per-night budget. `total_trip_budget` is
    # retained as a backwards-compatible alternative for existing clients.
    budget_per_person_per_night: Optional[float] = None
    total_trip_budget: Optional[float] = None

    # How many days are you planning to travel?
    trip_length_days: int

    @model_validator(mode="after")
    def validate_budget(self):
        if self.budget_per_person_per_night is None and self.total_trip_budget is None:
            raise ValueError(
                "Provide budget_per_person_per_night or the legacy total_trip_budget."
            )
        if self.budget_per_person_per_night is not None and self.budget_per_person_per_night <= 0:
            raise ValueError("budget_per_person_per_night must be greater than zero.")
        if self.total_trip_budget is not None and self.total_trip_budget <= 0:
            raise ValueError("total_trip_budget must be greater than zero.")
        if self.trip_length_days <= 0:
            raise ValueError("trip_length_days must be greater than zero.")
        return self

    @property
    def effective_total_budget(self) -> float:
        """Budget for one traveler across the stay, including legacy inputs."""
        if self.budget_per_person_per_night is not None:
            return self.budget_per_person_per_night * self.trip_length_days
        return float(self.total_trip_budget or 0)


# ==================== CITY SUGGESTION REQUEST/RESPONSE ====================
#
# POST /get_suggested_city now takes the same 15-question payload as
# POST /v2/retreat-recommendations (see app/schemas/retreat_v2_schema.py,
# RetreatRecommendationRequest) instead of the old QuestionAnswers-based
# InputData wrapper. The old wrapper has been removed since the endpoint no
# longer asks a model to invent cities from a free-form profile -- see
# API_CITY_FLOW_DOCS.md for the full contract.


class RegenerateInputData(BaseModel):
    """Input for regenerating city suggestions."""
    session_id: str
    user_instruction: str  # User's preference for regeneration (e.g., "more adventure", "budget options")


# ==================== STAY / HOTEL SCHEMA ====================

class StayInfo(BaseModel):
    """Hotel/resort where the user stays during the trip."""
    name: str
    address: str
    rating: float
    price_level: str
    photos: List[str] = []
    coords: Optional[dict] = None
    average_nightly_price: str = ""
    budget_tier: str = ""
    facilities: List[str] = []
    website: str = ""
    estimate_note: str = ""


# ==================== ACTIVITY/TOUR PLAN REQUEST/RESPONSE ====================

class TourPlanActivityInput(BaseModel):
    """Single activity in a day."""
    activity_name: str
    activity_description: str
    activity_location: str
    activity_address: str = "N/A"
    activity_image: List[str] = []
    activity_time: str  # e.g., "9:00 AM - 12:00 PM"
    activity_cost: float = 0.0
    distance_from_previous_km: Optional[float] = None


class TourPlanDayInput(BaseModel):
    """Activities for a single day."""
    day: int
    activities: List[TourPlanActivityInput]


class TourPlanRequestData(BaseModel):
    """
    Request payload for generating a day-wise tour plan for a chosen city.

    `property_id` (from POST /v2/retreat-recommendations) is now preferred over
    `selected_city` per BACKEND_DEVELOPER_CHANGES.md ("Itinerary generation must
    accept property_id, not only a city name."). `selected_city` is kept
    required for backward compatibility with the legacy /get_suggested_city
    flow; when `property_id` is supplied it takes precedence and the resolved
    retreat's location is used instead of `selected_city`.
    """
    session_id: str
    selected_city: str
    property_id: Optional[str] = None


class TourPlanInput(BaseModel):
    """Complete tour plan (all days)."""
    suggested_city: "CitySuggestionInput"
    tour_plan: List[TourPlanDayInput]


class CitySuggestionInput(BaseModel):
    """
    Suggested city details. `property_id` and `match_score` are populated by
    the deterministic property-matching engine (see
    src/core/retreat_matching_orchestrator.py): each suggested city is the
    real, bookable property that ranked highest in that city/region, not a
    model-invented place. Select a city by `property_id` when calling
    POST /get_tour_plan -- see API_CITY_FLOW_DOCS.md.
    """
    city_name: str
    country_name: str
    number_of_days: int
    description: Optional[str] = None
    city_image: List[str] = []
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    property_id: Optional[str] = None
    match_score: Optional[int] = None
    warnings: List[str] = []


class RegenerateActivityInputData(BaseModel):
    """Input for regenerating activities (tour plan)."""
    activity_session_id: str
    day_to_regenerate: Optional[int] = None  # If None, regenerate entire plan. If int, regenerate only that day.
    user_instruction: str  # e.g., "more budget-friendly activities", "more adventurous Day 2"


# ==================== RESPONSE SCHEMAS ====================

class CitySuggestionResponse(BaseModel):
    """Response after generating city suggestions."""
    session_id: str
    suggested_cities: List[CitySuggestionInput]
    response: dict  # Full AI response (for reference)


class TourPlanResponse(BaseModel):
    """Response after generating tour plan."""
    activity_session_id: str
    city: str
    stay: StayInfo
    tour_plan: List[TourPlanDayInput]
    total_cost_estimate: float = 0.0
    packing_tips: str = ""
    travel_tips: str = ""
    response: dict  # Full AI response (for reference)
    source: str  # "generated" or "cached"


class SessionDetailsResponse(BaseModel):
    """Response when fetching full session details."""
    session_id: str
    questions_answers: dict
    suggested_cities: List[CitySuggestionInput]
    regeneration_history: List[dict]


class ActivitySessionDetailsResponse(BaseModel):
    """Response when fetching full activity session details."""
    activity_session_id: str
    parent_session_id: str
    city: str
    tour_plan: List[TourPlanDayInput]
    regeneration_history: List[dict]