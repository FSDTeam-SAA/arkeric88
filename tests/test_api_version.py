"""Every endpoint is served under /v2; the unversioned paths still work but are not documented."""

from fastapi.testclient import TestClient

import main
from intake_fixtures import build_intake

client = TestClient(main.app)


def test_swagger_lists_only_v2_paths():
    paths = client.get("/openapi.json").json()["paths"]
    assert "/v2/get_suggested_city" in paths
    assert "/v2/recommend_travel_dates" in paths
    assert all(path.startswith("/v2/") for path in paths)


def test_unversioned_paths_still_work_for_existing_clients():
    body = build_intake()
    v2 = client.post("/v2/get_suggested_city", json=body)
    legacy = client.post("/get_suggested_city", json=body)
    assert v2.status_code == legacy.status_code == 200
    assert [city["destination_id"] for city in v2.json()["suggested_cities"]] == [
        city["destination_id"] for city in legacy.json()["suggested_cities"]
    ]
    # A session made on one path is readable on the other.
    session_id = v2.json()["session_id"]
    assert client.get(f"/session/{session_id}").status_code == 200
    assert client.get(f"/v2/session/{session_id}").status_code == 200
