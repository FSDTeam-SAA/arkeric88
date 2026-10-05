from copy import deepcopy
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from typing import Any, List, Optional
from uuid import uuid4

from app.schemas.city_body import CitySuggestionInput, StayInfo, TourPlanActivityInput, TourPlanDayInput
from src.session import session_persistence

CITY_KIND, ACTIVITY_KIND = "city", "activity"


# ==================== CITY SESSION ====================

@dataclass
class CitySession:
    """Session for destination suggestions (parent session)."""
    session_id: str
    # The validated Velari intake (TravelIntakeRequest.model_dump(mode="json")),
    # kept so regenerate and the itinerary step re-read the same answers.
    intake: dict
    suggested_cities: List[CitySuggestionInput]
    response: dict
    created_at: str
    updated_at: str
    history: List[dict] = field(default_factory=list)  # Track regenerations
    # destination_ids already surfaced across generate + regenerate calls, so
    # regeneration shows new options and the itinerary step only accepts a
    # destination the guest was actually offered.
    shown_destination_ids: List[str] = field(default_factory=list)


def _to_suggestions(cities: List[Any]) -> List[CitySuggestionInput]:
    return [CitySuggestionInput.model_validate(city) for city in cities]


def _to_stay(stay_data: dict) -> StayInfo:
    return StayInfo(
        **{
            "name": "N/A",
            "address": "N/A",
            "rating": 0.0,
            "price_level": "NOT_AVAILABLE",
            **{key: value for key, value in stay_data.items() if key in StayInfo.model_fields},
        }
    )


def _to_day(day: dict) -> TourPlanDayInput:
    if isinstance(day, TourPlanDayInput):
        return day
    return TourPlanDayInput(
        day=day.get("day"),
        stop=day.get("stop") or 1,
        day_type=day.get("day_type") or "standard",
        activities=[
            {
                **{key: value for key, value in act.items() if key in TourPlanActivityInput.model_fields},
                "activity_address": act.get("activity_address", "N/A"),
                "activity_image": act.get("activity_image", []),
                "activity_cost": act.get("activity_cost", 0),
            }
            for act in day.get("activities", [])
        ],
    )


def _city_payload(session: "CitySession") -> dict:
    payload = {item.name: deepcopy(getattr(session, item.name)) for item in fields(session)}
    payload["suggested_cities"] = [city.model_dump(mode="json") for city in session.suggested_cities]
    return payload


def _city_from_payload(payload: dict) -> "CitySession":
    return CitySession(**{**payload, "suggested_cities": _to_suggestions(payload.get("suggested_cities", []))})


class CitySessionStore:
    """
    Destination suggestion sessions: kept in memory and written through to
    durable storage (session_persistence), so a saved search can still be
    reopened after a restart.
    """
    _sessions: dict[str, CitySession] = {}

    @classmethod
    def _live(cls, session_id: str) -> Optional["CitySession"]:
        session = cls._sessions.get(session_id)
        if session is None:
            payload = session_persistence.load(CITY_KIND, session_id)
            if payload is not None:
                session = _city_from_payload(payload)
                cls._sessions[session_id] = session
        return session

    @classmethod
    def _save(cls, session: "CitySession") -> None:
        session_persistence.save(CITY_KIND, session.session_id, _city_payload(session))

    @classmethod
    def create(
        cls,
        intake: dict,
        suggested_cities: List[Any],
        response: dict,
        shown_destination_ids: Optional[List[str]] = None,
    ) -> CitySession:
        """Create a new session when the guest first submits the intake."""
        session_id = str(uuid4())
        now = datetime.now(timezone.utc).isoformat()

        session = CitySession(
            session_id=session_id,
            intake=deepcopy(intake),
            suggested_cities=_to_suggestions(suggested_cities),
            response=deepcopy(response),
            created_at=now,
            updated_at=now,
            history=[
                {
                    "action": "generated",
                    "timestamp": now,
                    "suggested_cities_count": len(suggested_cities),
                    "match_status": response.get("match_status"),
                }
            ],
            shown_destination_ids=list(shown_destination_ids or []),
        )

        cls._sessions[session_id] = session
        cls._save(session)
        return deepcopy(session)

    @classmethod
    def get(cls, session_id: str) -> Optional[CitySession]:
        """Retrieve a city session by ID."""
        session = cls._live(session_id)
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
        shown_destination_ids: Optional[List[str]] = None,
        intake: Optional[dict] = None,
    ) -> Optional[CitySession]:
        """Replace the suggestions on regenerate (optionally with refined answers)."""
        session = cls._live(session_id)
        if session is None:
            return None

        new_cities = response.get("suggested_cities", [])
        session.suggested_cities = _to_suggestions(new_cities)
        session.response = deepcopy(response)
        now = datetime.now(timezone.utc).isoformat()
        session.updated_at = now
        if shown_destination_ids is not None:
            session.shown_destination_ids = list(shown_destination_ids)
        if intake is not None:
            session.intake = deepcopy(intake)

        session.history.append(
            {
                "action": "regenerated",
                "timestamp": now,
                "update_field_name": update_field_name,
                "user_instruction": user_instruction,
                "intake_updated": intake is not None,
                "suggested_cities_count": len(new_cities),
                "match_status": response.get("match_status"),
            }
        )

        cls._save(session)
        return deepcopy(session)

    @classmethod
    def delete(cls, session_id: str) -> bool:
        """Delete a city session."""
        in_memory = cls._sessions.pop(session_id, None) is not None
        stored = session_persistence.delete(CITY_KIND, session_id)
        return in_memory or stored

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
    destination_id: Optional[str] = None  # Catalog entry the plan was built for


def _activity_payload(session: "ActivitySession") -> dict:
    payload = {item.name: deepcopy(getattr(session, item.name)) for item in fields(session)}
    payload["tour_plan"] = [day.model_dump(mode="json") for day in session.tour_plan]
    payload["stay"] = session.stay.model_dump(mode="json") if session.stay is not None else None
    return payload


def _activity_from_payload(payload: dict) -> "ActivitySession":
    return ActivitySession(**{
        **payload,
        "tour_plan": [_to_day(day) for day in payload.get("tour_plan", [])],
        "stay": _to_stay(payload["stay"]) if payload.get("stay") else None,
    })


class ActivitySessionStore:
    """
    Itinerary sessions: kept in memory and written through to durable storage
    (session_persistence), so a saved itinerary reopens after a restart.
    """
    _sessions: dict[str, ActivitySession] = {}

    @classmethod
    def _live(cls, activity_session_id: str) -> Optional["ActivitySession"]:
        session = cls._sessions.get(activity_session_id)
        if session is None:
            payload = session_persistence.load(ACTIVITY_KIND, activity_session_id)
            if payload is not None:
                session = _activity_from_payload(payload)
                cls._sessions[activity_session_id] = session
        return session

    @classmethod
    def _save(cls, session: "ActivitySession") -> None:
        session_persistence.save(
            ACTIVITY_KIND, session.activity_session_id, _activity_payload(session), session.parent_session_id,
        )

    @classmethod
    def _children(cls, parent_session_id: str) -> List["ActivitySession"]:
        """Every activity session of a parent, from memory and from storage."""
        for payload in session_persistence.load_children(ACTIVITY_KIND, parent_session_id):
            if payload["activity_session_id"] not in cls._sessions:
                cls._sessions[payload["activity_session_id"]] = _activity_from_payload(payload)
        return [session for session in cls._sessions.values() if session.parent_session_id == parent_session_id]

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
        destination_id: Optional[str] = None,
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
            stay_obj = _to_stay(stay_data)
        
        session = ActivitySession(
            activity_session_id=activity_session_id,
            parent_session_id=parent_session_id,
            city=city_name,
            tour_plan=[
                _to_day(day)
                for day in tour_plan
            ],
            response=deepcopy(response),
            created_at=now,
            updated_at=now,
            stay=stay_obj,
            total_cost_estimate=total_cost_estimate,
            packing_tips=packing_tips,
            travel_tips=travel_tips,
            destination_id=destination_id,
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
        cls._save(session)
        return deepcopy(session)

    @classmethod
    def get(cls, activity_session_id: str) -> Optional[ActivitySession]:
        """Retrieve an activity session by ID."""
        session = cls._live(activity_session_id)
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
        for session in cls._children(session_id):
            if session.city == city_name:
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
        session = cls._live(activity_session_id)
        if session is None:
            return None

        new_tour_plan = response.get("tour_plan", [])

        if day_to_regenerate is None:
            # Regenerate entire plan
            session.tour_plan = [
                _to_day(day)
                for day in new_tour_plan
            ]
        else:
            # Regenerate single day
            for new_day in new_tour_plan:
                if new_day.get("day") == day_to_regenerate:
                    # Find and replace the day in existing plan
                    for i, existing_day in enumerate(session.tour_plan):
                        if existing_day.day == day_to_regenerate:
                            session.tour_plan[i] = _to_day(new_day)
                            break

        # Update stay if new data provided
        if stay_data:
            session.stay = _to_stay(stay_data)

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

        cls._save(session)
        return deepcopy(session)

    @classmethod
    def delete(cls, activity_session_id: str) -> bool:
        """Delete an activity session."""
        in_memory = cls._sessions.pop(activity_session_id, None) is not None
        stored = session_persistence.delete(ACTIVITY_KIND, activity_session_id)
        return in_memory or stored

    @classmethod
    def delete_by_parent(cls, parent_session_id: str) -> int:
        """Delete all activity sessions linked to a parent city session."""
        to_delete = [session.activity_session_id for session in cls._children(parent_session_id)]
        for activity_session_id in to_delete:
            cls.delete(activity_session_id)
        return len(to_delete)

    @classmethod
    def list_by_parent(cls, parent_session_id: str) -> List[ActivitySession]:
        """List all activity sessions for a given city session."""
        return [deepcopy(session) for session in cls._children(parent_session_id)]

    @classmethod
    def list_all(cls) -> List[ActivitySession]:
        """List all active activity sessions (for debugging)."""
        return [deepcopy(s) for s in cls._sessions.values()]