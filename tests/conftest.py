from unittest.mock import MagicMock

import pytest

from src.core.destination_places import clear_place_cache


@pytest.fixture(autouse=True)
def _session_db(monkeypatch, tmp_path):
    """Durable session storage goes to a per-test file, never into data/."""
    monkeypatch.setenv("SESSION_DB_PATH", str(tmp_path / "sessions.sqlite3"))


@pytest.fixture(autouse=True)
def _offline_map_lookups(monkeypatch):
    """
    Keep tests offline and deterministic: map lookups for the departure point
    and catalog destinations fail by default (so they are reported as
    UNVERIFIED), and the per-process place cache never leaks between tests.
    Tests that need a lookup result patch these again.
    """
    clear_place_cache()
    offline = MagicMock()
    offline.invoke.return_value = {"error": "offline in tests"}
    monkeypatch.setattr("src.core.destination_places.get_cityinfo", offline)
    monkeypatch.setattr("src.core.origin.get_cityinfo", offline)
    # Itinerary base-area lookups and routing: offline, so travel times fall
    # back to labelled straight-line estimates unless a test patches them.
    monkeypatch.setattr("app.router.city_content_route.get_cityinfo", offline)
    monkeypatch.setattr("src.core.travel_time.compute_route_matrix", lambda origins, destinations: {"error": "offline in tests"})
    monkeypatch.setattr("app.router.city_content_route.compute_drive_route", lambda origin, destination: {"error": "offline in tests"})
    yield
    clear_place_cache()
