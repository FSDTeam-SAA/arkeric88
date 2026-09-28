"""Integrity of the imported catalog and coverage of the intake mappings."""

import csv
import re

from app.schemas.intake_schema import (
    ActivityRestriction,
    PreferredEnvironment,
    PreferredMoment,
    TravelIntakeRequest,
    TripPace,
)
from src.core import intake_mappings as mappings
from src.core.destination_catalog import (
    CATALOG_SNAPSHOT_DATE,
    DATA_DIR,
    EXPECTED_BACKLOG_COUNT,
    EXPECTED_CANDIDATE_COUNT,
    get_catalog_version,
    get_destination,
    load_destination_candidates,
    load_discovery_backlog,
    parse_goals,
)


def _csv_rows(suffix: str) -> list:
    path = next(DATA_DIR.glob(f"*{suffix}"))
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


# ==================== catalog integrity ====================

def test_counts_match_import_rules_version_and_scope():
    rules = {row["Rule"]: row["Required behavior"] for row in _csv_rows("IMPORT_RULES.csv")}
    scope = rules["Version and scope"]
    assert CATALOG_SNAPSHOT_DATE in scope
    assert f"{EXPECTED_CANDIDATE_COUNT} editorial candidate records" in scope
    assert f"{EXPECTED_BACKLOG_COUNT} geographic backlog records" in scope
    assert len(load_destination_candidates()) == EXPECTED_CANDIDATE_COUNT
    assert len(load_discovery_backlog()) == EXPECTED_BACKLOG_COUNT
    assert get_catalog_version()["candidate_count"] == EXPECTED_CANDIDATE_COUNT


def test_candidate_ids_are_unique_and_never_in_the_backlog():
    candidate_ids = [destination.destination_id for destination in load_destination_candidates()]
    backlog_ids = {row["destination_id"] for row in load_discovery_backlog()}
    assert len(candidate_ids) == len(set(candidate_ids))
    assert not set(candidate_ids) & backlog_ids


def test_backlog_ids_never_resolve_to_a_destination():
    for row in load_discovery_backlog():
        assert row["import_status"] == "NOT_ELIGIBLE"
        assert get_destination(row["destination_id"]) is None


def test_every_candidate_stays_unverified_and_price_unknown():
    for destination in load_destination_candidates():
        assert destination.candidate_status == "CANDIDATE_ONLY"
        assert destination.source_review_status == "SOURCE_CLAIM_REVIEW_REQUIRED"
        assert destination.live_lodging_status == "NOT_CHECKED"
        # Blank means UNKNOWN, never zero.
        assert destination.verified_nightly_usd is None


def test_every_candidate_has_at_least_one_recognized_theme():
    for destination in load_destination_candidates():
        assert destination.goals, destination.destination_id
        tokens = [t.strip().lower() for t in re.split(r"[|;,]", destination.raw_themes) if t.strip()]
        assert all(token in mappings.CATALOG_THEME_TO_GOAL for token in tokens), destination.raw_themes


def test_both_theme_vocabularies_normalize_to_trip_goals():
    assert parse_goals("Explorer|Connector|Reflector") == ("discovery", "connection", "reflection")
    assert parse_goals("Discovery; Inspiration") == ("discovery", "inspiration")
    assert parse_goals("Restorer|Restoration") == ("restoration",)


def test_catalog_pace_values_are_understood():
    for destination in load_destination_candidates():
        if destination.catalog_pace is None:
            assert destination.pace_context.lower().startswith("guest-dependent")
        else:
            assert destination.catalog_pace in mappings.CATALOG_PACE_ORDER


# ==================== mapping coverage ====================

def test_every_intake_mapping_key_is_mapped_to_real_form_fields():
    csv_keys = {row["intake_key"] for row in _csv_rows("INTAKE_MAPPING.csv")}
    assert csv_keys == set(mappings.INTAKE_KEY_TO_FORM_FIELDS)
    model_fields = set(TravelIntakeRequest.model_fields)
    for fields in mappings.INTAKE_KEY_TO_FORM_FIELDS.values():
        assert set(fields) <= model_fields


def test_private_free_text_fields_exist_on_the_intake():
    assert set(mappings.PRIVATE_FREE_TEXT_FIELDS) <= set(TravelIntakeRequest.model_fields)


def test_every_moment_environment_and_pace_has_a_mapping():
    assert set(mappings.MOMENT_KEYWORDS) == {moment.value for moment in PreferredMoment}
    assert set(mappings.ENVIRONMENT_KEYWORDS) == {
        environment.value for environment in PreferredEnvironment
    } - {"surprise_me"}
    assert set(mappings.TRIP_PACE_TO_CATALOG_PACE) == {pace.value for pace in TripPace}
    assert set(mappings.TRIP_PACE_ITINERARY_GUIDANCE) == {pace.value for pace in TripPace}


def test_every_restriction_has_a_defined_check():
    handled = (
        set(mappings.RESTRICTION_CONFLICT_RULES)
        | mappings.ITINERARY_LEVEL_RESTRICTIONS
        | {"other"}
    )
    assert handled == {restriction.value for restriction in ActivityRestriction}
    assert set(mappings.RESTRICTION_RISK_SEASON) <= set(mappings.RESTRICTION_CONFLICT_RULES)


def test_every_goal_has_label_and_definition():
    assert set(mappings.TRIP_GOAL_LABELS) == set(mappings.TRIP_GOAL_DEFINITIONS)
    assert set(mappings.CATALOG_THEME_TO_GOAL.values()) <= set(mappings.TRIP_GOAL_LABELS)


def test_score_weights_sum_to_100():
    assert sum(mappings.SCORE_WEIGHTS.values()) == 100
