from unittest.mock import MagicMock, Mock, patch

from app.router.city_content_route import _enrich_destination_media
from src.core.destination_catalog import get_destination
from src.core.destination_places import lookup_destination_place, lookup_missing_coordinates, lookup_name
from src.core.geography import country_matches_region, great_circle_km, region_for_country, same_country
from src.tools.tools import get_cityinfo


def _tool(return_value) -> MagicMock:
    tool = MagicMock()
    tool.invoke.return_value = return_value
    return tool


def test_known_country_region_validation():
    assert country_matches_region("Canada", "North America") is True
    assert country_matches_region("Bangladesh", "North America") is False
    assert country_matches_region("Brazil", "South America") is True
    assert country_matches_region("Bangladesh", "South America") is False
    assert region_for_country("Portugal") == "europe"
    assert region_for_country("Atlantis") is None


def test_country_names_from_different_sources_compare_equal():
    assert same_country("United States", "United States of America")
    assert same_country("USA", "united states")
    assert not same_country("Seychelles", "Canada")
    assert not same_country(None, "Canada")


def test_great_circle_distance():
    lisbon_to_madrid = great_circle_km(38.7223, -9.1393, 40.4168, -3.7038)
    assert 490 < lisbon_to_madrid < 510


def test_lookup_uses_locality_part_of_the_destination_name():
    assert lookup_name("Tucson & Sonoran Desert") == "Tucson"
    assert lookup_name("São Miguel, Azores") == "São Miguel, Azores"


def test_same_named_place_in_the_wrong_country_contributes_nothing():
    # Victoria, Seychelles must never pick up Victoria, Canada.
    seychelles = get_destination("NE-1159151191")
    wrong_place = _tool({
        "city_name": "Victoria", "country": "Canada", "lat": 48.43, "lng": -123.37,
        "photos": ["wrong-place-image"],
    })
    with patch("src.core.destination_places.get_cityinfo", wrong_place):
        place = lookup_destination_place(seychelles)
    wrong_place.invoke.assert_called_once_with({"city_name": "Victoria", "region_hint": "Seychelles"})
    assert place["outcome"] == "COUNTRY_MISMATCH"
    assert place["photos"] == []
    assert place["latitude"] is None


def test_media_enrichment_keeps_catalog_coordinates_and_records_the_lookup():
    seychelles = get_destination("NE-1159151191")
    lookup = _tool({
        "city_name": "Victoria", "country": "Seychelles", "lat": -4.62, "lng": 55.45,
        "photos": ["https://maps.example/victoria.jpg"],
    })
    suggestion = {
        "destination_id": seychelles.destination_id,
        "city_name": seychelles.destination,
        "country_name": seychelles.country,
        "latitude": seychelles.latitude,
        "longitude": seychelles.longitude,
        "evidence": {"destination_source_url": seychelles.destination_source_url},
    }
    with patch("src.core.destination_places.get_cityinfo", lookup):
        enriched = _enrich_destination_media(suggestion)
    assert enriched["city_image"] == ["https://maps.example/victoria.jpg"]
    assert enriched["latitude"] == seychelles.latitude
    assert enriched["evidence"]["coordinates_source"] == "catalog"
    assert enriched["evidence"]["place_lookup"]["outcome"] == "MATCHED"
    assert enriched["evidence"]["destination_source_url"] == seychelles.destination_source_url


def test_missing_catalog_coordinates_come_from_a_country_checked_lookup():
    tucson = get_destination("US-TUC")
    assert tucson.latitude is None
    lookup = _tool({"city_name": "Tucson", "country": "United States", "lat": 32.22, "lng": -110.97, "photos": []})
    with patch("src.core.destination_places.get_cityinfo", lookup):
        coordinates = lookup_missing_coordinates([tucson, get_destination("NE-1159151609")])
    assert coordinates == {"US-TUC": (32.22, -110.97)}  # Tokyo already has catalog coordinates.


def test_successful_lookups_are_cached_but_failures_are_retried():
    tucson = get_destination("US-TUC")
    failing = _tool({"error": "timeout"})
    with patch("src.core.destination_places.get_cityinfo", failing):
        assert lookup_destination_place(tucson)["outcome"] == "UNVERIFIED"
        assert lookup_destination_place(tucson)["outcome"] == "UNVERIFIED"
    assert failing.invoke.call_count == 2

    working = _tool({"city_name": "Tucson", "country": "United States", "lat": 32.22, "lng": -110.97, "photos": []})
    with patch("src.core.destination_places.get_cityinfo", working):
        lookup_destination_place(tucson)
        lookup_destination_place(tucson)
    assert working.invoke.call_count == 1


def test_google_city_search_includes_region_hint():
    response = Mock(status_code=200)
    response.json.return_value = {
        "places": [{
            "displayName": {"text": "Victoria"},
            "addressComponents": [{
                "types": ["country"],
                "longText": "Canada",
            }],
            "location": {"latitude": 48.4284, "longitude": -123.3656},
            "photos": [],
        }]
    }
    with patch("src.tools.tools.requests.post", return_value=response) as post:
        result = get_cityinfo.invoke({
            "city_name": "Victoria",
            "region_hint": "Canada",
        })

    assert result["country"] == "Canada"
    assert post.call_args.kwargs["json"]["textQuery"] == "Victoria, Canada"
