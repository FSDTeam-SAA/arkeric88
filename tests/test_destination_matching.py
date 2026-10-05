"""
Deterministic destination matching against data/3. IMPORT_RULES.csv:
hard exclusions before ranking, prefer-avoid as penalty, grounded fit,
travel-distance screening, diverse 2-3 options and "no valid result".
"""

from dataclasses import replace

import pytest

from app.schemas.intake_schema import TravelIntakeRequest
from src.core import destination_matching
from src.core.destination_catalog import get_destination, load_destination_candidates, load_discovery_backlog
from src.core.destination_matching import (
    build_no_valid_result,
    build_suggestion,
    check_restriction,
    rank_destinations,
    select_diverse,
)
from src.core.guest_text import has_internal_wording
from src.core.origin import Origin
from src.core.trip_profile import build_trip_profile
from intake_fixtures import build_intake, exact_dates, with_restriction

LISBON = Origin("GUEST_SUPPLIED", 38.7223, -9.1393, "Portugal", "europe", "2026-09-28T00:00:00+00:00")


def _profile(payload: dict):
    return build_trip_profile(TravelIntakeRequest(**payload))


def _ids(items) -> set:
    return {item.destination_id for item in items}


def _excluded_ids(result, reason_prefix: str) -> set:
    return {e.destination_id for e in result.exclusions if e.reason.startswith(reason_prefix)}


# ==================== hard exclusions ====================

def test_must_avoid_conflict_is_excluded_before_ranking():
    # Wadi Rum's catalog tradeoff: "Walking, heat and transport require planning".
    profile = _profile(with_restriction(build_intake(), "avoid_extreme_heat", "must_avoid"))
    result = rank_destinations(profile, None)
    assert "JO-WAD" in _excluded_ids(result, "must_avoid:avoid_extreme_heat")
    assert "JO-WAD" not in _ids(result.ranked)


def test_prefer_avoid_is_a_penalty_with_an_explained_tradeoff():
    base = build_intake(trip_goals=["restoration"], preferred_moments=["spa_wellness"])
    plain = {item.destination_id: item for item in rank_destinations(_profile(base), None).ranked}
    penalized = {
        item.destination_id: item
        for item in rank_destinations(_profile(with_restriction(base, "no_long_drives", "prefer_avoid")), None).ranked
    }
    # Baa Atoll: "Seaplane transfers and resort exclusivity can increase cost".
    assert "MV-BAA" in penalized
    assert penalized["MV-BAA"].total_score < plain["MV-BAA"].total_score
    assert penalized["MV-BAA"].breakdown["prefer_avoid_penalty"] < 0
    assert any("No long drives" in tradeoff for tradeoff in penalized["MV-BAA"].tradeoffs)


def test_seasonal_heat_concern_clears_outside_local_summer():
    # Tucson: "Summer heat limits midday outings" (Northern hemisphere).
    tucson = get_destination("US-TUC")
    december = _profile(build_intake(travel_timing="month_season", travel_period="december"))
    july = _profile(build_intake(travel_timing="month_season", travel_period="july"))
    assert check_restriction("avoid_extreme_heat", "must_avoid", tucson, december).status == "seasonal_outside_dates"
    assert check_restriction("avoid_extreme_heat", "must_avoid", tucson, july).status == "conflict"


def test_seasonal_check_flips_for_the_southern_hemisphere():
    # Brisbane: "Summer humidity and side-trip distances" (Southern hemisphere).
    brisbane = get_destination("NE-1159151169")
    july = _profile(build_intake(travel_timing="month_season", travel_period="july"))
    january = _profile(build_intake(travel_timing="month_season", travel_period="january"))
    assert check_restriction("avoid_extreme_heat", "must_avoid", brisbane, july).status == "seasonal_outside_dates"
    assert check_restriction("avoid_extreme_heat", "must_avoid", brisbane, january).status == "conflict"


def test_seasonal_concern_is_not_cleared_without_known_dates():
    tucson = get_destination("US-TUC")
    flexible = _profile(build_intake())
    assert check_restriction("avoid_extreme_heat", "must_avoid", tucson, flexible).status == "conflict"


def test_unknown_high_impact_must_avoid_is_not_a_pass():
    profile = _profile(with_restriction(build_intake(), "mobility_accessibility", "must_avoid"))
    result = rank_destinations(profile, None)
    assert result.ranked == []
    no_result = build_no_valid_result(result, profile)
    assert no_result["relaxed_automatically"] is False
    constraints = {item["constraint"] for item in no_result["blocking_constraints"]}
    assert "unverified_high_impact:mobility_accessibility" in constraints
    assert "must_avoid:mobility_accessibility" in constraints
    assert "prefer to avoid" in no_result["question"]


def test_prefer_avoid_high_impact_keeps_options_but_flags_them_unverified():
    profile = _profile(with_restriction(build_intake(), "mobility_accessibility", "prefer_avoid"))
    result = rank_destinations(profile, None)
    assert result.ranked
    suggestion = build_suggestion(result.ranked[0], profile, None)
    assert any(fact.startswith("Mobility or accessibility:") for fact in suggestion["unresolved_facts"])


def test_food_dietary_is_carried_to_the_itinerary_stage_not_guessed():
    profile = _profile(with_restriction(build_intake(), "food_dietary", "must_avoid"))
    result = rank_destinations(profile, None)
    assert result.ranked
    check = result.ranked[0].restriction_checks[0]
    assert check.status == "itinerary_stage"
    assert any(c["field"] == "restriction_notes" for c in profile.clarifications)


def test_other_must_avoid_asks_for_clarification():
    profile = _profile(with_restriction(build_intake(), "other", "must_avoid"))
    assert rank_destinations(profile, None).ranked
    assert any("Other" in c["question"] for c in profile.clarifications)


def test_verified_price_above_budget_is_excluded_but_open_ended_is_not(monkeypatch):
    catalog = tuple(
        replace(destination, verified_nightly_usd=900.0) if destination.destination_id == "MV-BAA" else destination
        for destination in load_destination_candidates()
    )
    monkeypatch.setattr(destination_matching, "load_destination_candidates", lambda: catalog)
    capped = rank_destinations(_profile(build_intake(budget_per_night=400)), None)
    assert "MV-BAA" in _excluded_ids(capped, "budget")
    open_ended = rank_destinations(_profile(build_intake(budget_per_night=7000)), None)
    assert "MV-BAA" in _ids(open_ended.ranked)


def test_destination_matching_no_goal_and_no_moment_is_not_grounded():
    profile = _profile(build_intake(trip_goals=["growth"], preferred_moments=["music_nightlife"]))
    result = rank_destinations(profile, None)
    for item in result.ranked:
        assert item.matched_goals or item.matched_moments
    assert _excluded_ids(result, "no_grounded_fit")


# ==================== travel distance ====================

def test_nearby_excludes_destinations_beyond_the_straight_line_limit():
    profile = _profile(build_intake(travel_distance="nearby"))
    result = rank_destinations(profile, LISBON)
    for item in result.ranked:
        if item.distance.straight_line_km is not None:
            assert item.distance.straight_line_km <= 1500
    assert "NE-1159151609" in _excluded_ids(result, "travel_distance")  # Tokyo


def test_nearby_excludes_unmeasurable_destinations_in_another_region():
    profile = _profile(build_intake(travel_distance="nearby"))
    result = rank_destinations(profile, LISBON)
    assert "MV-BAA" in _excluded_ids(result, "travel_distance")  # Asia, no coordinates


def test_looked_up_coordinates_are_used_for_the_distance_check():
    profile = _profile(build_intake(travel_distance="nearby"))
    # Douro Valley has no catalog coordinates; a lookup places it ~300 km away.
    result = rank_destinations(profile, LISBON, {"PT-DOU": (41.16, -7.79)})
    douro = next(item for item in result.ranked if item.destination_id == "PT-DOU")
    assert douro.distance.status == "within_straight_line_limit"
    assert 200 < douro.distance.straight_line_km < 400


def test_unlocated_origin_skips_distance_screening_and_says_so():
    unverified = Origin("UNVERIFIED", None, None, None, None, "2026-09-28T00:00:00+00:00")
    profile = _profile(build_intake(travel_distance="nearby"))
    result = rank_destinations(profile, unverified)
    assert not _excluded_ids(result, "travel_distance")
    assert any("departure point could not be located" in gap for gap in result.data_gaps)


def test_anywhere_scores_full_travel_distance():
    result = rank_destinations(_profile(build_intake()), None)
    assert all(item.breakdown["travel_distance"] == 10 for item in result.ranked)


# ==================== scoring and selection ====================

def test_ranking_is_deterministic():
    profile = _profile(build_intake())
    first = [(i.destination_id, i.total_score) for i in rank_destinations(profile, None).ranked]
    second = [(i.destination_id, i.total_score) for i in rank_destinations(profile, None).ranked]
    assert first == second
    scores = [score for _, score in first]
    assert scores == sorted(scores, reverse=True)


def test_surprise_me_gives_full_setting_credit():
    result = rank_destinations(_profile(build_intake(preferred_environments=["surprise_me"])), None)
    assert all(item.breakdown["settings"] == 15 for item in result.ranked)


def test_select_diverse_returns_up_to_three_distinct_countries_in_score_order():
    result = rank_destinations(_profile(build_intake()), None)
    selected = select_diverse(result.ranked)
    assert 2 <= len(selected) <= 3
    assert len({item.destination.country for item in selected}) == len(selected)
    assert [i.total_score for i in selected] == sorted((i.total_score for i in selected), reverse=True)


def test_select_diverse_skips_excluded_ids():
    result = rank_destinations(_profile(build_intake()), None)
    first = select_diverse(result.ranked)
    second = select_diverse(result.ranked, exclude_ids=_ids(first))
    assert not _ids(first) & _ids(second)


def test_backlog_destinations_are_never_ranked():
    result = rank_destinations(_profile(build_intake(preferred_environments=["surprise_me"])), None)
    backlog_ids = {row["destination_id"] for row in load_discovery_backlog()}
    assert not _ids(result.ranked) & backlog_ids
    assert result.total_candidate_count == 72


def test_goal_with_no_catalog_coverage_is_reported_as_a_data_gap():
    result = rank_destinations(_profile(build_intake(trip_goals=["celebration"])), None)
    assert result.ranked
    assert any("Celebration" in gap for gap in result.data_gaps)


def test_budget_gap_is_reported_while_no_prices_are_verified():
    result = rank_destinations(_profile(build_intake()), None)
    assert any("verified nightly price" in gap for gap in result.data_gaps)


# ==================== explanation ====================

def test_suggestion_ties_reasons_to_guest_labels_and_states_what_is_unverified():
    profile = _profile(build_intake(**exact_dates(nights=5)))
    item = select_diverse(rank_destinations(profile, None).ranked)[0]
    suggestion = build_suggestion(item, profile, None)
    assert "restored" in suggestion["match_reasons"][0]
    assert suggestion["primary_feeling"] == "Restored"
    verification = suggestion["verification"]
    assert verification["candidate_status"] == "CANDIDATE_ONLY"
    assert verification["verified_nightly_usd"] is None
    assert verification["budget_status"] == "UNKNOWN"
    assert verification["price_claim"] == "NONE"
    assert verification["availability_claim"] == "NONE"
    # Review status is kept for the team, not shown to guests.
    assert any("Source review pending" in note for note in verification["internal_notes"])
    facts = " ".join(suggestion["unresolved_facts"])
    assert "Source review pending" not in facts
    assert "confirmed for your dates" in facts
    assert "entry, safety and accessibility guidance" in facts
    assert suggestion["evidence"]["destination_source_url"].startswith("http")
    assert suggestion["tradeoffs"], "every catalog row has a tradeoff to show"


def test_guest_facing_suggestion_text_has_no_catalog_language_or_repeats():
    profile = _profile(build_intake(**exact_dates(nights=5)))
    for item in select_diverse(rank_destinations(profile, None).ranked):
        suggestion = build_suggestion(item, profile, None)
        guest_text = [suggestion["description"], *suggestion["match_reasons"], *suggestion["tradeoffs"],
                      *suggestion["unresolved_facts"]]
        for text in guest_text:
            assert not has_internal_wording(text), text
            assert "You chose" not in text and "Check before booking" not in text, text
        lowered = [text.lower() for text in suggestion["tradeoffs"] + suggestion["unresolved_facts"]]
        assert len(lowered) == len(set(lowered)), "each note is shown once"


def test_flexible_timing_is_labelled_inspiration_only():
    profile = _profile(build_intake())
    item = rank_destinations(profile, None).ranked[0]
    facts = " ".join(build_suggestion(item, profile, None)["unresolved_facts"])
    assert "inspiration only" in facts


@pytest.mark.parametrize("field_name", ["recent_feelings_other", "trip_prompt_other"])
def test_private_free_text_never_reaches_the_profile(field_name):
    payload = build_intake(
        recent_feelings=["something_else"],
        recent_feelings_other="PRIVATE-FEELING",
        trip_prompt="something_else",
        trip_prompt_other="PRIVATE-REASON",
    )
    profile = _profile(payload)
    assert profile.has_private_context is True
    assert not hasattr(profile, field_name)
    rendered = repr(profile) + repr(profile.guest_context())
    assert "PRIVATE" not in rendered
