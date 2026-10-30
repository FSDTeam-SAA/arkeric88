"""Every endpoint is served under /v1; the unversioned paths still work but are not documented."""

from fastapi.testclient import TestClient

import main
from intake_fixtures import build_intake

client = TestClient(main.app)


def test_swagger_lists_only_v1_paths():
    paths = client.get("/openapi.json").json()["paths"]
    assert "/v1/get_suggested_city" in paths
    assert "/v1/recommend_travel_dates" in paths
    assert all(path.startswith("/v1/") for path in paths)


def test_unversioned_paths_still_work_for_existing_clients():
    body = build_intake()
    v1 = client.post("/v1/get_suggested_city", json=body)
    legacy = client.post("/get_suggested_city", json=body)
    assert v1.status_code == legacy.status_code == 200
    assert [city["destination_id"] for city in v1.json()["suggested_cities"]] == [
        city["destination_id"] for city in legacy.json()["suggested_cities"]
    ]
    # A session made on one path is readable on the other.
    session_id = v1.json()["session_id"]
    assert client.get(f"/session/{session_id}").status_code == 200
    assert client.get(f"/v1/session/{session_id}").status_code == 200


def test_v2_paths_do_not_exist():
    assert client.post("/v2/get_suggested_city", json=build_intake()).status_code == 404
