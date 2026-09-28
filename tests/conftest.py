from unittest.mock import MagicMock

import pytest

from src.core.destination_places import clear_place_cache


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
    yield
    clear_place_cache()
