"""
Version-controlled deterministic mappings from the 15-question v2 questionnaire's
stable enum codes to Retreat Master Database dimensions.

This is the single source of truth referenced by AI_DEVELOPER_CHANGES.md ("Keep
these mappings in version-controlled configuration and cover every frontend enum
with tests") and BACKEND_DEVELOPER_CHANGES.md's questionnaire schema. Every enum
value accepted by app/schemas/retreat_v2_schema.py must appear as a key somewhere
in this file; tests/test_retreat_matching_v2.py enforces that.

Bump ANSWER_MAPPING_VERSION whenever a mapping changes so recommendation sessions
can record which ruleset produced a given shortlist (see retreat_session_store.py).
"""

ANSWER_MAPPING_VERSION = "2026-08-15.1"


# ---------------------------------------------------------------------------
# Q1: Archetype -> canonical "Archetypes" column token
# ---------------------------------------------------------------------------
ARCHETYPE_TO_DB = {
    "burned_out_achiever": "Burned-Out Achiever",
    "transformer": "Transformer",
    "seeker": "Seeker",
    "optimizer": "Optimizer",
    "escapist": "Escapist",
    "reconnector": "Reconnector",
}

# Q2: what a secondary archetype signal each "break from" answer nudges toward.
# Never overrides the explicit Q1 selection -- see matching_profile.py.
BREAK_FROM_SECONDARY_ARCHETYPE = {
    "noise_stimulation": "Burned-Out Achiever",
    "responsibility_decisions": "Escapist",
    "routine_repetition": "Transformer",
    "emotional_heaviness": "Reconnector",
}

BREAK_FROM_EMOTIONAL_TONES = {
    "noise_stimulation": ["calm", "quiet"],
    "responsibility_decisions": ["freedom", "simple"],
    "routine_repetition": ["adventurous", "inspired"],
    "emotional_heaviness": ["warm", "gentle"],
}

# ---------------------------------------------------------------------------
# Q3: Arrival priority -> nature/luxury/privacy targets and emotional tone.
# ---------------------------------------------------------------------------
ARRIVAL_PRIORITY_EFFECTS = {
    "breathtaking_view": {"nature_target": 9, "tones": ["scenic", "awe"]},
    "silence_privacy": {"solo_target": 9, "emotional_safety_target": 9, "social_target": 2, "tones": ["quiet", "private"]},
    "warmth_water_sunshine": {"nature_target": 7, "tones": ["warm", "sunny"]},
    "beautiful_design_service": {"luxury_target": 9, "tones": ["luxurious", "elegant"]},
}

# ---------------------------------------------------------------------------
# Q4: Retreat structure -> canonical "Structure" column value.
# Q4's two middle answers ("optional_rituals", "one_daily_anchor") both map to
# the database value "Semi-Guided"; the raw answer is preserved separately on
# the profile for itinerary nuance even though the DB-facing value is shared.
# ---------------------------------------------------------------------------
STRUCTURE_TO_DB = {
    "almost_none": "Freeform",
    "optional_rituals": "Semi-Guided",
    "one_daily_anchor": "Semi-Guided",
    "full_program": "Highly Structured",
}
STRUCTURE_ORDER = ["Freeform", "Semi-Guided", "Highly Structured"]

# ---------------------------------------------------------------------------
# Q5: Reset style -> emotional tone + structure/physical intensity bias.
# ---------------------------------------------------------------------------
RESET_STYLE_EFFECTS = {
    "digital_disconnection": {"tones": ["calm", "isolated"], "digital_detox": True},
    "sensory_indulgence": {"tones": ["luxurious", "sensory"], "luxury_target": 8},
    "creative_inspiration": {"tones": ["inspired", "creative"]},
    "doing_nothing": {"tones": ["calm", "restorative"], "structure_bias": "Freeform", "intensity_bias": "Gentle"},
}

# ---------------------------------------------------------------------------
# Q6: Physical intensity -> canonical "Physical Intensity" column value.
# ---------------------------------------------------------------------------
PHYSICAL_INTENSITY_TO_DB = {
    "gentle": "Gentle",
    "moderate": "Moderate",
    "challenging": "Challenging",
}
PHYSICAL_INTENSITY_ORDER = ["Gentle", "Moderate", "Challenging"]

# ---------------------------------------------------------------------------
# Q7: Party type -> which workbook suitability column drives party-fit scoring.
# Small Group and Family both fall back to the "Social" column today because
# the workbook has no Family Fit / Small Group Fit column yet (tracked in
# BACKEND_DEVELOPER_CHANGES.md "Database Changes"); this is documented as a
# data gap in the recommendation response, not silently assumed to be correct.
# ---------------------------------------------------------------------------
PARTY_TYPE_SCORE_COLUMN = {
    "solo": "Solo",
    "couple": "Couple",
    "small_group": "Social",
    "family": "Social",
}
PARTY_TYPES_NEEDING_UNVERIFIED_FIT = {"small_group", "family"}

# ---------------------------------------------------------------------------
# Q8: Spirituality -> canonical "Spirituality" column value + ordinal for
# hard-filtering ("none" must exclude anything above "None").
# ---------------------------------------------------------------------------
SPIRITUALITY_TO_DB = {
    "none": "None",
    "light": "Light",
    "moderate": "Moderate",
    "deep": "Deep",
}
SPIRITUALITY_ORDER = ["None", "Light", "Moderate", "Deep"]

# ---------------------------------------------------------------------------
# Q9: Travel timing convenience mapping from a season word to month numbers.
# The backend/frontend contract prefers explicit month numbers because seasons
# differ by hemisphere; season names are offered as a convenience and are
# always resolved to Northern-hemisphere month numbers before filtering, which
# is surfaced to the caller as a documented assumption (see API_V2_DOCS.md).
# ---------------------------------------------------------------------------
SEASON_TO_MONTHS = {
    "spring": [3, 4, 5],
    "summer": [6, 7, 8],
    "autumn": [9, 10, 11],
    "winter": [12, 1, 2],
}

# ---------------------------------------------------------------------------
# Q10: Planning service level. Stored only for the itinerary/concierge
# workflow -- AI_DEVELOPER_CHANGES.md and BACKEND_DEVELOPER_CHANGES.md both
# require this NEVER affect property ranking, so no scoring effect is defined
# here on purpose.
# ---------------------------------------------------------------------------
PLANNING_SERVICE_LEVELS = {"loose", "well_planned", "hour_by_hour"}

# ---------------------------------------------------------------------------
# Q11: Restrictions. Deterministic keyword -> code extraction; see
# restriction_extractor.py for the extraction logic that uses this table.
# ---------------------------------------------------------------------------
RESTRICTION_KEYWORDS = {
    "no_hiking": ["hiking", "hikes", "trekking"],
    "no_water_activities": ["water activit", "swimming", "diving", "snorkel", "surf"],
    "no_long_drives": ["long drive", "long car", "road trip"],
    "no_extreme_heat": ["extreme heat", "hot climate", "very hot"],
    "no_high_impact_exercise": ["high-impact", "high impact", "intense exercise", "strenuous"],
    "no_alcohol": ["no alcohol", "alcohol-free", "sober"],
    "no_altitude": ["altitude", "high elevation"],
    "no_cold_exposure": ["cold plunge", "ice bath", "cold exposure"],
}
ACCESSIBILITY_KEYWORDS = ["wheelchair", "mobility", "accessible", "accessibility"]

# ---------------------------------------------------------------------------
# Q12: Setting. The workbook has no Setting column yet (tracked in
# BACKEND_DEVELOPER_CHANGES.md "Database Changes"), so these codes cannot be
# hard-filtered or scored against real property data today. They are still
# accepted and validated so the frontend contract is stable ahead of that
# database work, and every response surfaces this as a known data gap.
# ---------------------------------------------------------------------------
SETTING_CODES = {
    "mountains", "ocean_beach", "jungle_rainforest", "desert",
    "countryside_farmland", "lake", "city_urban",
}

# ---------------------------------------------------------------------------
# Q14: Duration bucket -> representative night count used only when the
# caller does not supply exact_nights (kept purely informational; duration is
# not a hard filter in v1 because the workbook has no reliable Minimum/Maximum
# Nights columns yet).
# ---------------------------------------------------------------------------
DURATION_BUCKET_TO_NIGHTS = {
    "1_3_nights": 2,
    "4_7_nights": 5,
    "1_2_weeks": 10,
    "2_plus_weeks": 16,
}

# ---------------------------------------------------------------------------
# Q15: Transform Focus. Frontend display label -> canonical database value,
# copied verbatim from FRONTEND_DEVELOPER_CHANGES.md so the two stay in sync.
# ---------------------------------------------------------------------------
TRANSFORM_FOCUS_VALUES = {
    "Burnout Recovery",
    "Longevity",
    "Detox",
    "Weight Loss",
    "Spiritual Growth",
    "Emotional Healing",
    "Nervous System Reset",
    "Fitness",
    "Creativity",
    "Relationship Repair",
    "Community",
    "Sleep",
    "Digital Detox",
    "Cultural Immersion",
}
