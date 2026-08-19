from unittest.mock import Mock, patch

from app.router.city_content_route import _enrich_city_suggestions
from src.core.geography import country_matches_region
from src.tools.tools import get_cityinfo


def test_known_country_region_validation():
    assert country_matches_region("Canada", "North America") is True
    assert country_matches_region("Bangladesh", "North America") is False
    assert country_matches_region("Brazil", "South America") is True
    assert country_matches_region("Bangladesh", "South America") is False


def test_ambiguous_city_uses_country_hint_and_rejects_wrong_map_location():
    suggestions = [{
        "city_name": "Victoria",
        "country_name": "Canada",
        "number_of_days": 3,
        "description": "Coastal restoration",
    }]

    with patch("app.router.city_content_route.get_cityinfo") as city_tool:
        city_tool.invoke.return_value = {
            "city_name": "Victoria",
            "country": "Bangladesh",
            "lat": 23.75,
            "lng": 90.37,
            "photos": ["wrong-place-image"],
        }
        enriched = _enrich_city_suggestions(suggestions, "North America")

    city_tool.invoke.assert_called_once_with({
        "city_name": "Victoria",
        "region_hint": "Canada",
    })
    assert enriched[0]["country_name"] == "Canada"
    assert enriched[0]["latitude"] is None
    assert enriched[0]["longitude"] is None
    assert enriched[0]["city_image"] == []


def test_city_is_removed_when_model_and_map_both_violate_region():
    suggestions = [{
        "city_name": "Victoria",
        "country_name": "Bangladesh",
        "number_of_days": 3,
        "description": "Wrong region",
    }]
    with patch("app.router.city_content_route.get_cityinfo") as city_tool:
        city_tool.invoke.return_value = {
            "city_name": "Victoria",
            "country": "Bangladesh",
            "lat": 23.75,
            "lng": 90.37,
            "photos": ["wrong-place-image"],
        }
        assert _enrich_city_suggestions(suggestions, "North America") == []


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
