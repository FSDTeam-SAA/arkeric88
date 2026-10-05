from typing import List, Optional

from app.schemas.city_body import TourPlanDayInput
from src.core.destination_catalog import Destination
from src.core.intake_mappings import (
    TRIP_GOAL_DEFINITIONS,
    TRIP_GOAL_FEELING_WORDS,
    TRIP_PACE_ITINERARY_GUIDANCE,
    TRIP_PACE_LABELS,
    TRIP_PROMPT_LABELS,
    RECENT_FEELING_LABELS,
)
from src.core.itinerary_geo import MIN_NIGHTS_FOR_SECOND_STOP, max_stops_for
from src.core.travel_time import MAX_LEG_MINUTES
from src.core.trip_profile import TripProfile

_RESPONSE_FIELD_RULES = """CRITICAL - Do NOT invent or include these fields. They will be filled by a real data tool later:
   - Do NOT include an exact street address (no "activity_address" field)
   - Do NOT include any image URLs (no "activity_image" field)
   - Do NOT include any distance values (no "distance_from_previous_km" field)
   - Do NOT state that anything is available, open, bookable or sold at a given price.
   Only the fields listed in the RESPONSE FORMAT below should be included."""

_WHY_SELECTED_RULE = (
    '"why_selected" is ONE guest-facing sentence (at least 8 words) on why this activity suits THIS traveler: '
    "tie it to their chosen feeling, the moments they enjoy, their pace or their party, and to something "
    'specific about the place. Never write filler such as "a convenient stop", "a must-see" or '
    '"near the route", and never mention tags, catalogs or internal labels.'
)


class PromptGenerator:
    """
    Itinerary prompts for Step 2 (POST /get_tour_plan).

    Destination choice is not prompt-led: POST /get_suggested_city ranks the
    destination catalog deterministically (src/core/destination_matching.py).
    These prompts only receive the TripProfile (option codes and labels) and
    the chosen catalog entry. The guest's private free text is never included,
    because the model can call travel tools (IMPORT_RULES.csv "Freshness and
    privacy").
    """

    # ==================== TOUR PLAN / ACTIVITY PROMPTS ====================
    @staticmethod
    def gen_tour_plan_prompt(
        profile: TripProfile,
        selected_city: str,
        destination: Optional[Destination] = None,
        revision_note: str = "",
    ) -> str:
        """
        Prompt for a day-wise itinerary. The LLM only proposes activity names,
        descriptions, areas, times and costs; addresses, images, distances and
        the stay are filled in by a separate tool step. `revision_note` is set
        when a previous draft did not support the traveler's chosen feeling
        (see src/core/feeling_block.py).
        """
        revision = f"\nREVISION REQUIRED: {revision_note}\n" if revision_note else ""
        prompt = f"""
Design a {profile.nights}-day trip in {selected_city} shaped around how this traveler wants to feel, not a checklist of tourist sights.
TRAVELER PROFILE:
{PromptGenerator._build_profile_summary(profile)}
{PromptGenerator._build_destination_summary(destination)}{revision}
{PromptGenerator._stop_requirement(profile, selected_city)}
REQUIREMENTS:
1. Create {profile.nights} days of activities. Each day belongs to one stop and every activity that day must be
   in or very near that stop's base area (within about {MAX_LEG_MINUTES} minutes by road of the base, and of the
   previous activity). Never mix activities from distant regions into the same day.
2. Pace: {TRIP_PACE_ITINERARY_GUIDANCE[profile.pace]}
3. {PromptGenerator._feeling_requirement(profile)}
4. {PromptGenerator._restriction_requirement(profile)}
5. Prefer these settings: {', '.join(profile.environment_labels)}.
6. Suit the whole travel party ({profile.party_phrase()}).
7. For each activity provide ONLY the activity name, a short description, the rough area/neighborhood,
   a suggested time window, an estimated cost per person in USD (an estimate, not a quote) and "why_selected".
8. {_WHY_SELECTED_RULE}
9. Time-sensitive activities (tours, tickets, performances) must say "confirm with the operator" in the description.
10. Do not repeat the same attraction/place on multiple days.
11. Do NOT include breakfast, lunch, or dinner stops. The system will add those as verified restaurant activities later.
{_RESPONSE_FIELD_RULES}
RESPONSE FORMAT (JSON ONLY):
{{
    "stops": [
        {{"base_area": "Town or neighbourhood to stay in", "nights": <number of nights>}}
    ],
    "tour_plan": [
        {{
            "day": 1,
            "stop": 1,
            "activities": [
                {{
                    "activity_name": "Activity Name",
                    "activity_description": "What you'll do",
                    "activity_location": "Area or neighborhood in the destination",
                    "activity_time": "HH:MM AM - HH:MM PM",
                    "activity_cost": <estimated cost per person in USD>,
                    "why_selected": "One specific sentence tying this to the traveler's answers"
                }}
            ]
        }}
    ],
    "total_cost_estimate": <sum of all activity costs across all days>,
    "packing_tips": "<Brief packing advice for this destination and timing>",
    "travel_tips": "<Brief travel advice; tell the traveler to check official entry and safety guidance>"
}}
Respond ONLY with valid JSON, no preamble.
"""
        return prompt

    @staticmethod
    def regenerate_tour_plan_prompt(
        profile: TripProfile,
        city_name: str,
        current_tour_plan: List[TourPlanDayInput],
        day_to_regenerate: Optional[int] = None,
        user_instruction: str = "",
        destination: Optional[Destination] = None,
        stops: Optional[List[dict]] = None,
    ) -> str:
        """Prompt for different activities (all days or one day) in the same destination and bases."""
        scope = f"Day {day_to_regenerate}" if day_to_regenerate else "the entire itinerary"
        instruction_context = f"\nTraveler's request: {user_instruction}" if user_instruction else ""
        current_plan_summary = PromptGenerator._build_plan_summary(current_tour_plan)
        prompt = f"""
You are an expert trip planner. Regenerate activities for {city_name}.
Previously suggested itinerary:
{current_plan_summary}
Now provide DIFFERENT activities for {scope} while keeping the rest the same.
TRAVELER PROFILE:
{PromptGenerator._build_profile_summary(profile)}
{PromptGenerator._build_destination_summary(destination)}{instruction_context}
KEEP THESE BASES (the traveler's stays do not change):
{PromptGenerator._stops_summary(stops)}
REQUIREMENTS:
1. Every activity must be in or very near its day's base area (within about {MAX_LEG_MINUTES} minutes by road of
   the base, and of the previous activity).
2. Pace: {TRIP_PACE_ITINERARY_GUIDANCE[profile.pace]}
3. {PromptGenerator._restriction_requirement(profile)} {PromptGenerator._feeling_requirement(profile)}
4. Suit the whole travel party ({profile.party_phrase()}).
5. For each activity provide ONLY the activity name, a short description, the rough area/neighborhood,
   a suggested time window, an estimated cost per person in USD (an estimate, not a quote) and "why_selected".
6. {_WHY_SELECTED_RULE}
7. Do not repeat attractions already present in the itinerary unless the traveler explicitly asked for that place.
8. Do NOT include breakfast, lunch, or dinner stops. The system will add those as verified restaurant activities later.
{_RESPONSE_FIELD_RULES}
RESPONSE FORMAT (JSON ONLY):
{{
    "tour_plan": [
        {{
            "day": <day number>,
            "activities": [
                {{
                    "activity_name": "Different Activity Name",
                    "activity_description": "What you'll do",
                    "activity_location": "Area or neighborhood",
                    "activity_time": "HH:MM AM - HH:MM PM",
                    "activity_cost": <estimated cost per person in USD>,
                    "why_selected": "One specific sentence tying this to the traveler's answers"
                }}
            ]
        }}
    ],
    "reasoning": "<Why these are good alternatives>"
}}
Respond ONLY with valid JSON, no preamble.
"""
        return prompt

    # ==================== HELPER METHODS ====================
    @staticmethod
    def _stop_requirement(profile: TripProfile, selected_city: str) -> str:
        """One base by default; more stops only for longer trips that need another region."""
        limit = max_stops_for(profile.nights)
        if limit == 1:
            return (
                f"BASE: plan the whole trip from ONE base area in {selected_city} (a town or neighbourhood to stay in). "
                'Return exactly one entry in "stops".'
            )
        return (
            f"BASES: plan from ONE base area in {selected_city} (a town or neighbourhood to stay in) whenever the "
            f"experiences fit within about {MAX_LEG_MINUTES} minutes of it. Only if the traveler's interests genuinely "
            f"need a region more than {MAX_LEG_MINUTES} minutes away, use up to {limit} stops, each of at least 2 nights, "
            f"with nights adding up to {profile.nights}. The first day at a new stop is a travel day: plan at most one "
            f"light activity after arrival. (Trips under {MIN_NIGHTS_FOR_SECOND_STOP} nights always use one base.)"
        )

    @staticmethod
    def _stops_summary(stops: Optional[List[dict]]) -> str:
        if not stops:
            return "- One base for the whole trip."
        return "\n".join(
            f"- Days {stop['first_day']}-{stop['last_day']}: stay in {stop['base_area']}" for stop in stops
        )

    @staticmethod
    def _build_profile_summary(profile: TripProfile) -> str:
        goals = "; ".join(
            f"{label} ({TRIP_GOAL_DEFINITIONS[goal]})"
            for goal, label in zip(profile.goals, profile.goal_labels)
        )
        feelings = ", ".join(RECENT_FEELING_LABELS[code] for code in profile.recent_feelings)
        restrictions = "; ".join(profile.restriction_phrases()) or "None"
        notes = f"\n- Other planning needs: {profile.restriction_notes}" if profile.restriction_notes else ""
        budget = (
            f"${profile.budget_per_night:,.0f}+ per room, per night (open-ended)"
            if profile.budget_open_ended
            else f"Up to ${profile.budget_per_night:,.0f} per room, per night"
        )
        return f"""
- Desired feelings (primary): {goals}
- Recent feelings (context only; do not interpret or diagnose): {feelings}
- What prompted the trip: {TRIP_PROMPT_LABELS[profile.trip_prompt]}
- Moments they enjoy: {', '.join(profile.moment_labels)}
- Preferred settings: {', '.join(profile.environment_labels)}
- Pace: {TRIP_PACE_LABELS[profile.pace]}
- Travel party: {profile.party_phrase()}
- Restrictions: {restrictions}{notes}
- Timing: {profile.timing_phrase()}
- Lodging budget: {budget}
"""

    @staticmethod
    def _build_destination_summary(destination: Optional[Destination]) -> str:
        if destination is None:
            return ""
        return f"""DESTINATION CONTEXT (editorial catalog notes, not verified facts):
- Experience context: {destination.experience_context}
- Setting: {destination.setting_context}
- Tradeoffs to plan around: {destination.tradeoffs_to_check}
"""

    @staticmethod
    def _feeling_requirement(profile: TripProfile) -> str:
        feelings = " and ".join(TRIP_GOAL_FEELING_WORDS[goal] for goal in profile.goals)
        return (
            f"The traveler chose to feel {feelings}. Across the trip, include at least two experiences that "
            "clearly give room for that feeling through the moments they enjoy. Do not promise an emotional "
            "result, diagnose the traveler, or add spa or spiritual programs they did not ask for."
        )

    @staticmethod
    def _restriction_requirement(profile: TripProfile) -> str:
        parts = []
        if profile.must_avoid:
            parts.append(
                "NEVER include an activity that conflicts with a 'Must avoid' restriction; "
                "if an activity's suitability is unclear, leave it out."
            )
        if profile.prefer_avoid:
            parts.append("Avoid 'Prefer to avoid' items where possible and say so when an activity involves one.")
        return " ".join(parts) or "No activity restrictions were given."

    @staticmethod
    def _build_plan_summary(tour_plan: List[TourPlanDayInput]) -> str:
        """Build human-readable summary of current tour plan."""
        summary = ""
        for day_plan in tour_plan:
            summary += f"\nDay {day_plan.day}:\n"
            for activity in day_plan.activities:
                summary += f"  - {activity.activity_name} ({activity.activity_time}) @ {activity.activity_location} (${activity.activity_cost})\n"
        return summary
