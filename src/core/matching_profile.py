"""
Build a normalized MatchingProfile from a validated RetreatRecommendationRequest.

This is the "matching profile" required by AI_DEVELOPER_CHANGES.md #1: a
deterministic, testable translation of the 15 questionnaire answers into the
dimensions the ranking engine (retreat_scoring.py) actually scores against.
No LLM involvement here -- every field is produced by table lookups in
answer_mappings.py so the same questionnaire answers always yield the same
profile.
"""

from dataclasses import dataclass, field
from typing import List, Optional

from app.schemas.retreat_v2_schema import RetreatRecommendationRequest, PartyType
from src.core.answer_mappings import (
    ARCHETYPE_TO_DB,
    ARRIVAL_PRIORITY_EFFECTS,
    BREAK_FROM_EMOTIONAL_TONES,
    BREAK_FROM_SECONDARY_ARCHETYPE,
    DURATION_BUCKET_TO_NIGHTS,
    PARTY_TYPE_SCORE_COLUMN,
    PARTY_TYPES_NEEDING_UNVERIFIED_FIT,
    PHYSICAL_INTENSITY_TO_DB,
    RESET_STYLE_EFFECTS,
    SEASON_TO_MONTHS,
    SPIRITUALITY_TO_DB,
    STRUCTURE_TO_DB,
)
from src.core.restriction_extractor import ExtractedRestrictions, extract_restrictions


@dataclass
class MatchingProfile:
    primary_archetype: str
    secondary_archetype: Optional[str]
    desired_tones: List[str]

    structure_db_value: str
    raw_structure_answer: str

    physical_intensity_db_value: str

    party_type: str
    party_score_column: str
    party_fit_unverified: bool

    spirituality_db_value: str
    spirituality_max_ordinal: int

    travel_months: List[int]

    settings: List[str]

    budget_max: float
    budget_open_ended: bool

    duration_bucket: str
    duration_nights_estimate: int

    transform_focus: List[str]

    nature_target: float
    luxury_target: float
    solo_target: float
    emotional_safety_target: float
    social_target: float

    planning_service_level: str

    restrictions: ExtractedRestrictions

    digital_detox: bool = False
    structure_bias: Optional[str] = None
    intensity_bias: Optional[str] = None


def build_matching_profile(request: RetreatRecommendationRequest) -> MatchingProfile:
    primary_archetype = ARCHETYPE_TO_DB[request.archetype.value]
    secondary_archetype = BREAK_FROM_SECONDARY_ARCHETYPE.get(request.escape_from.value)
    if secondary_archetype == primary_archetype:
        secondary_archetype = None

    tones: List[str] = []
    tones += BREAK_FROM_EMOTIONAL_TONES.get(request.escape_from.value, [])
    arrival_effects = ARRIVAL_PRIORITY_EFFECTS.get(request.arrival_priority.value, {})
    tones += arrival_effects.get("tones", [])
    reset_effects = RESET_STYLE_EFFECTS.get(request.reset_style.value, {})
    tones += reset_effects.get("tones", [])

    nature_target = arrival_effects.get("nature_target", 5.0)
    luxury_target = max(
        arrival_effects.get("luxury_target", 5.0),
        reset_effects.get("luxury_target", 5.0),
    )
    solo_target = arrival_effects.get("solo_target", 5.0)
    emotional_safety_target = arrival_effects.get("emotional_safety_target", 7.0)
    social_target = arrival_effects.get("social_target", 5.0)

    structure_db_value = STRUCTURE_TO_DB[request.structure_preference.value]
    structure_bias = reset_effects.get("structure_bias")
    if structure_bias:
        structure_db_value = structure_bias

    physical_intensity_db_value = PHYSICAL_INTENSITY_TO_DB[request.physical_intensity.value]
    intensity_bias = reset_effects.get("intensity_bias")

    party_type = request.party.type.value
    party_score_column = PARTY_TYPE_SCORE_COLUMN[party_type]
    party_fit_unverified = party_type in PARTY_TYPES_NEEDING_UNVERIFIED_FIT

    spirituality_db_value = SPIRITUALITY_TO_DB[request.spirituality.value]
    from src.core.answer_mappings import SPIRITUALITY_ORDER
    spirituality_max_ordinal = SPIRITUALITY_ORDER.index(spirituality_db_value)

    travel_window = request.travel_window
    if travel_window.mode.value == "flexible":
        travel_months: List[int] = []
    elif travel_window.season and travel_window.season.value != "choose_month":
        travel_months = SEASON_TO_MONTHS[travel_window.season.value]
    else:
        travel_months = list(travel_window.months)

    duration_bucket = request.duration.bucket.value
    duration_nights_estimate = (
        request.duration.exact_nights or DURATION_BUCKET_TO_NIGHTS[duration_bucket]
    )

    restrictions = extract_restrictions(
        text=request.restrictions.text,
        submitted_codes=request.restrictions.codes,
    )

    return MatchingProfile(
        primary_archetype=primary_archetype,
        secondary_archetype=secondary_archetype,
        desired_tones=[tone for tone in dict.fromkeys(tones)],
        structure_db_value=structure_db_value,
        raw_structure_answer=request.structure_preference.value,
        physical_intensity_db_value=physical_intensity_db_value,
        party_type=party_type,
        party_score_column=party_score_column,
        party_fit_unverified=party_fit_unverified,
        spirituality_db_value=spirituality_db_value,
        spirituality_max_ordinal=spirituality_max_ordinal,
        travel_months=travel_months,
        settings=[setting.value for setting in request.settings],
        budget_max=request.budget.per_person_per_night_max,
        budget_open_ended=request.budget.open_ended,
        duration_bucket=duration_bucket,
        duration_nights_estimate=duration_nights_estimate,
        transform_focus=list(request.transform_focus),
        nature_target=float(nature_target),
        luxury_target=float(luxury_target),
        solo_target=float(solo_target),
        emotional_safety_target=float(emotional_safety_target),
        social_target=float(social_target),
        planning_service_level=request.planning_service_level.value,
        restrictions=restrictions,
        digital_detox=bool(reset_effects.get("digital_detox", False)),
        structure_bias=structure_bias,
        intensity_bias=intensity_bias,
    )
