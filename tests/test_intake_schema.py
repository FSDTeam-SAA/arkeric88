"""Validation of the 11-step Velari intake against the client's form definition."""

import json
from datetime import date, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.schemas.intake_schema import (
    ActivityRestriction,
    PreferredEnvironment,
    PreferredMoment,
    RecentFeeling,
    RestrictionSeverity,
    STORED_INTAKE_CONTEXT,
    TravelDistance,
    TravelIntakeRequest,
    TravelParty,
    TravelPeriod,
    TravelTiming,
    TripGoal,
    TripPace,
    TripPrompt,
)
from src.core import intake_mappings as mappings
from intake_fixtures import build_intake, exact_dates, with_restriction

FORM = json.loads(
    (Path(__file__).resolve().parents[1] / "app" / "schemas" / "velari_intake_form.json").read_text(encoding="utf-8")
)
STEPS = {step["key"]: step for step in FORM["steps"]}


def _options(step_key: str) -> dict:
    return {option["value"]: option["label"] for option in STEPS[step_key]["options"]}


def _conditional_options(step_key: str, index: int) -> dict:
    field = STEPS[step_key]["conditionalFields"][index]
    return {option["value"]: option["label"] for option in field["options"]}


# ==================== form contract ====================

@pytest.mark.parametrize("enum_cls, options, labels", [
    (RecentFeeling, _options("recent_feelings"), mappings.RECENT_FEELING_LABELS),
    (TripGoal, _options("trip_goals"), mappings.TRIP_GOAL_LABELS),
    (TripPrompt, _options("trip_prompt"), mappings.TRIP_PROMPT_LABELS),
    (PreferredMoment, _options("preferred_moments"), mappings.PREFERRED_MOMENT_LABELS),
    (PreferredEnvironment, _options("preferred_environments"), mappings.ENVIRONMENT_LABELS),
    (TripPace, _options("trip_pace"), mappings.TRIP_PACE_LABELS),
    (TravelParty, _options("travel_party"), mappings.TRAVEL_PARTY_LABELS),
    (ActivityRestriction, _options("activity_restrictions"), mappings.RESTRICTION_LABELS),
    (RestrictionSeverity, _conditional_options("activity_restrictions", 0), mappings.RESTRICTION_SEVERITY_LABELS),
    (TravelDistance, {o["value"]: o["label"] for o in STEPS["departure"]["fields"][1]["options"]}, mappings.TRAVEL_DISTANCE_LABELS),
    (TravelTiming, _options("travel_timing"), mappings.TRAVEL_TIMING_LABELS),
    (TravelPeriod, _conditional_options("travel_timing", 1), mappings.TRAVEL_PERIOD_LABELS),
])
def test_every_form_option_is_an_enum_value_with_the_form_label(enum_cls, options, labels):
    assert {member.value for member in enum_cls} == set(options)
    assert labels == options


def test_goal_definitions_match_the_form_cards():
    cards = {option["value"]: option["description"] for option in STEPS["trip_goals"]["options"]}
    assert mappings.TRIP_GOAL_DEFINITIONS == cards


def test_selection_limits_match_the_form():
    model_fields = TravelIntakeRequest.model_fields
    for key in ("recent_feelings", "trip_goals", "preferred_moments", "preferred_environments"):
        max_length = next(m.max_length for m in model_fields[key].metadata if hasattr(m, "max_length"))
        assert max_length == STEPS[key]["maxSelections"]


def test_party_defaults_match_the_form():
    for option in STEPS["travel_party"]["options"]:
        if "defaults" in option:
            intake = TravelIntakeRequest(**build_intake(travel_party=option["value"]))
            for field_name, value in option["defaults"].items():
                assert getattr(intake, field_name) == value


# ==================== valid payloads ====================

def test_minimal_payload_is_valid_and_fills_solo_defaults():
    intake = TravelIntakeRequest(**build_intake())
    assert (intake.party_adults, intake.party_children, intake.party_rooms) == (1, 0, 1)
    assert intake.activity_restrictions == []
    assert intake.budget_open_ended is False


def test_group_uses_form_defaults_when_counts_are_omitted():
    intake = TravelIntakeRequest(**build_intake(travel_party="group"))
    assert (intake.party_adults, intake.party_children, intake.party_rooms) == (1, 0, 1)


def test_top_of_budget_slider_is_open_ended():
    assert TravelIntakeRequest(**build_intake(budget_per_night=7000)).budget_open_ended is True


def test_flat_restriction_severity_keys_are_folded_into_one_map():
    payload = with_restriction(build_intake(), "no_long_drives", "must_avoid")
    payload = with_restriction(payload, "avoid_cold_weather", "prefer_avoid")
    intake = TravelIntakeRequest(**payload)
    assert intake.restriction_severity == {
        ActivityRestriction.no_long_drives: RestrictionSeverity.must_avoid,
        ActivityRestriction.avoid_cold_weather: RestrictionSeverity.prefer_avoid,
    }
    dumped = intake.model_dump(mode="json")["restriction_severity"]
    assert dumped == {"no_long_drives": "must_avoid", "avoid_cold_weather": "prefer_avoid"}


def test_severity_for_unselected_restriction_is_dropped():
    payload = build_intake(restriction_severity={"no_long_drives": "must_avoid"})
    assert TravelIntakeRequest(**payload).restriction_severity == {}


def test_hidden_conditional_fields_are_dropped_not_rejected():
    intake = TravelIntakeRequest(**build_intake(
        recent_feelings_other="stale text",
        trip_prompt_other="stale text",
        travel_period="july",
        check_in_date="2030-01-01",
    ))
    assert intake.recent_feelings_other is None
    assert intake.trip_prompt_other is None
    assert intake.travel_period is None
    assert intake.check_in_date is None


def test_exact_dates_derive_nights_when_omitted():
    payload = build_intake(**exact_dates(nights=4))
    payload.pop("trip_nights")
    assert TravelIntakeRequest(**payload).trip_nights == 4


def test_stored_intake_with_past_check_in_can_be_reread():
    past = date.today() - timedelta(days=3)
    payload = build_intake(**exact_dates(start=past, nights=5))
    with pytest.raises(ValidationError):
        TravelIntakeRequest.model_validate(payload)
    intake = TravelIntakeRequest.model_validate(payload, context=STORED_INTAKE_CONTEXT)
    assert intake.check_in_date == past


# ==================== rejected payloads ====================

@pytest.mark.parametrize("overrides, message", [
    ({"trip_goals": ["restoration", "connection", "growth"]}, "at most 2"),
    ({"recent_feelings": ["curious", "energized", "disconnected"]}, "at most 2"),
    ({"preferred_moments": ["food_drinks", "spa_wellness", "quiet_privacy", "music_nightlife"]}, "at most 3"),
    ({"preferred_environments": ["coast", "mountains", "desert"]}, "at most 2"),
    ({"trip_goals": []}, "at least 1"),
    ({"trip_goals": ["growth", "growth"]}, "duplicates"),
    ({"preferred_environments": ["surprise_me", "coast"]}, "surprise_me"),
    ({"recent_feelings": ["something_else"]}, "recent_feelings_other"),
    ({"recent_feelings": ["something_else"], "recent_feelings_other": "   "}, "recent_feelings_other"),
    ({"trip_prompt": "something_else"}, "trip_prompt_other"),
    ({"activity_restrictions": ["no_long_drives"]}, "no_long_drives"),
    ({"party_adults": 2}, "solo"),
    ({"travel_party": "family", "party_adults": 1, "party_children": 0, "party_rooms": 2}, "party_rooms"),
    ({"travel_party": "family", "party_adults": 2, "party_children": 2, "party_child_ages": [5]}, "one age per child"),
    ({"travel_party": "family", "party_adults": 2, "party_children": 1, "party_child_ages": [19]}, "between 0 and 17"),
    ({"departure_location": "   "}, "departure_location"),
    ({"departure_latitude": 38.7}, "together"),
    ({"travel_timing": "month_season"}, "travel_period"),
    ({"trip_nights": None}, "trip_nights"),
    ({"trip_nights": 0}, "greater than or equal to 1"),
    ({"budget_per_night": 99}, "greater than or equal to 100"),
    ({"budget_per_night": 7001}, "less than or equal to 7000"),
    ({"currency": "EUR"}, "USD"),
    ({"archetype": "seeker"}, "Extra inputs are not permitted"),
])
def test_invalid_payloads_are_rejected(overrides, message):
    with pytest.raises(ValidationError) as error:
        TravelIntakeRequest(**build_intake(**overrides))
    assert message in str(error.value)


@pytest.mark.parametrize("overrides, message", [
    ({"travel_timing": "exact_dates", "check_in_date": "2031-01-10"}, "check_out_date"),
    ({**exact_dates(nights=3), "check_out_date": exact_dates(nights=0)["check_in_date"]}, "after check_in_date"),
    ({**exact_dates(nights=5), "trip_nights": 7}, "does not match"),
    (exact_dates(start=date.today() - timedelta(days=1)), "past"),
])
def test_invalid_exact_dates_are_rejected(overrides, message):
    with pytest.raises(ValidationError) as error:
        TravelIntakeRequest(**build_intake(**overrides))
    assert message in str(error.value)


def test_unknown_restriction_severity_key_is_rejected():
    payload = build_intake(activity_restrictions=["other"], restriction_severity_other="sometimes")
    with pytest.raises(ValidationError):
        TravelIntakeRequest(**payload)
