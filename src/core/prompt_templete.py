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
from src.core.trip_profile import TripProfile

_RESPONSE_FIELD_RULES = """CRITICAL - Do NOT invent or include these fields. They will be filled by a real data tool later:
   - Do NOT include an exact street address (no "activity_address" field)
   - Do NOT include any image URLs (no "activity_image" field)
   - Do NOT include any distance values (no "distance_from_previous_km" field)
   - Do NOT state that anything is available, open, bookable or sold at a given price.
   Only the fields listed in the RESPONSE FORMAT below should be included."""


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
REQUIREMENTS:
1. Create {profile.nights} days of activities.
2. Pace: {TRIP_PACE_ITINERARY_GUIDANCE[profile.pace]}
3. {PromptGenerator._feeling_requirement(profile)}
4. {PromptGenerator._restriction_requirement(profile)}
5. Prefer these settings: {', '.join(profile.environment_labels)}.
6. Suit the whole travel party ({profile.party_phrase()}).
7. For each activity provide ONLY the activity name, a short description, the rough area/neighborhood,
   a suggested time window, and an estimated cost per person in USD (an estimate, not a quote).
8. Time-sensitive activities (tours, tickets, performances) must say "confirm with the operator" in the description.
9. Do not repeat the same attraction/place on multiple days.
10. Do NOT include breakfast, lunch, or dinner stops. The system will add those as verified restaurant activities later.
{_RESPONSE_FIELD_RULES}
RESPONSE FORMAT (JSON ONLY):
{{
    "tour_plan": [
        {{
            "day": 1,
            "activities": [
                {{
                    "activity_name": "Activity Name",
                    "activity_description": "What you'll do",
                    "activity_location": "Area or neighborhood in the destination",
                    "activity_time": "HH:MM AM - HH:MM PM",
                    "activity_cost": <estimated cost per person in USD>
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
    ) -> str:
        """Prompt for different activities (all days or one day) in the same destination."""
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
REQUIREMENTS:
1. Pace: {TRIP_PACE_ITINERARY_GUIDANCE[profile.pace]}
2. {PromptGenerator._restriction_requirement(profile)} {PromptGenerator._feeling_requirement(profile)}
3. Suit the whole travel party ({profile.party_phrase()}).
4. For each activity provide ONLY the activity name, a short description, the rough area/neighborhood,
   a suggested time window, and an estimated cost per person in USD (an estimate, not a quote).
5. Do not repeat attractions already present in the itinerary unless the traveler explicitly asked for that place.
6. Do NOT include breakfast, lunch, or dinner stops. The system will add those as verified restaurant activities later.
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
                    "activity_cost": <estimated cost per person in USD>
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
