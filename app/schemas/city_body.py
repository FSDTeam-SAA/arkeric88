from pydantic import BaseModel, ConfigDict, Field
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
    # Guest-facing: a price label such as "$$ · Moderate" (None when unknown)
    # and why this stay is the base for its stop.
    price_indication: Optional[str] = None
    why_selected: str = ""
    base_area: str = ""
    nights: Optional[int] = None
    live_rate_status: str = "NOT_REQUESTED"
    provider: Optional[str] = None
    total_price: Optional[float] = None
    currency: Optional[str] = None
    room_offers: List[Dict[str, Any]] = []
    included_taxes_and_fees: List[Dict[str, Any]] = []
    payable_at_property: List[Dict[str, Any]] = []
    fee_disclosure: str = ""


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
    # "experience" | "meal" | "transfer" | "free_time"
    item_type: str = "experience"
    # Guest-facing reason this item was selected (never generic filler).
    why_selected: str = ""
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    # Travel times measured from coordinates (see src/core/travel_time.py).
    travel_minutes_from_previous: Optional[int] = None
    travel_from: Optional[str] = None
    travel_minutes_from_base: Optional[int] = None
    travel_time_source: Optional[str] = None
    return_to_base_km: Optional[float] = None
    # Meals: restaurant details; open_slot marks a meal left flexible on purpose.
    meal: Optional[str] = None
    restaurant_name: Optional[str] = None
    rating: Optional[float] = None
    price_level: Optional[str] = None
    price_indication: Optional[str] = None
    open_slot: bool = False
    # Viator product for this experience (src/core/viator_match.py): booking link,
    # rating, "from" price or the price on the travel date, and whether it runs that day.
    viator: Optional[Dict[str, Any]] = None
    # "viator_schedule" | "viator_from_price" | None (estimate from the plan)
    price_source: Optional[str] = None
    # "SCHEDULED" when Viator's schedule lists the experience on the travel date.
    availability_status: Optional[str] = None
    # Transfers between stops.
    transfer_from: Optional[str] = None
    transfer_minutes: Optional[int] = None
    transfer_buffer_minutes: Optional[int] = None
    transfer_km: Optional[float] = None
    includes_ferry: bool = False


class TourPlanDayInput(BaseModel):
    """Activities for a single day."""
    day: int
    activities: List[TourPlanActivityInput]
    # Which stop (base) this day belongs to; "transfer" days move between stops.
    stop: int = 1
    day_type: str = "standard"


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


class HotelRateRefreshInput(BaseModel):
    """Explicit live-rate request for a saved itinerary; no booking occurs."""
    guest_nationality: str
    room_occupancies: List[Dict[str, Any]] = Field(default_factory=list)
    currency: str = "USD"


class HotelPrebookInput(BaseModel):
    """Revalidate an existing itinerary offer; payment and booking stay disabled."""
    offer_id: str


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
    # Guest display: "Designed to help you feel: **{primary_feeling}**", then description.
    primary_feeling: Optional[str] = None
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
