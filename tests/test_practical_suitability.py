"""
Fixes from the customer test review (October 2026), using the reviewer's own
answers: travel effort against trip length, preliminary budget fit from the
price estimates, no near-duplicate destinations, card text that says each
thing once, both selected feelings, accessibility details, the flexible-date
window, a departure the map can't match, and LiteAPI hotel matching.
"""

from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from app.schemas.intake_schema import TravelIntakeRequest
from src.core import destination_matching
from src.core.destination_catalog import get_destination
from src.core.destination_matching import (
    Exclusion,
    MatchResult,
    build_no_valid_result,
    build_suggestion,
    match_label,
    rank_destinations,
    select_diverse,
    suggested_min_nights,
)
from src.core.geography import country_key, country_spellings
from src.core.hotel_rates import _branded_name_match
from src.core.origin import Origin, matches_typed_place, resolve_origin
from src.core.price_estimates import (
    FITS,
    LIKELY_ABOVE,
    MAY_STRETCH,
    UNKNOWN,
    PriceEstimate,
    budget_fit,
    parse_price_rows,
)
from src.core.trip_profile import build_trip_profile
from src.tools.tools import _extract_photo_urls
from intake_fixtures import build_intake, exact_dates

AUSTIN = Origin(
    "GEOCODED", 30.2672, -97.7431, "United States", "north america", "2026-10-30T00:00:00+00:00",
    input_text="Austin, Texas", normalized_name="Austin, TX, USA",
)
# Map-lookup coordinates for catalog rows that have none.
LOOKED_UP = {
    "ID-UBU": (-8.51, 115.26), "IN-RIS": (30.09, 78.27), "MV-BAA": (5.2, 73.0), "US-SED": (34.87, -111.76),
    "US-TUC": (32.22, -110.97), "MX-OAX": (17.07, -96.72), "MX-VDG": (32.09, -116.57), "CO-COF": (4.81, -75.69),
    "MX-SMA": (20.91, -100.74), "JP-KIN": (35.63, 134.81),
}

# The reviewer's Test 1.
TEST_1 = dict(
    recent_feelings=["stretched_thin"], trip_goals=["restoration", "reflection"], trip_prompt="need_a_break",
    preferred_moments=["nature_wildlife", "quiet_privacy", "spa_wellness"],
    preferred_environments=["mountains", "forest_jungle"], trip_pace="one_highlight", travel_party="couple",
    activity_restrictions=["no_intense_activity"], restriction_severity={"no_intense_activity": "prefer_avoid"},
    departure_location="Austin, Texas", travel_distance="anywhere", travel_timing="flexible",
    trip_nights=5, budget_per_night=300,
)


def _request(**overrides) -> TravelIntakeRequest:
    return TravelIntakeRequest(**build_intake(**{**TEST_1, **overrides}))


def _profile(**overrides):
    return build_trip_profile(_request(**overrides))


def _ranked(profile, origin=AUSTIN) -> dict:
    return {item.destination_id: item for item in rank_destinations(profile, origin, LOOKED_UP).ranked}


def _shortlist(profile, origin=AUSTIN) -> list:
    return select_diverse(rank_destinations(profile, origin, LOOKED_UP).ranked)


# ==================== travel effort ====================

@pytest.mark.parametrize("km, nights", [(1000, 2), (3000, 4), (7000, 6), (15000, 9)])
def test_suggested_minimum_nights_grow_with_distance(km, nights):
    assert suggested_min_nights(km) == nights


def test_long_journey_on_a_short_trip_ranks_lower_with_a_reason():
    ranked = _ranked(_profile())
    ubud, sedona = ranked["ID-UBU"], ranked["US-SED"]
    assert ubud.breakdown["travel_effort_penalty"] < 0
    assert ubud.breakdown["travel_distance"] < 10
    assert ubud.total_score < 100
    assert any("A long journey for a 5-night trip" in note and "from Austin" in note for note in ubud.tradeoffs)
    assert sedona.breakdown["travel_effort_penalty"] == 0 and sedona.breakdown["travel_distance"] == 10


def test_long_journey_is_not_penalised_on_a_long_enough_trip():
    ubud = _ranked(_profile(trip_nights=10))["ID-UBU"]
    assert ubud.breakdown["travel_effort_penalty"] == 0
    assert not any("long journey" in note for note in ubud.tradeoffs)


def test_unknown_departure_gets_no_travel_effort_claims():
    profile = _profile(departure_location=None, travel_distance=None)
    ubud = _ranked(profile, origin=None)["ID-UBU"]
    assert ubud.breakdown["travel_effort_penalty"] == 0 and ubud.total_score == 100


def test_reviewer_test_1_shortlist_includes_a_realistic_option():
    # Before: Ubud, Rishikesh and Baa Atoll, all 13,000+ km away for 5 nights.
    shortlist = _shortlist(_profile())
    assert any(item.distance.straight_line_km <= 4000 for item in shortlist)
    nearby = next(item for item in shortlist if item.distance.straight_line_km <= 4000)
    suggestion = build_suggestion(nearby, _profile(), AUSTIN)
    assert any("from Austin" in reason and "comfortable distance for 5 nights" in reason for reason in suggestion["match_reasons"])


# ==================== budget fit from price estimates ====================

ESTIMATES = {
    "MV-BAA": PriceEstimate(900, 1400, 2500),
    "ID-UBU": PriceEstimate(80, 180, 600),
    "IN-RIS": PriceEstimate(250, 350, 500),
}


@pytest.fixture
def priced(monkeypatch):
    monkeypatch.setattr(destination_matching, "price_estimate", ESTIMATES.get)
    monkeypatch.setattr(destination_matching, "load_price_estimates", lambda: ESTIMATES)


@pytest.mark.parametrize("estimate, budget, open_ended, fit", [
    (PriceEstimate(80, 180, 600), 300, False, FITS),
    (PriceEstimate(250, 350, 500), 300, False, MAY_STRETCH),
    (PriceEstimate(900, 1400, 2500), 300, False, LIKELY_ABOVE),
    (PriceEstimate(900, 1400, 2500), 7000, True, FITS),
    (None, 300, False, UNKNOWN),
])
def test_budget_fit(estimate, budget, open_ended, fit):
    assert budget_fit(estimate, budget, open_ended) == fit


def test_estimates_rank_but_never_exclude(priced):
    ranked = _ranked(_profile())
    baa, rishikesh, ubud = ranked["MV-BAA"], ranked["IN-RIS"], ranked["ID-UBU"]
    assert baa.budget_fit == LIKELY_ABOVE and baa.breakdown["budget_penalty"] == -10
    assert rishikesh.budget_fit == MAY_STRETCH and rishikesh.breakdown["budget_penalty"] == -5
    assert ubud.budget_fit == FITS and ubud.breakdown["budget_penalty"] == 0
    assert any("Likely above your $300 nightly budget" in note and "not a quote" in note for note in baa.tradeoffs)


def test_estimate_is_shown_as_an_estimate_never_a_rate(priced):
    profile = _profile(travel_party="family", party_adults=2, party_children=0, party_rooms=2)
    baa = _ranked(profile)["MV-BAA"]
    suggestion = build_suggestion(baa, profile, AUSTIN)
    verification = suggestion["verification"]
    assert verification["budget_status"] == LIKELY_ABOVE
    assert verification["price_claim"] == "ESTIMATE"
    assert verification["price_estimate"]["estimated_stay_total_usd"] == 1400 * 2 * 5
    assert any(fact.startswith("Estimated stays: about $900-$2,500") and "not a quote" in fact
               for fact in suggestion["unresolved_facts"])


def test_luxury_budget_is_never_penalised(priced):
    baa = _ranked(_profile(budget_per_night=7000))["MV-BAA"]
    assert baa.budget_fit == FITS and baa.breakdown["budget_penalty"] == 0


def test_price_sheet_columns_are_found_by_name():
    rows = [
        {"destination_id": "ID-UBU", "Low nightly (USD)": "$80", "Typical nightly (USD)": "180",
         "High nightly (USD)": "600", "Notes": "two adults"},
        {"destination_id": "IN-RIS", "Low nightly (USD)": "", "Typical nightly (USD)": "350",
         "High nightly (USD)": "500", "Notes": ""},
    ]
    assert parse_price_rows(rows) == {"ID-UBU": PriceEstimate(80, 180, 600)}
    with pytest.raises(ValueError):
        parse_price_rows([{"destination_id": "ID-UBU", "price": "100"}])


# ==================== no near-duplicates ====================

def test_nearby_allows_two_clearly_different_places_in_one_country():
    shortlist = _shortlist(_profile(travel_distance="nearby"))
    ids = {item.destination_id for item in shortlist}
    assert len(shortlist) == 3
    # Tucson and Sedona are both Arizona desert: never shown together.
    assert not {"US-TUC", "US-SED"} <= ids
    countries = [country_key(item.destination.country) for item in shortlist]
    assert max(countries.count(country) for country in countries) <= 2


def test_a_country_is_never_shown_three_times():
    profile = _profile(travel_distance="nearby", preferred_environments=["surprise_me"])
    for size in (3, 5):
        countries = [country_key(item.destination.country) for item in select_diverse(
            rank_destinations(profile, AUSTIN, LOOKED_UP).ranked, limit=size)]
        assert max(countries.count(country) for country in countries) <= 2


# ==================== card text ====================

def test_card_sections_each_say_something_different():
    profile = _profile()
    for item in _shortlist(profile):
        suggestion = build_suggestion(item, profile, AUSTIN)
        description = suggestion["description"]
        assert description and "Room to feel" not in description
        assert not any(reason in description or description in reason for reason in suggestion["match_reasons"])
        assert all("Room to feel" not in reason for reason in suggestion["match_reasons"])
        assert suggestion["primary_feeling"] == "Restored and reflective"
        assert suggestion["match_label"] == match_label(item.total_score)


def test_ubud_card_names_specific_experiences_for_the_selected_goals():
    profile = _profile(departure_location=None, travel_distance=None)
    ubud = _ranked(profile, origin=None)["ID-UBU"]
    suggestion = build_suggestion(ubud, profile, None)
    assert suggestion["description"] == "Ubud is known for art, nature, food and yoga, in a town and jungle setting."
    assert suggestion["match_reasons"][0] == "For restoration and reflection: nature, yoga and jungle."
    assert "spa and wellness (yoga)" in suggestion["match_reasons"][1]


def test_both_goals_are_shown_and_a_missing_one_is_explained():
    profile = _profile(recent_feelings=["curious"], trip_goals=["discovery", "inspiration"], trip_prompt="curious_to_explore")
    suggestion = build_suggestion(_ranked(profile)["CO-COF"], profile, AUSTIN)
    assert suggestion["primary_feeling"] == "Curious and inspired"
    assert suggestion["match_reasons"][0] == "For discovery: coffee and culture."
    assert "Offers less for inspiration than for discovery." in suggestion["tradeoffs"]


def test_one_country_is_spelled_one_way_on_every_card():
    # The catalog writes both "United States" and "United States of America"; a
    # Nearby shortlist from Austin showed both side by side.
    assert get_destination("US-TUC").country == "United States"
    shortlist = _shortlist(_profile(travel_distance="nearby"))
    countries = [build_suggestion(item, _profile(travel_distance="nearby"), AUSTIN)["country_name"] for item in shortlist]
    assert "United States of America" not in countries


def test_setting_the_guest_did_not_pick_is_stated_as_a_tradeoff():
    profile = _profile()
    sedona = build_suggestion(_ranked(profile)["US-SED"], profile, AUSTIN)
    assert "Its desert setting isn't one you picked (mountains, forest or jungle), so it ranks lower for setting." in sedona["tradeoffs"]
    ubud = build_suggestion(_ranked(profile)["ID-UBU"], profile, AUSTIN)
    assert not any("isn't one you picked" in line for line in ubud["tradeoffs"])


def test_reasons_never_repeat_an_introduction_built_from_the_rationale():
    # Location-enriched rows often have no experience tag backing the goal; the
    # fallback used to be the rationale sentence the introduction already shows.
    profile = _profile(trip_goals=["reflection"], departure_location=None, travel_distance=None)
    tuzla = next(d for d in destination_matching.load_destination_candidates() if d.destination == "Tuzla")
    item = _ranked(profile, origin=None)[tuzla.destination_id]
    suggestion = build_suggestion(item, profile, None)
    assert suggestion["description"].startswith("Tuzla offers")
    assert not any(suggestion["description"].rstrip(".") in reason for reason in suggestion["match_reasons"])


def test_photos_are_requested_at_the_largest_size_google_allows():
    urls = _extract_photo_urls({"photos": [{"name": "places/abc/photos/def"}]}, "test-key")
    assert urls == [
        "https://places.googleapis.com/v1/places/abc/photos/def/media"
        "?key=test-key&maxHeightPx=4800&maxWidthPx=4800"
    ]


def test_any_country_spelled_two_ways_gets_one_name():
    # Not only the United States: a future catalogue update may mix other spellings.
    spellings = country_spellings(["UK", "United Kingdom", "UK", "The Bahamas", "Bahamas", "Bahamas", "Peru"])
    assert spellings["united kingdom"] == "United Kingdom"  # preferred name wins over the more common "UK"
    assert spellings["bahamas"] == "Bahamas"  # otherwise the most common spelling
    assert spellings["peru"] == "Peru"


@pytest.mark.parametrize("phrase, article", [
    ("island", "an"), ("alpine lake", "an"), ("urban / regional gateway", "an"), ("desert", "a"),
    ("university town", "a"), ("one-street village", "a"), ("hour from the coast", "an"),
])
def test_article(phrase, article):
    assert destination_matching._article(phrase) == article


def test_introduction_uses_the_right_article():
    profile = _profile(departure_location=None, travel_distance=None)
    baa = build_suggestion(_ranked(profile, origin=None)["MV-BAA"], profile, None)
    assert baa["description"].endswith("in an island setting.")


def test_goal_click_order_never_changes_the_label_or_results():
    forward = _profile(trip_goals=["restoration", "reflection"])
    reverse = _profile(trip_goals=["reflection", "restoration"])
    assert forward.feeling_label == reverse.feeling_label == "Restored and reflective"
    assert [item.destination_id for item in _shortlist(forward)] == [item.destination_id for item in _shortlist(reverse)]
    assert _profile(trip_goals=["reflection", "restoration"], primary_trip_goal="reflection").feeling_label == (
        "Reflective and restored"
    )


def test_editor_shorthand_is_not_shown_to_guests():
    assert get_destination("MX-VDG").specific_tradeoffs == "Driving and wine focus may not suit everyone"
    assert "travelers seeking silence" in get_destination("MA-MAR").specific_tradeoffs


@pytest.mark.parametrize("score, label", [(92, "Excellent match"), (72, "Strong match"), (60, "Good match"), (30, "Possible match")])
def test_match_label(score, label):
    assert match_label(score) == label


# ==================== accessibility ====================

MOBILITY = dict(
    activity_restrictions=["mobility_accessibility"],
    restriction_severity={"mobility_accessibility": "must_avoid"},
)


def test_accessibility_details_are_kept_and_used_on_the_card():
    profile = _profile(**MOBILITY, accessibility_needs=["step_free_access", "limited_walking"])
    assert "Must avoid: Mobility or accessibility (Step-free access, Limited walking)" in profile.restriction_phrases()
    assert not any(question["field"] == "accessibility_needs" for question in profile.clarifications)
    ubud = _ranked(profile)["ID-UBU"]
    facts = build_suggestion(ubud, profile, AUSTIN)["unresolved_facts"]
    assert any(fact.startswith("Mobility or accessibility (step-free access, limited walking):") for fact in facts)


def test_missing_accessibility_details_are_asked_for_without_blocking():
    profile = _profile(**MOBILITY)
    question = next(question for question in profile.clarifications if question["field"] == "accessibility_needs")
    assert question["blocking"] is False
    assert _shortlist(profile)


def test_accessibility_details_are_dropped_without_the_mobility_restriction():
    request = _request(accessibility_needs=["wheelchair_user"], accessibility_needs_other="ramp")
    assert request.accessibility_needs == [] and request.accessibility_needs_other is None


def test_a_mandatory_need_is_never_suggested_for_downgrade():
    profile = _profile(**MOBILITY)
    result = MatchResult(
        ranked=[], exclusions=[Exclusion("ID-UBU", "must_avoid:mobility_accessibility", "")],
        total_candidate_count=1, data_gaps=[],
    )
    question = build_no_valid_result(result, profile)["question"]
    assert "prefer to avoid" not in question.lower()
    assert "requirement stays as it is" in question


# ==================== flexible dates ====================

def test_flexible_window_is_kept_separately_from_the_number_of_nights():
    request = _request(earliest_check_in="2031-12-01", latest_check_out="2031-12-20")
    assert request.travel_timing.value == "flexible" and request.trip_nights == 5
    profile = build_trip_profile(request)
    assert profile.travel_months == [12]
    assert profile.guest_context()["travel_window"] == {"earliest_check_in": "2031-12-01", "latest_check_out": "2031-12-20"}
    assert profile.timing_phrase() == "Flexible dates, 5 nights, available 2031-12-01 to 2031-12-20"


def test_exact_dates_drop_a_stale_window():
    request = _request(**exact_dates(nights=5), earliest_check_in="2031-12-01", latest_check_out="2031-12-20")
    assert request.earliest_check_in is None and request.latest_check_out is None


def test_window_must_run_forwards():
    with pytest.raises(ValidationError):
        _request(earliest_check_in="2031-12-20", latest_check_out="2031-12-01")


# ==================== departure the map can't match ====================

@pytest.mark.parametrize("typed, found, ok", [
    ("Testville, TX", {"city_name": "Stephenville"}, False),
    ("Austin, Texas", {"city_name": "Austin"}, True),
    ("Austn, TX", {"city_name": "Austin"}, True),
    ("Ho Chi Minh", {"city_name": "Ho Chi Minh City"}, True),
    ("Sao Paulo", {"city_name": "São Paulo"}, True),
    ("AUS", {"city_name": "Austin-Bergstrom International Airport"}, True),
])
def test_map_match_must_be_the_place_typed(typed, found, ok):
    assert matches_typed_place(typed, found) is ok


def test_a_different_town_is_not_used_as_the_departure(monkeypatch):
    lookup = MagicMock()
    lookup.invoke.return_value = {
        "city_name": "Stephenville", "formatted_address": "Stephenville, TX 76401, USA",
        "country": "United States", "country_code": "US", "lat": 32.22, "lng": -98.2,
    }
    monkeypatch.setattr("src.core.origin.get_cityinfo", lookup)
    origin = resolve_origin("Testville, TX")
    assert origin.status == "UNVERIFIED" and not origin.has_coordinates
    assert origin.input_text == "Testville, TX"
    assert "Stephenville" in origin.detail
    # The state was real, so "Nearby" still stays in North America.
    assert origin.region == "north america"
    shortlist = select_diverse(rank_destinations(_profile(departure_location="Testville, TX", travel_distance="nearby"), origin).ranked)
    assert shortlist and all(item.destination.world_region.lower() == "north america" for item in shortlist)


# ==================== LiteAPI hotel matching ====================

def test_supplier_name_with_a_brand_suffix_matches_the_same_spot():
    google = {"name": "The Driskill"}
    branded = (0.4, 40.0, {"name": "The Driskill, in The Unbound Collection by Hyatt"}, "lp1e099")
    assert _branded_name_match(google, [branded]) == branded
    # Same name 800 m away, or a name too short to be distinctive: no match.
    assert _branded_name_match(google, [(0.4, 800.0, branded[2], "lp1e099")]) is None
    assert _branded_name_match({"name": "Inn"}, [(0.4, 10.0, {"name": "Inn at the Park"}, "x")]) is None
