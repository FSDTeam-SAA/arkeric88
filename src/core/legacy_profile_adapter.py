"""
Bridge from the v2 15-question profile to the legacy QuestionAnswers shape
that the itinerary/activity pipeline (PromptGenerator.gen_tour_plan_prompt,
PromptGenerator.regenerate_tour_plan_prompt, _build_profile_search_context in
app/router/city_content_route.py) has always consumed.

Why this exists: POST /get_suggested_city and POST /get_tour_plan are two
steps of one session, but they were built at different times against
different questionnaires. Rather than rewrite the already-working itinerary
prompt/search-query code (see FRONTEND_DEVELOPER_CHANGES.md and
BACKEND_DEVELOPER_CHANGES.md -- neither asked for that), this module
deterministically fills the old QuestionAnswers fields from the new profile so
"Step 2 keeps working exactly as it does today," per the request that drove
this change. It is presentation-only: no matching/ranking decision anywhere
in the system depends on the phrases produced here.
"""

from app.schemas.city_body import QuestionAnswers
from app.schemas.retreat_v2_schema import RetreatRecommendationRequest
from src.core.matching_profile import MatchingProfile

_ESCAPE_FROM_PHRASES = {
    "noise_stimulation": "wanting a break from noise and overstimulation",
    "responsibility_decisions": "wanting a break from responsibility and constant decisions",
    "routine_repetition": "wanting a break from routine and repetition",
    "emotional_heaviness": "wanting to process emotional heaviness",
}

_ARRIVAL_PRIORITY_PHRASES = {
    "breathtaking_view": "arriving to a breathtaking view",
    "silence_privacy": "arriving to silence and privacy",
    "warmth_water_sunshine": "arriving to warmth, water and sunshine",
    "beautiful_design_service": "arriving to beautiful design and effortless service",
}

_RESET_STYLE_PHRASES = {
    "digital_disconnection": "a full digital disconnection",
    "sensory_indulgence": "sensory indulgence",
    "creative_inspiration": "creative inspiration",
    "doing_nothing": "doing absolutely nothing",
}

_PHYSICAL_INTENSITY_TO_ENERGY = {"gentle": "low", "moderate": "medium", "challenging": "high"}

_STRUCTURE_TO_TRAVEL_STYLE = {
    "almost_none": "fully unstructured and freeform",
    "optional_rituals": "semi-guided, with optional rituals to join or skip",
    "one_daily_anchor": "one guided anchor activity per day, the rest free",
    "full_program": "fully guided, structured program",
}

_PLANNING_TO_ORGANIZATION = {
    "loose": "loose -- figure it out as I go",
    "well_planned": "well planned, with a clear day-by-day itinerary",
    "hour_by_hour": "hour-by-hour, fully planned logistics",
}

_ARCHETYPE_TO_LIFE_SEASON = {
    "Burned-Out Achiever": "burnout recovery",
    "Transformer": "active transformation",
    "Seeker": "searching and meaning-making",
    "Optimizer": "optimization and measurable growth",
    "Escapist": "escape and simplification",
    "Reconnector": "reconnection and renewal",
}

_SETTING_TO_ENVIRONMENT_LABEL = {
    "mountains": "mountains",
    "ocean_beach": "ocean/beach",
    "jungle_rainforest": "jungle/rainforest",
    "desert": "desert",
    "countryside_farmland": "countryside/farmland",
    "lake": "lake",
    "city_urban": "city/urban",
}


def build_legacy_answers(
    request: RetreatRecommendationRequest, profile: MatchingProfile
) -> QuestionAnswers:
    """Deterministically map a v2 request/profile onto the legacy QuestionAnswers shape."""
    experience_kind = ", ".join(
        phrase
        for phrase in (
            _RESET_STYLE_PHRASES.get(request.reset_style.value),
            _ARRIVAL_PRIORITY_PHRASES.get(request.arrival_priority.value),
        )
        if phrase
    ) or "a restorative reset"

    activity_restrictions = list(
        dict.fromkeys([*profile.restrictions.codes, *profile.restrictions.unresolved_text])
    )
    preferred_environments = [
        _SETTING_TO_ENVIRONMENT_LABEL.get(setting, setting) for setting in profile.settings
    ] or ["no strong preference"]

    return QuestionAnswers(
        todays_feeling=_ESCAPE_FROM_PHRASES.get(request.escape_from.value, request.escape_from.value),
        experience_kind=experience_kind,
        energy_level=_PHYSICAL_INTENSITY_TO_ENERGY[request.physical_intensity.value],
        travel_style=_STRUCTURE_TO_TRAVEL_STYLE[request.structure_preference.value],
        trip_organization=_PLANNING_TO_ORGANIZATION[request.planning_service_level.value],
        activity_restrictions=activity_restrictions,
        life_season=_ARCHETYPE_TO_LIFE_SEASON.get(profile.primary_archetype, "renewal"),
        preferred_environments=preferred_environments,
        birthdate=None,
        # Left unset deliberately: one v2 session can suggest cities across
        # several different countries, so no single hard region constraint is
        # correct for the whole session. When a specific property is chosen
        # (property_id), its own Country is used directly instead -- see
        # get_tour_plan / _find_hotel(forced_retreat=...).
        preferred_region=None,
        budget_per_person_per_night=profile.budget_max,
        trip_length_days=profile.duration_nights_estimate,
    )
