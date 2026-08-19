"""
Versioned schema for the 15-question retreat-matching questionnaire and the
POST /v2/retreat-recommendations contract described in
BACKEND_DEVELOPER_CHANGES.md and FRONTEND_DEVELOPER_CHANGES.md.

This intentionally does NOT reuse app/schemas/city_body.QuestionAnswers -- that
model backs the legacy /get_suggested_city flow (birthdate, trip_length_days as
lodging nights, etc.) which stays untouched so existing clients are unaffected.
"""

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field, model_validator

from src.core.answer_mappings import (
    PLANNING_SERVICE_LEVELS,
    SETTING_CODES,
    TRANSFORM_FOCUS_VALUES,
)

QUESTIONNAIRE_SCHEMA_VERSION = "2026-08-15.v2"


# ==================== ENUMS (stable codes only, never display labels) ====================

class Archetype(str, Enum):
    burned_out_achiever = "burned_out_achiever"
    transformer = "transformer"
    seeker = "seeker"
    optimizer = "optimizer"
    escapist = "escapist"
    reconnector = "reconnector"


class BreakFrom(str, Enum):
    noise_stimulation = "noise_stimulation"
    responsibility_decisions = "responsibility_decisions"
    routine_repetition = "routine_repetition"
    emotional_heaviness = "emotional_heaviness"


class ArrivalPriority(str, Enum):
    breathtaking_view = "breathtaking_view"
    silence_privacy = "silence_privacy"
    warmth_water_sunshine = "warmth_water_sunshine"
    beautiful_design_service = "beautiful_design_service"


class StructurePreference(str, Enum):
    almost_none = "almost_none"
    optional_rituals = "optional_rituals"
    one_daily_anchor = "one_daily_anchor"
    full_program = "full_program"


class ResetStyle(str, Enum):
    digital_disconnection = "digital_disconnection"
    sensory_indulgence = "sensory_indulgence"
    creative_inspiration = "creative_inspiration"
    doing_nothing = "doing_nothing"


class PhysicalIntensity(str, Enum):
    gentle = "gentle"
    moderate = "moderate"
    challenging = "challenging"


class PartyType(str, Enum):
    solo = "solo"
    couple = "couple"
    small_group = "small_group"
    family = "family"


class Spirituality(str, Enum):
    none = "none"
    light = "light"
    moderate = "moderate"
    deep = "deep"


class TravelTimingMode(str, Enum):
    flexible = "flexible"
    specific = "specific"


class Season(str, Enum):
    spring = "spring"
    summer = "summer"
    autumn = "autumn"
    winter = "winter"
    choose_month = "choose_month"


class PlanningServiceLevel(str, Enum):
    loose = "loose"
    well_planned = "well_planned"
    hour_by_hour = "hour_by_hour"


class Setting(str, Enum):
    mountains = "mountains"
    ocean_beach = "ocean_beach"
    jungle_rainforest = "jungle_rainforest"
    desert = "desert"
    countryside_farmland = "countryside_farmland"
    lake = "lake"
    city_urban = "city_urban"


class DurationBucket(str, Enum):
    nights_1_3 = "1_3_nights"
    nights_4_7 = "4_7_nights"
    weeks_1_2 = "1_2_weeks"
    weeks_2_plus = "2_plus_weeks"


assert PLANNING_SERVICE_LEVELS == {level.value for level in PlanningServiceLevel}
assert SETTING_CODES == {setting.value for setting in Setting}


# ==================== SUB-MODELS ====================

class Party(BaseModel):
    type: PartyType
    adults: int = Field(ge=1, le=20)
    children: int = Field(default=0, ge=0, le=10)

    @model_validator(mode="after")
    def validate_party_counts(self):
        if self.type == PartyType.solo and (self.adults != 1 or self.children != 0):
            raise ValueError("Solo party must have exactly 1 adult and 0 children.")
        if self.type == PartyType.couple and (self.adults != 2 or self.children != 0):
            raise ValueError("Couple party must have exactly 2 adults and 0 children.")
        if self.type == PartyType.family and self.adults < 1:
            raise ValueError("Family party must have at least 1 adult.")
        if self.type == PartyType.small_group and self.adults < 3:
            raise ValueError("Small group party must have at least 3 adults.")
        return self


class TravelWindow(BaseModel):
    mode: TravelTimingMode
    season: Optional[Season] = None
    months: List[int] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_window(self):
        if self.mode == TravelTimingMode.flexible:
            if self.season is not None or self.months:
                raise ValueError("Flexible travel timing must not include season or months.")
            return self
        # mode == specific
        if self.season is None:
            raise ValueError("Specific travel timing requires a season (use 'choose_month' for exact months).")
        if self.season == Season.choose_month:
            if not self.months:
                raise ValueError("season='choose_month' requires at least one month number (1-12).")
        elif self.months:
            raise ValueError("A named season must not also include explicit months; use season='choose_month' instead.")
        for month in self.months:
            if not 1 <= month <= 12:
                raise ValueError("Months must be between 1 and 12.")
        return self


class Restrictions(BaseModel):
    text: str = Field(default="", max_length=1000)
    codes: List[str] = Field(default_factory=list)


class Budget(BaseModel):
    currency: str = Field(default="USD", max_length=3)
    per_person_per_night_max: float = Field(gt=0)
    open_ended: bool = False


class Duration(BaseModel):
    bucket: DurationBucket
    exact_nights: Optional[int] = Field(default=None, gt=0)


# ==================== TOP-LEVEL REQUEST ====================

class RetreatRecommendationRequest(BaseModel):
    """POST /v2/retreat-recommendations request body -- the 15-question payload."""

    schema_version: str = QUESTIONNAIRE_SCHEMA_VERSION

    archetype: Archetype
    escape_from: BreakFrom
    arrival_priority: ArrivalPriority
    structure_preference: StructurePreference
    reset_style: ResetStyle
    physical_intensity: PhysicalIntensity
    party: Party
    spirituality: Spirituality
    travel_window: TravelWindow
    planning_service_level: PlanningServiceLevel
    restrictions: Restrictions = Field(default_factory=Restrictions)
    settings: List[Setting] = Field(default_factory=list)
    budget: Budget
    duration: Duration
    transform_focus: List[str] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def validate_transform_focus(self):
        unknown = [value for value in self.transform_focus if value not in TRANSFORM_FOCUS_VALUES]
        if unknown:
            raise ValueError(
                f"Unknown transform_focus value(s): {unknown}. "
                f"Allowed values: {sorted(TRANSFORM_FOCUS_VALUES)}"
            )
        if len(set(self.transform_focus)) != len(self.transform_focus):
            raise ValueError("transform_focus values must be unique.")
        return self


# ==================== RESPONSE ====================

class ScoreBreakdown(BaseModel):
    archetype: float
    transform_focus: float
    emotional_tone: float
    structure: float
    physical_intensity: float
    party_social: float
    emotional_safety: float
    nature: float
    luxury: float
    spirituality: float


class ExtractedRestrictions(BaseModel):
    codes: List[str] = Field(default_factory=list)
    accessibility_needs: List[str] = Field(default_factory=list)
    unresolved_text: List[str] = Field(default_factory=list)


class RankedProperty(BaseModel):
    property_id: str
    property_name: str
    country: str
    region: str
    settings: List[str] = Field(default_factory=list)
    match_score: int
    score_breakdown: ScoreBreakdown
    match_reasons: List[str]
    warnings: List[str]
    restriction_status: str
    avg_night: Optional[float]
    avg_night_is_lower_bound: bool
    avg_night_raw: str
    budget_tier: str
    program_cost: str
    best_season: List[int]
    best_season_raw: str


class RetreatRecommendationResponse(BaseModel):
    recommendation_session_id: str
    schema_version: str
    scoring_version: str
    answer_mapping_version: str
    database_version: str
    recommendations: List[RankedProperty]
    excluded_count: int
    total_candidate_count: int
    extracted_restrictions: ExtractedRestrictions
    data_gaps: List[str]
