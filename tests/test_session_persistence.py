"""Saved searches and itineraries survive a restart (Search History / eye icon)."""

from fastapi.testclient import TestClient

import main
from src.session.city_session_store import ActivitySessionStore, CitySessionStore
from intake_fixtures import build_intake

client = TestClient(main.app)


def _restart() -> None:
    """Drop everything held in process memory, as a restart or redeploy would."""
    CitySessionStore._sessions.clear()
    ActivitySessionStore._sessions.clear()


def test_saved_search_reopens_after_a_restart():
    created = client.post("/get_suggested_city", json=build_intake()).json()
    _restart()
    reopened = client.get(f"/session/{created['session_id']}")
    assert reopened.status_code == 200
    assert [city["destination_id"] for city in reopened.json()["suggested_cities"]] == [
        city["destination_id"] for city in created["suggested_cities"]
    ]


def test_saved_itinerary_reopens_after_a_restart():
    session = CitySessionStore.create(intake={"trip_goals": ["reflection"]}, suggested_cities=[], response={})
    activity = ActivitySessionStore.create(
        parent_session_id=session.session_id,
        city_name="Kyoto",
        tour_plan=[{"day": 1, "stop": 1, "day_type": "standard", "activities": [{
            "activity_name": "Moss garden walk", "activity_description": "Quiet walk", "activity_location": "Kyoto",
            "activity_time": "09:00 AM - 10:00 AM", "item_type": "experience", "why_selected": "Room to think.",
            "latitude": 35.0, "longitude": 135.7,
        }]}],
        response={"stops": [{"stop": 1, "base_area": "Kyoto"}], "price_breakdown": {"total": 100.0}},
        stay_data={"name": "Quiet Inn", "address": "1 Inn Rd", "rating": 4.5, "price_level": "$$ · Moderate"},
    )
    _restart()
    reopened = ActivitySessionStore.get(activity.activity_session_id)
    assert reopened is not None
    assert reopened.tour_plan[0].activities[0].latitude == 35.0
    assert reopened.stay.name == "Quiet Inn"
    assert reopened.response["price_breakdown"]["total"] == 100.0
    assert ActivitySessionStore.get_by_city(session.session_id, "Kyoto") is not None


def test_deleting_a_search_removes_it_from_storage_too():
    created = client.post("/get_suggested_city", json=build_intake()).json()
    assert client.delete(f"/session/{created['session_id']}").status_code == 200
    _restart()
    assert client.get(f"/session/{created['session_id']}").status_code == 404


def test_unknown_session_is_still_a_clear_404():
    response = client.get("/session/does-not-exist")
    assert response.status_code == 404
    assert response.json()["detail"] == "Session not found."


def test_cross_origin_requests_are_allowed():
    response = client.options(
        "/get_suggested_city",
        headers={"Origin": "https://app.example.com", "Access-Control-Request-Method": "POST"},
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] in {"*", "https://app.example.com"}
