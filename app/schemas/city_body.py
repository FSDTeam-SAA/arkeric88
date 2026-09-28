from pydantic import BaseModel, ConfigDict
from typing import Any, Dict, List, Optional


# ==================== CITY SUGGESTION REQUEST/RESPONSE ====================
#
# POST /get_suggested_city takes the 11-step Velari intake
# (app/schemas/intake_schema.TravelIntakeRequest) and ranks the destination
# catalog in data/1. AI_IMPORT_DESTINATIONS.csv. See API_CITY_FLOW_DOCS.md.


class RegenerateInputData(BaseModel):
    """
    Input for regenerating destination suggestions.

    Without `intake_updates`, the same answers are re-ranked and destinations
    already shown in this session are excluded. With `intake_updates` (any
    intake fields, e.g. a flexed restriction or travel distance after a
    "no valid result" response), the stored answers are updated, re-validated
    and re-ranked from scratch -- IMPORT_RULES.csv "Let guest refine the fit".
    """
    session_id: str
    user_instruction: str = ""
    intake_updates: Optional[Dict[str, Any]] = None


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
    # IMPORT_RULES.csv "Booking search and availability": search prices are
    # estimates; availability is never claimed without a live check.
    price_status: str = "ESTIMATED"
    availability_status: str = "NOT_CHECKED"


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
    # IMPORT_RULES.csv "Places": a place ID + business status identify a real
    # business; they do not prove ticket or activity availability.
    place_id: Optional[str] = None
    business_status: Optional[str] = None
    availability_note: str = ""


class TourPlanDayInput(BaseModel):
    """Activities for a single day."""
    day: int
    activities: List[TourPlanActivityInput]


class TourPlanRequestData(BaseModel):
    """
    Request payload for generating a day-wise tour plan.

    `destination_id` (from POST /get_suggested_city) is preferred. Without it,
    `selected_city` must name one of the destinations suggested in this
    session -- a place that was never matched (or that a must-avoid
    restriction excluded) cannot be planned.
    """
    session_id: str
    selected_city: str = ""
    destination_id: Optional[str] = None


class CitySuggestionInput(BaseModel):
    """
    One destination suggestion. Every suggestion is a CANDIDATE_ONLY catalog
    entry: `verification` and `unresolved_facts` say what has and has not
    been checked. Select it by `destination_id` when calling POST /get_tour_plan.
    """
    model_config = ConfigDict(extra="ignore")

    city_name: str
    country_name: str
    number_of_days: int
    description: Optional[str] = None
    city_image: List[str] = []
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    destination_id: Optional[str] = None
    world_region: Optional[str] = None
    match_score: Optional[int] = None
    score_breakdown: Dict[str, float] = {}
    match_reasons: List[str] = []
    tradeoffs: List[str] = []
    unresolved_facts: List[str] = []
    warnings: List[str] = []
    restriction_checks: List[dict] = []
    distance_check: Dict[str, Any] = {}
    verification: Dict[str, Any] = {}
    evidence: Dict[str, Any] = {}


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
    response: dict


class TourPlanResponse(BaseModel):
    """Response after generating tour plan."""
    activity_session_id: str
    city: str
    # "The feeling behind your journey" -- see src/core/feeling_block.py.
    feeling_block: Optional[dict] = None
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
    intake: dict
    suggested_cities: List[CitySuggestionInput]
    regeneration_history: List[dict]


class ActivitySessionDetailsResponse(BaseModel):
    """Response when fetching full activity session details."""
    activity_session_id: str
    parent_session_id: str
    city: str
    tour_plan: List[TourPlanDayInput]
    regeneration_history: List[dict]
