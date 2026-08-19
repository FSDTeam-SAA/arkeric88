from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, List, Optional
from uuid import uuid4

from app.schemas.city_body import QuestionAnswers, CitySuggestionInput, TourPlanDayInput, StayInfo


# ==================== CITY SESSION ====================

@dataclass
class CitySession:
    """Session for city suggestions (parent session)."""
    session_id: str
    questions_answers: QuestionAnswers
    suggested_cities: List[CitySuggestionInput]
    response: dict  # Full AI response
    created_at: str
    updated_at: str
    history: List[dict] = field(default_factory=list)  # Track regenerations
    # The original v2 15-question request (see RetreatRecommendationRequest),
    # kept so /regenerate_suggested_city can re-run the identical deterministic
    # matching pipeline. None for any session created before this field
    # existed -- regenerate is not available for those (see city_content_route.py).
    v2_request: Optional[dict] = None
    # property_ids already surfaced to the user across generate + regenerate
    # calls, so regeneration can exclude them and always show distinct cities.
    shown_property_ids: List[str] = field(default_factory=list)


class CitySessionStore:
    """In-memory store for city suggestion sessions."""
    _sessions: dict[str, CitySession] = {}

    @classmethod
    def create(
        cls,
        questions_answers: QuestionAnswers,
        suggested_cities: List[Any],
        response: dict,
        v2_request: Optional[dict] = None,
        shown_property_ids: Optional[List[str]] = None,
    ) -> CitySession:
        """
        Create a new city session.
        Called when user generates city suggestions for first time.
        """
        session_id = str(uuid4())
        now = datetime.now(timezone.utc).isoformat()

        session = CitySession(
            session_id=session_id,
            questions_answers=questions_answers,
            suggested_cities=[
                CitySuggestionInput(
                    city_name=city.get("city_name"),
                    country_name=city.get("country_name"),
                    number_of_days=city.get("number_of_days"),
                    description=city.get("description"),
                    city_image=city.get("city_image", []),
                    latitude=city.get("latitude"),
                    longitude=city.get("longitude"),
                    property_id=city.get("property_id"),
                    match_score=city.get("match_score"),
                    warnings=city.get("warnings", []),
                )
                for city in suggested_cities
            ],
            response=deepcopy(response),
            created_at=now,
            updated_at=now,
            history=[
                {
                    "action": "generated",
                    "timestamp": now,
                    "suggested_cities_count": len(suggested_cities),
                }
            ],
            v2_request=deepcopy(v2_request) if v2_request is not None else None,
            shown_property_ids=list(shown_property_ids or []),
        )

        cls._sessions[session_id] = session
        return deepcopy(session)

    @classmethod
    def get(cls, session_id: str) -> Optional[CitySession]:
        """Retrieve a city session by ID."""
        session = cls._sessions.get(session_id)
        if session is None:
            return None
        return deepcopy(session)

    @classmethod
    def update_response(
        cls,
        session_id: str,
        response: dict,
        update_field_name: str,
        user_instruction: str,
        shown_property_ids: Optional[List[str]] = None,
    ) -> Optional[CitySession]:
        """
        Update session with new AI response (on regenerate).
        Called when user regenerates city suggestions.
        """
        session = cls._sessions.get(session_id)
        if session is None:
            return None

        # Extract new suggested cities from response
        new_cities = response.get("suggested_cities", [])
        session.suggested_cities = [
            CitySuggestionInput(
                city_name=city.get("city_name"),
                country_name=city.get("country_name"),
                number_of_days=city.get("number_of_days"),
                description=city.get("description"),
                city_image=city.get("city_image", []),
                latitude=city.get("latitude"),
                longitude=city.get("longitude"),
                property_id=city.get("property_id"),
                match_score=city.get("match_score"),
                warnings=city.get("warnings", []),
            )
            for city in new_cities
        ]

        # Update response and timestamp
        session.response = deepcopy(response)
        now = datetime.now(timezone.utc).isoformat()
        session.updated_at = now
        if shown_property_ids is not None:
            session.shown_property_ids = list(shown_property_ids)

        # Track regeneration in history
        session.history.append(
            {
                "action": "regenerated",
                "timestamp": now,
                "update_field_name": update_field_name,
                "user_instruction": user_instruction,
                "suggested_cities_count": len(new_cities),
            }
        )

        return deepcopy(session)

    @classmethod
    def delete(cls, session_id: str) -> bool:
        """Delete a city session."""
        return cls._sessions.pop(session_id, None) is not None

    @classmethod
    def list_all(cls) -> List[CitySession]:
        """List all active city sessions (for debugging)."""
        return [deepcopy(s) for s in cls._sessions.values()]


# ==================== ACTIVITY SESSION ====================

@dataclass
class ActivitySession:
    """Session for day-wise activities (child session, linked to city session)."""
    activity_session_id: str
    parent_session_id: str  # Link to CitySession
    city: str
    tour_plan: List[TourPlanDayInput]
    response: dict  # Full AI response
    created_at: str
    updated_at: str
    history: List[dict] = field(default_factory=list)  # Track regenerations
    stay: Optional[StayInfo] = None  # Hotel/stay info
    total_cost_estimate: float = 0.0  # Combined hotel + activities total
    packing_tips: str = ""
    travel_tips: str = ""


class ActivitySessionStore:
    """In-memory store for activity/tour plan sessions."""
    _sessions: dict[str, ActivitySession] = {}

    @classmethod
    def create(
        cls,
        parent_session_id: str,
        city_name: str,
        tour_plan: List[Any],
        response: dict,
        stay_data: Optional[dict] = None,
        total_cost_estimate: float = 0.0,
        packing_tips: str = "",
        travel_tips: str = "",
    ) -> ActivitySession:
        """
        Create a new activity session.
        Called when user generates tour plan for selected city for first time.
        """
        activity_session_id = str(uuid4())
        now = datetime.now(timezone.utc).isoformat()
        
        # Build stay object from raw data if provided
        stay_obj = None
        if stay_data:
            stay_obj = StayInfo(
                name=stay_data.get("name", "N/A"),
                address=stay_data.get("address", "N/A"),
                rating=stay_data.get("rating", 0.0),
                price_level=stay_data.get("price_level", "NOT_AVAILABLE"),
                photos=stay_data.get("photos", []),
                coords=stay_data.get("coords"),
                average_nightly_price=stay_data.get("average_nightly_price", ""),
                budget_tier=stay_data.get("budget_tier", ""),
                facilities=stay_data.get("facilities", []),
                website=stay_data.get("website", ""),
                estimate_note=stay_data.get("estimate_note", ""),
            )
        
        session = ActivitySession(
            activity_session_id=activity_session_id,
            parent_session_id=parent_session_id,
            city=city_name,
            tour_plan=[
                TourPlanDayInput(
                    day=day.get("day"),
                    activities=[
                        {
                            "activity_name": act.get("activity_name"),
                            "activity_description": act.get("activity_description"),
                            "activity_location": act.get("activity_location"),
                            "activity_address": act.get("activity_address", "N/A"),
                            "activity_image": act.get("activity_image", []),
                            "activity_time": act.get("activity_time"),
                            "activity_cost": act.get("activity_cost", 0),
                            "distance_from_previous_km": act.get("distance_from_previous_km"),
                        }
                        for act in day.get("activities", [])
                    ],
                )
                for day in tour_plan
            ],
            response=deepcopy(response),
            created_at=now,
            updated_at=now,
            stay=stay_obj,
            total_cost_estimate=total_cost_estimate,
            packing_tips=packing_tips,
            travel_tips=travel_tips,
            history=[
                {
                    "action": "generated",
                    "timestamp": now,
                    "city": city_name,
                    "days_count": len(tour_plan),
                    "total_cost_estimate": total_cost_estimate,
                }
            ],
        )
        
        cls._sessions[activity_session_id] = session
        return deepcopy(session)

    @classmethod
    def get(cls, activity_session_id: str) -> Optional[ActivitySession]:
        """Retrieve an activity session by ID."""
        session = cls._sessions.get(activity_session_id)
        if session is None:
            return None
        return deepcopy(session)

    @classmethod
    def get_by_city(
        cls,
        session_id: str,
        city_name: str,
    ) -> Optional[ActivitySession]:
        """
        Retrieve activity session by parent session ID and city name.
        Used to check if activities already exist for this city (caching).
        """
        for session in cls._sessions.values():
            if session.parent_session_id == session_id and session.city == city_name:
                return deepcopy(session)
        return None

    @classmethod
    def update_response(
        cls,
        activity_session_id: str,
        response: dict,
        day_to_regenerate: Optional[int] = None,
        user_instruction: str = "",
        stay_data: Optional[dict] = None,
        total_cost_estimate: Optional[float] = None,
    ) -> Optional[ActivitySession]:
        """
        Update session with new AI response (on regenerate).
        Called when user regenerates activities.
        - If day_to_regenerate is None: regenerate entire plan.
        - If day_to_regenerate is int: regenerate only that day.
        """
        session = cls._sessions.get(activity_session_id)
        if session is None:
            return None

        new_tour_plan = response.get("tour_plan", [])

        if day_to_regenerate is None:
            # Regenerate entire plan
            session.tour_plan = [
                TourPlanDayInput(
                    day=day.get("day"),
                    activities=[
                        {
                            "activity_name": act.get("activity_name"),
                            "activity_description": act.get("activity_description"),
                            "activity_location": act.get("activity_location"),
                            "activity_address": act.get("activity_address", "N/A"),
                            "activity_image": act.get("activity_image", []),
                            "activity_time": act.get("activity_time"),
                            "activity_cost": act.get("activity_cost", 0),
                            "distance_from_previous_km": act.get("distance_from_previous_km"),
                        }
                        for act in day.get("activities", [])
                    ],
                )
                for day in new_tour_plan
            ]
        else:
            # Regenerate single day
            for new_day in new_tour_plan:
                if new_day.get("day") == day_to_regenerate:
                    # Find and replace the day in existing plan
                    for i, existing_day in enumerate(session.tour_plan):
                        if existing_day.day == day_to_regenerate:
                            session.tour_plan[i] = TourPlanDayInput(
                                day=new_day.get("day"),
                                activities=[
                                    {
                                        "activity_name": act.get("activity_name"),
                                        "activity_description": act.get("activity_description"),
                                        "activity_location": act.get("activity_location"),
                                        "activity_address": act.get("activity_address", "N/A"),
                                        "activity_image": act.get("activity_image", []),
                                        "activity_time": act.get("activity_time"),
                                        "activity_cost": act.get("activity_cost", 0),
                                        "distance_from_previous_km": act.get("distance_from_previous_km"),
                                    }
                                    for act in new_day.get("activities", [])
                                ],
                            )
                            break

        # Update stay if new data provided
        if stay_data:
            session.stay = StayInfo(
                name=stay_data.get("name", "N/A"),
                address=stay_data.get("address", "N/A"),
                rating=stay_data.get("rating", 0.0),
                price_level=stay_data.get("price_level", "NOT_AVAILABLE"),
                photos=stay_data.get("photos", []),
                coords=stay_data.get("coords"),
                average_nightly_price=stay_data.get("average_nightly_price", ""),
                budget_tier=stay_data.get("budget_tier", ""),
                facilities=stay_data.get("facilities", []),
                website=stay_data.get("website", ""),
                estimate_note=stay_data.get("estimate_note", ""),
            )

        # Update total cost estimate if provided
        if total_cost_estimate is not None:
            session.total_cost_estimate = total_cost_estimate

        # Update response and timestamp
        session.response = deepcopy(response)
        now = datetime.now(timezone.utc).isoformat()
        session.updated_at = now

        # Track regeneration in history
        scope = f"day {day_to_regenerate}" if day_to_regenerate else "entire itinerary"
        session.history.append(
            {
                "action": "regenerated",
                "timestamp": now,
                "scope": scope,
                "user_instruction": user_instruction,
                "total_cost_estimate": total_cost_estimate or session.total_cost_estimate,
            }
        )

        return deepcopy(session)

    @classmethod
    def delete(cls, activity_session_id: str) -> bool:
        """Delete an activity session."""
        return cls._sessions.pop(activity_session_id, None) is not None

    @classmethod
    def delete_by_parent(cls, parent_session_id: str) -> int:
        """Delete all activity sessions linked to a parent city session."""
        to_delete = [
            sid for sid, s in cls._sessions.items()
            if s.parent_session_id == parent_session_id
        ]
        for sid in to_delete:
            cls._sessions.pop(sid, None)
        return len(to_delete)

    @classmethod
    def list_by_parent(cls, parent_session_id: str) -> List[ActivitySession]:
        """List all activity sessions for a given city session."""
        return [
            deepcopy(s) for s in cls._sessions.values()
            if s.parent_session_id == parent_session_id
        ]

    @classmethod
    def list_all(cls) -> List[ActivitySession]:
        """List all active activity sessions (for debugging)."""
        return [deepcopy(s) for s in cls._sessions.values()]