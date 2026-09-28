"""
Version-controlled deterministic mappings from the Velari emotional travel
intake (app/schemas/intake_schema.py) to the destination catalog
(data/1. AI_IMPORT_DESTINATIONS.csv).

This is the single source of truth for how each answer affects matching.
data/4. INTAKE_MAPPING.csv defines the role of each intake key and
data/3. IMPORT_RULES.csv defines the required behavior; the comments below
cite the rule each table implements. tests/test_intake_mappings.py checks
that every intake enum value and every INTAKE_MAPPING.csv key is covered here.

Bump INTAKE_MAPPING_VERSION whenever a mapping or threshold changes so each
session records which ruleset produced its suggestions.
"""

INTAKE_MAPPING_VERSION = "2026-09-28.1"
SCORING_VERSION = "velari-destinations-2026-09-28.1"

# ---------------------------------------------------------------------------
# Guest-facing labels, copied verbatim from the intake form so explanations
# can "reflect guest wording" (INTAKE_MAPPING.csv: current_feeling).
# ---------------------------------------------------------------------------
RECENT_FEELING_LABELS = {
    "stretched_thin": "Stretched thin",
    "stuck_in_routine": "Stuck in a routine",
    "disconnected": "Disconnected",
    "curious": "Curious",
    "energized": "Energized",
    "turning_point": "At a turning point",
    "content_ready": "Content and ready to enjoy life",
    "something_else": "Something else",
}

TRIP_GOAL_LABELS = {
    "restoration": "Restoration",
    "connection": "Connection",
    "discovery": "Discovery",
    "adventure": "Adventure",
    "inspiration": "Inspiration",
    "celebration": "Celebration",
    "reflection": "Reflection",
    "growth": "Growth",
}

# INTAKE_MAPPING.csv desired_outcomes: "Show concise theme definitions".
TRIP_GOAL_DEFINITIONS = {
    "restoration": "Space to slow down and feel less pulled in every direction",
    "connection": "Meaningful time with people who matter to you",
    "discovery": "New places, flavors, stories and perspectives",
    "adventure": "Movement, challenge and doing something new",
    "inspiration": "Fresh ideas through art, learning or creativity",
    "celebration": "Joy, pleasure and a moment worth remembering",
    "reflection": "Quiet and perspective to think about what matters",
    "growth": "An experience that helps you stretch or make a change",
}

# Headline word for the itinerary's "The feeling behind your journey" block
# ("THE FEELING: REFLECTIVE"). Reflective, Connected, Inspired and Curious
# come from the client's sample descriptors; the other four are editorial
# choices and can be changed here without touching code.
TRIP_GOAL_FEELING_WORDS = {
    "restoration": "Restored",
    "connection": "Connected",
    "discovery": "Curious",
    "adventure": "Adventurous",
    "inspiration": "Inspired",
    "celebration": "Joyful",
    "reflection": "Reflective",
    "growth": "Growing",
}

TRIP_PROMPT_LABELS = {
    "need_a_break": "I need a break",
    "time_with_someone": "Time with someone",
    "celebrating": "I’m celebrating",
    "curious_to_explore": "I’m curious to explore",
    "ready_for_change": "I’m ready for change",
    "change_of_scenery": "A change of scenery",
    "no_particular_reason": "No particular reason",
    "something_else": "Something else",
}

PREFERRED_MOMENT_LABELS = {
    "food_drinks": "Memorable food and drinks",
    "art_history_culture": "Art, history and culture",
    "nature_wildlife": "Nature and wildlife",
    "beaches_water": "Beaches and water",
    "movement_adventure": "Movement and adventure",
    "quiet_privacy": "Quiet and privacy",
    "meeting_people": "Meeting people",
    "spa_wellness": "Spa and wellness",
    "music_nightlife": "Music and nightlife",
    "learning_making": "Learning or making something",
}

ENVIRONMENT_LABELS = {
    "coast": "Coast",
    "mountains": "Mountains",
    "forest_jungle": "Forest or jungle",
    "desert": "Desert",
    "countryside": "Countryside",
    "small_town": "Small town",
    "vibrant_city": "Vibrant city",
    "surprise_me": "Surprise me",
}

TRIP_PACE_LABELS = {
    "mostly_open": "Mostly open time",
    "one_highlight": "One highlight each day, with plenty of free time",
    "balanced": "A balance of activities and downtime",
    "full_days": "Full days with plenty to do",
}

TRAVEL_PARTY_LABELS = {
    "solo": "Just me",
    "couple": "My partner and me",
    "group": "Friends or a group",
    "family": "Family",
}

RESTRICTION_LABELS = {
    "mobility_accessibility": "Mobility or accessibility",
    "food_dietary": "Food allergy or dietary need",
    "no_long_drives": "No long drives",
    "no_intense_activity": "No intense activity",
    "no_water_activities": "No water activities",
    "avoid_extreme_heat": "Avoid extreme heat",
    "avoid_cold_weather": "Avoid cold weather",
    "other": "Other",
}

RESTRICTION_SEVERITY_LABELS = {
    "must_avoid": "I must avoid this",
    "prefer_avoid": "I’d prefer to avoid this",
}

TRAVEL_DISTANCE_LABELS = {
    "nearby": "Nearby",
    "manageable_flight": "A manageable flight",
    "anywhere": "Open to anywhere",
}

TRAVEL_TIMING_LABELS = {
    "exact_dates": "I have exact dates",
    "flexible": "My dates are flexible",
    "month_season": "I know the month or season",
}

TRAVEL_PERIOD_LABELS = {
    "spring": "Spring", "summer": "Summer", "autumn": "Autumn", "winter": "Winter",
    "january": "January", "february": "February", "march": "March",
    "april": "April", "may": "May", "june": "June", "july": "July",
    "august": "August", "september": "September", "october": "October",
    "november": "November", "december": "December",
}

# The guest's hemisphere is unknown, so season names are read as
# Northern-hemisphere months. Only the heat/cold seasonal checks use these
# months, and the response records the assumption in data_gaps.
TRAVEL_PERIOD_TO_MONTHS = {
    "spring": [3, 4, 5], "summer": [6, 7, 8], "autumn": [9, 10, 11], "winter": [12, 1, 2],
    "january": [1], "february": [2], "march": [3], "april": [4], "may": [5],
    "june": [6], "july": [7], "august": [8], "september": [9], "october": [10],
    "november": [11], "december": [12],
}
SEASON_PERIODS = {"spring", "summer", "autumn", "winter"}

# ---------------------------------------------------------------------------
# INTAKE_MAPPING.csv intake_key -> the intake form field(s) that carry it.
# tests/test_intake_mappings.py fails if the CSV gains a key not listed here.
# ---------------------------------------------------------------------------
INTAKE_KEY_TO_FORM_FIELDS = {
    "current_feeling": ["recent_feelings", "recent_feelings_other"],
    "desired_outcomes": ["trip_goals"],
    "trip_trigger": ["trip_prompt", "trip_prompt_other"],
    "experience_interests": ["preferred_moments"],
    "settings": ["preferred_environments"],
    "pace": ["trip_pace"],
    "party": ["travel_party", "party_adults", "party_children", "party_child_ages", "party_rooms"],
    "constraints": ["activity_restrictions", "restriction_severity", "restriction_notes"],
    "origin_radius": ["departure_location", "travel_distance"],
    "dates_duration": ["travel_timing", "check_in_date", "check_out_date", "travel_period", "trip_nights"],
    "nightly_budget": ["budget_per_night"],
}

# Free-text fields that may hold private emotional content. IMPORT_RULES.csv
# "Freshness and privacy": never sent to travel APIs, the LLM (which can call
# travel tools), or logs.
PRIVATE_FREE_TEXT_FIELDS = ("recent_feelings_other", "trip_prompt_other")

# ---------------------------------------------------------------------------
# Catalog theme vocabulary -> trip_goals. The 28 editorial rows use traveler
# names (Restorer, Explorer...) and the rest use outcome names (Discovery...).
# IMPORT_RULES.csv "Emotion interpretation": themes are editorial hypotheses
# and can blend -- a match is never a promise of an emotional result.
# ---------------------------------------------------------------------------
CATALOG_THEME_TO_GOAL = {
    "restorer": "restoration",
    "connector": "connection",
    "explorer": "discovery",
    "adventurer": "adventure",
    "reflector": "reflection",
    "restoration": "restoration",
    "connection": "connection",
    "discovery": "discovery",
    "adventure": "adventure",
    "inspiration": "inspiration",
    "celebration": "celebration",
    "reflection": "reflection",
    "growth": "growth",
}

# ---------------------------------------------------------------------------
# preferred_moments -> regex fragments matched (whole word) against the
# destination's experience_context, setting_context and editorial_rationale.
# source_scope is never searched ("Never turn source_scope into a fact").
# ---------------------------------------------------------------------------
MOMENT_KEYWORDS = {
    "food_drinks": [r"food", r"culinary", r"dining", r"wine", r"coffee", r"markets?", r"flavou?rs?"],
    "art_history_culture": [
        r"arts?", r"history", r"historic(?:al)?", r"cultur(?:e|al)", r"museums?",
        r"architecture", r"heritage", r"archaeological", r"traditions?", r"traditional",
        r"craft", r"craftsmanship", r"m[aā]ori culture",
    ],
    "nature_wildlife": [
        r"nature", r"wildlife", r"forests?", r"rainforest", r"landscapes?", r"geothermal",
        r"red rocks", r"scenery", r"reef", r"dry forest",
    ],
    "beaches_water": [
        r"beach(?:es)?", r"coast(?:al)?", r"ocean", r"sea", r"reef", r"islands?",
        r"water", r"waterfront", r"harbou?r", r"lake", r"river", r"hot springs",
        r"hot water", r"onsen", r"atoll",
    ],
    "movement_adventure": [
        r"hiking", r"adventure", r"movement", r"active", r"outdoor", r"trails?",
        r"challenge", r"reef experiences",
    ],
    "quiet_privacy": [
        r"quiet", r"slow(?:er)?", r"reflective", r"silence", r"gentle", r"private",
        r"rural", r"stargazing", r"relaxed",
    ],
    "meeting_people": [
        r"community", r"local cultural engagement", r"living culture", r"neighbou?rhoods?",
        r"markets?", r"vibrant", r"urban culture",
    ],
    "spa_wellness": [
        r"spa", r"wellness", r"yoga", r"hot springs", r"onsen", r"thermal", r"geothermal",
        r"hot water", r"recovery",
    ],
    "music_nightlife": [r"nightlife", r"music", r"vibrant", r"urban", r"large city"],
    "learning_making": [
        r"learning", r"craft", r"craftsmanship", r"creative", r"creativity",
        r"cultural learning", r"art",
    ],
}

# Structured catalog fields that also evidence a moment, beyond keywords.
MOMENT_PACE_SIGNALS = {"quiet_privacy": {"Gentle"}, "movement_adventure": {"Active"}}
MOMENT_GOAL_SIGNALS = {"movement_adventure": {"adventure"}}

# ---------------------------------------------------------------------------
# preferred_environments -> regex fragments matched against setting_context,
# experience_context and editorial_rationale. INTAKE_MAPPING.csv settings:
# "Preference unless explicitly mandatory" -- scored, never hard-filtered.
# ---------------------------------------------------------------------------
ENVIRONMENT_KEYWORDS = {
    "coast": [
        r"coast(?:al)?", r"beach(?:es)?", r"ocean", r"sea", r"islands?", r"harbou?r",
        r"waterfront", r"atoll", r"port", r"peninsula",
    ],
    "mountains": [
        r"mountains?", r"alpine", r"andean", r"highlands?", r"foothills", r"atlas",
        r"hinterland",
    ],
    "forest_jungle": [r"forests?", r"jungle", r"rainforest"],
    "desert": [r"desert", r"red rocks", r"wadi"],
    "countryside": [
        r"rural", r"valleys?", r"vineyards?", r"countryside", r"wine region",
        r"hinterland", r"coffee", r"landscape",
    ],
    "small_town": [r"small (?:city|town)", r"towns?", r"village", r"walkable", r"compact"],
    "vibrant_city": [
        r"(?<!small )cit(?:y|ies)", r"capital", r"urban", r"vibrant", r"metropolis",
    ],
}

# ---------------------------------------------------------------------------
# trip_pace -> catalog pace_context. INTAKE_MAPPING.csv pace: itinerary
# density, "Separate from fitness/accessibility" -- so pace never hard-filters.
# ---------------------------------------------------------------------------
TRIP_PACE_TO_CATALOG_PACE = {
    "mostly_open": "Gentle",
    "one_highlight": "Gentle",
    "balanced": "Balanced",
    "full_days": "Active",
}
CATALOG_PACE_ORDER = ["Gentle", "Balanced", "Active"]

# Itinerary density guidance handed to the tour-plan prompt.
TRIP_PACE_ITINERARY_GUIDANCE = {
    "mostly_open": "Plan at most one light, optional activity per day; leave most of each day unscheduled.",
    "one_highlight": "Plan exactly one highlight per day; leave the rest of the day free.",
    "balanced": "Plan two activities per day with real downtime between them.",
    "full_days": "Plan three to four activities per day.",
}

# ---------------------------------------------------------------------------
# Restrictions. IMPORT_RULES.csv "Hard exclusions": must-avoid filters before
# ranking, prefer-avoid is a penalty or an explained tradeoff, and "Unknown
# high-impact suitability is not a pass".
#
# Each entry lists regex fragments and the catalog fields they are matched
# against. A match means the catalog itself flags the concern.
# "seasonal" marks the fragments that describe a seasonal risk, which can be
# cleared when the guest's travel months fall outside the destination's local
# risk season (see RESTRICTION_RISK_SEASON).
# ---------------------------------------------------------------------------
RESTRICTION_CONFLICT_RULES = {
    "mobility_accessibility": {
        "fields": ["tradeoffs_to_check"],
        "patterns": [r"mobility", r"accessibility", r"cobblestones", r"steep", r"walking", r"medina"],
        "seasonal": [],
    },
    "no_long_drives": {
        "fields": ["tradeoffs_to_check"],
        "patterns": [r"driving", r"drives?", r"transfers?", r"seaplane", r"side trips require"],
        "seasonal": [],
    },
    "no_intense_activity": {
        "fields": ["tradeoffs_to_check", "pace_context"],
        "patterns": [r"active", r"walking", r"altitude", r"activity demands", r"adventure activities"],
        "seasonal": [],
    },
    "no_water_activities": {
        "fields": ["experience_context", "editorial_rationale", "tradeoffs_to_check"],
        "patterns": [r"reef", r"ocean", r"sea conditions", r"water setting", r"snorkel\w*", r"div(?:e|ing)", r"surf\w*"],
        "seasonal": [],
    },
    "avoid_extreme_heat": {
        "fields": ["tradeoffs_to_check", "setting_context"],
        "patterns": [r"heat", r"humidity", r"desert"],
        "seasonal": [r"summer"],
    },
    "avoid_cold_weather": {
        "fields": ["tradeoffs_to_check", "setting_context"],
        "patterns": [r"cold", r"alpine", r"seasonal weather", r"weather and driving safety", r"volcanic coast"],
        "seasonal": [r"seasonal", r"alpine"],
    },
}

# Local risk season (Northern-hemisphere months; flipped for the Southern
# hemisphere) used to clear a seasonal flag when the guest's months are known.
RESTRICTION_RISK_SEASON = {
    "avoid_extreme_heat": [6, 7, 8],
    "avoid_cold_weather": [12, 1, 2],
}

# Restrictions whose suitability depends on the destination itself and is
# high-impact: with "must avoid", an unknown (no catalog evidence either way)
# is NOT a pass and the destination is excluded.
HIGH_IMPACT_DESTINATION_RESTRICTIONS = {"mobility_accessibility"}

# Restrictions that cannot be judged at destination level at all; they are
# carried forward as hard constraints for the stay/restaurant/activity stage
# and listed as unresolved facts on every suggestion.
ITINERARY_LEVEL_RESTRICTIONS = {"food_dietary"}

# ---------------------------------------------------------------------------
# Travel distance. IMPORT_RULES.csv "Route": straight-line distance can never
# establish that a route is feasible. It is, however, a lower bound on any
# real route, so it is used only to EXCLUDE destinations that are certainly
# beyond the guest's tolerance, never to claim a route works.
# ---------------------------------------------------------------------------
TRAVEL_DISTANCE_MAX_STRAIGHT_LINE_KM = {
    "nearby": 1500,
    "manageable_flight": 9000,
    "anywhere": None,
}

# ---------------------------------------------------------------------------
# Scoring weights (sum to 100). Desired outcomes are the primary signal per
# IMPORT_RULES.csv "Emotion interpretation"; concrete interests, setting,
# pace and travel tolerance refine it.
# ---------------------------------------------------------------------------
SCORE_WEIGHTS = {
    "desired_outcomes": 40,
    "experience_interests": 25,
    "settings": 15,
    "pace": 10,
    "travel_distance": 10,
}
PREFER_AVOID_PENALTY = 8

# IMPORT_RULES.csv "Guest explanation": return 2-3 diverse options.
SUGGESTION_COUNT = 3
