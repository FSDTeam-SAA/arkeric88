from typing import List, Optional
from app.schemas.city_body import QuestionAnswers, TourPlanDayInput


class PromptGenerator:
    """
    Generate profile-led wellness journey prompts without changing output schemas.

    City suggestion is no longer prompt-led: POST /get_suggested_city now runs
    the deterministic property-matching engine
    (src/core/retreat_matching_orchestrator.py) and groups real ranked
    properties into cities instead of asking a model to invent destinations.
    Only the itinerary/activity prompts below remain -- Step 2 of the flow
    (POST /get_tour_plan) is unchanged. See API_CITY_FLOW_DOCS.md.
    """

    # ==================== TOUR PLAN / ACTIVITY PROMPTS ====================
    @staticmethod
    def gen_tour_plan_prompt(
        questions_answers: QuestionAnswers,
        selected_city: str,
        trip_length_days: int,
    ) -> str:
        """
        Generate prompt for day-wise activity itinerary.
        Generate flow: User selected a city -> AI creates day-by-day activities.
        The LLM only proposes activity names, descriptions, areas, times, and costs.
        Addresses, images, distances, and hotel info will be filled in by a separate tool step.
        """
        qa_summary = PromptGenerator._build_qa_summary(questions_answers)
        prompt = f"""
Design a {trip_length_days}-day restorative or transformative wellness journey in {selected_city}, not a checklist of tourist sights.
USER PROFILE:
{qa_summary}
REQUIREMENTS:
1. Create {trip_length_days} days of activities.
2. Match their travel style: {questions_answers.travel_style}
3. Fit their energy level: {questions_answers.energy_level}
4. Stay within the effective one-person trip budget: ${questions_answers.effective_total_budget:.2f}.
5. AVOID these activities: {', '.join(questions_answers.activity_restrictions)}
6. Prefer these environments: {', '.join(questions_answers.preferred_environments)}
7. Every activity must advance the desired emotional outcome and match the traveler's safe intensity and preferred structure.
8. Use dynamic tool queries combining activity intent, profile needs, environment, and city.
9. For each sightseeing/activity stop, provide ONLY the activity name, a short description, the rough area/neighborhood in the city,
   a suggested time window, and an estimated cost in USD.
10. Do not repeat the same attraction/place on multiple days; distribute unique places across the full itinerary.
11. Do NOT include breakfast, lunch, or dinner stops. The system will add those as verified restaurant activities later.
CRITICAL - Do NOT invent or include these fields. They will be filled by a real data tool later:
   - Do NOT include an exact street address (no "activity_address" field)
   - Do NOT include any image URLs (no "activity_image" field)
   - Do NOT include any distance values (no "distance_from_previous_km" field)
   Only the fields listed in the RESPONSE FORMAT below should be included.
RESPONSE FORMAT (JSON ONLY):
{{
    "tour_plan": [
        {{
            "day": 1,
            "activities": [
                {{
                    "activity_name": "Activity Name",
                    "activity_description": "What you'll do",
                    "activity_location": "Exact location in city",
                    "activity_time": "HH:MM AM - HH:MM PM",
                    "activity_cost": <cost in USD>
                }}
            ]
        }}
    ],
    "total_cost_estimate": <sum of all activity costs across all days>,
    "packing_tips": "<Brief packing advice for this city>",
    "travel_tips": "<Brief travel advice>"
}}
Respond ONLY with valid JSON, no preamble.
"""
        return prompt
    @staticmethod
    def regenerate_tour_plan_prompt(
        questions_answers: QuestionAnswers,
        city_name: str,
        current_tour_plan: List[TourPlanDayInput],
        day_to_regenerate: Optional[int] = None,
        user_instruction: str = "",
    ) -> str:
        """
        Generate prompt to get different activities.
        Regenerate flow: Keep city, regenerate activities (all or single day).
        The LLM only proposes activity names, descriptions, areas, times, and costs.
        Addresses, images, distances, and hotel info will be filled in by a separate tool step.
        """
        qa_summary = PromptGenerator._build_qa_summary(questions_answers)
        scope = f"Day {day_to_regenerate}" if day_to_regenerate else "the entire itinerary"
        instruction_context = ""
        if user_instruction:
            instruction_context = f"\nUser's request: {user_instruction}"
        current_plan_summary = PromptGenerator._build_plan_summary(current_tour_plan)
        prompt = f"""
You are an expert tour planner. Regenerate activities for {city_name}.
Previously suggested itinerary:
{current_plan_summary}
Now provide DIFFERENT activities for {scope} while keeping the rest the same.
USER PROFILE:
{qa_summary}
{instruction_context}
REQUIREMENTS:
1. Match travel style: {questions_answers.travel_style}
2. Match energy level: {questions_answers.energy_level}
3. Stay within the effective one-person trip budget: ${questions_answers.effective_total_budget:.2f}.
4. AVOID: {', '.join(questions_answers.activity_restrictions)}
5. For each sightseeing/activity stop, provide ONLY the activity name, a short description, the rough area/neighborhood,
   a suggested time window, and an estimated cost in USD.
6. Do not repeat attractions already present in the itinerary unless the user explicitly asked for that place.
7. Do NOT include breakfast, lunch, or dinner stops. The system will add those as verified restaurant activities later.
CRITICAL - Do NOT invent or include these fields. They will be filled by a real data tool later:
   - Do NOT include an exact street address (no "activity_address" field)
   - Do NOT include any image URLs (no "activity_image" field)
   - Do NOT include any distance values (no "distance_from_previous_km" field)
   Only the fields listed in the RESPONSE FORMAT below should be included.
RESPONSE FORMAT (JSON ONLY):
{{
    "tour_plan": [
        {{
            "day": <day number>,
            "activities": [
                {{
                    "activity_name": "Different Activity Name",
                    "activity_description": "What you'll do",
                    "activity_location": "Exact location",
                    "activity_time": "HH:MM AM - HH:MM PM",
                    "activity_cost": <cost in USD>
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
    def _build_qa_summary(qa: QuestionAnswers) -> str:
        """Build human-readable summary of Q&A."""
        budget_line = (
            f"${qa.budget_per_person_per_night:.2f} per person/night "
            f"(${qa.effective_total_budget:.2f} for one traveler)"
            if qa.budget_per_person_per_night is not None
            else f"${qa.effective_total_budget:.2f} total (legacy input)"
        )
        # birthdate is optional -- the v2 15-question flow never collects age
        # (see src/core/legacy_profile_adapter.py), so the age line is only
        # included when a birthdate was actually supplied.
        age_line = (
            f"- Age (from birthdate): {PromptGenerator._calculate_age(qa.birthdate)}\n"
            if qa.birthdate is not None
            else ""
        )
        return f"""
- Current feeling: {qa.todays_feeling}
- Desired experience: {qa.experience_kind}
- Energy level: {qa.energy_level}
- Travel style: {qa.travel_style}
- Trip organization preference: {qa.trip_organization}
- Restricted activities: {', '.join(qa.activity_restrictions) if qa.activity_restrictions else 'None'}
- Season of life: {qa.life_season}
- Preferred environments: {', '.join(qa.preferred_environments)}
{age_line}- Trip budget: {budget_line}
- Trip duration: {qa.trip_length_days} days
- Preferred region: {qa.preferred_region or 'No region constraint supplied'}
"""
    @staticmethod
    def _build_plan_summary(tour_plan: List[TourPlanDayInput]) -> str:
        """Build human-readable summary of current tour plan."""
        summary = ""
        for day_plan in tour_plan:
            summary += f"\nDay {day_plan.day}:\n"
            for activity in day_plan.activities:
                summary += f"  - {activity.activity_name} ({activity.activity_time}) @ {activity.activity_location} (${activity.activity_cost})\n"
        return summary
    @staticmethod
    def _calculate_age(birthdate) -> int:
        """Calculate age from birthdate."""
        from datetime import date
        today = date.today()
        return today.year - birthdate.year - ((today.month, today.day) < (birthdate.month, birthdate.day))
