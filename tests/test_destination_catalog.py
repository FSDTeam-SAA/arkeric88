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
from src.core import destination_catalog as catalog
from src.core import intake_mappings as mappings
from src.core.destination_catalog import (
    CATALOG_SNAPSHOT_DATE,
    DATA_DIR,
    EXPECTED_BACKLOG_COUNT,
    EXPECTED_CANDIDATE_COUNT,
    EXPECTED_EDITORIAL_COUNT,
    get_catalog_version,
    get_destination,
    load_destination_candidates,
    load_discovery_backlog,
    parse_goals,
    parse_paces,
    parse_secondary_goals,
)


def _csv_rows(suffix: str) -> list:
    path = next(DATA_DIR.glob(f"*{suffix}"))
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


# ==================== catalog integrity ====================

def test_counts_match_import_rules_import_verification():
    rules = {row["Rule"]: row["Required behavior"] for row in _csv_rows("IMPORT_RULES.csv")}
    assert "1,500 rows and 1,500 unique destination_id values" in rules["Import verification"]
    candidates = load_destination_candidates()
    assert len(_csv_rows("AI_IMPORT_DESTINATIONS.csv")) == EXPECTED_CANDIDATE_COUNT == len(candidates) == 1500
    assert len({destination.destination_id for destination in candidates}) == EXPECTED_CANDIDATE_COUNT
    assert len(load_discovery_backlog()) == EXPECTED_BACKLOG_COUNT
    version = get_catalog_version()
    assert version["candidate_count"] == EXPECTED_CANDIDATE_COUNT
    assert version["editorial_count"] == EXPECTED_EDITORIAL_COUNT
    assert version["location_enriched_count"] == EXPECTED_BACKLOG_COUNT
    assert version["snapshot_date"] == CATALOG_SNAPSHOT_DATE


def test_rows_keep_their_source_order_and_status():
    candidates = load_destination_candidates()
    assert all(d.candidate_status == "CANDIDATE_ONLY" for d in [*candidates[:72], *candidates[1000:]])
    assert all(d.candidate_status == "LOCATION_ENRICHED" for d in candidates[72:1000])


def test_every_backlog_row_is_imported_into_the_catalog():
    for row in load_discovery_backlog():
        assert row["import_status"] == "ENRICHED_IN_IMPORT"
        destination = get_destination(row["destination_id"])
        assert destination is not None and destination.is_location_enriched


def test_duplicate_ids_in_a_catalogue_update_keep_the_first_row(monkeypatch):
    rows = list(_csv_rows("AI_IMPORT_DESTINATIONS.csv"))
    duplicate = {**rows[0], "destination": "Imposter"}
    monkeypatch.setattr(catalog, "_read_rows", lambda suffix: [*rows, duplicate])
    load_destination_candidates.cache_clear()
    try:
        loaded = load_destination_candidates()
        assert len(loaded) == EXPECTED_CANDIDATE_COUNT
        assert loaded[0].destination == rows[0]["destination"]
    finally:
        load_destination_candidates.cache_clear()


def test_every_candidate_stays_unverified_and_price_unknown():
    for destination in load_destination_candidates():
        assert destination.candidate_status in {"CANDIDATE_ONLY", "LOCATION_ENRICHED"}
        assert destination.source_review_status in {
            "SOURCE_CLAIM_REVIEW_REQUIRED", "EDITORIAL_DRAFT_NEEDS_SOURCE_CHECK", "SOURCE_REVIEW_WITH_OPEN_CHECKS",
        }
        assert destination.live_lodging_status == "NOT_CHECKED"
        # Blank means UNKNOWN, never zero.
        assert destination.verified_nightly_usd is None


def test_every_supplied_theme_is_recognized_and_only_71_rows_have_none():
    candidates = load_destination_candidates()
    assert sum(not destination.raw_themes for destination in candidates) == 71
    for destination in candidates:
        assert destination.goals or not destination.raw_themes, destination.destination_id
        tokens = [t.strip().lower() for t in re.split(r"[|;,]", destination.raw_themes) if t.strip()]
        assert all(token in mappings.CATALOG_THEME_TO_GOAL for token in tokens), destination.raw_themes


def test_both_theme_vocabularies_normalize_to_trip_goals():
    assert parse_goals("Explorer|Connector|Reflector") == ("discovery", "connection", "reflection")
    assert parse_goals("Explorer|Connector|Inspirer") == ("discovery", "connection", "inspiration")
    assert parse_goals("Discovery; Inspiration") == ("discovery", "inspiration")
    assert parse_goals("Restorer|Restoration") == ("restoration",)
    assert parse_secondary_goals("Explorer|Connector") == ("adventure",)
    assert parse_secondary_goals("Explorer|Adventurer") == ()


def test_catalog_pace_values_are_understood():
    assert parse_paces("Gentle|Balanced") == ("Gentle", "Balanced")
    for destination in load_destination_candidates():
        if not destination.catalog_paces:
            # "Slow" (one row) is reported in the import validation and treated as pace to confirm.
            assert destination.pace_context == "Slow" or destination.pace_context.lower().startswith("guest-dependent")
        assert set(destination.catalog_paces) <= set(mappings.CATALOG_PACE_ORDER)


def test_template_clauses_are_left_out_of_matching_and_guest_text():
    pardubice = get_destination("NE-1159140667")
    assert "required transfers" in pardubice.tradeoffs_to_check
    assert "required transfers" not in pardubice.specific_tradeoffs
    assert "required transfers" not in pardubice.field_text("tradeoffs_to_check")
    assert "packed schedule" not in pardubice.guest_rationale
    barcelona = get_destination("NE-1159151261")
    assert barcelona.specific_tradeoffs == "Crowding and visitor pressure"
    assert "editorial interpretation" not in barcelona.guest_rationale
    assert not barcelona.guest_rationale.lower().startswith("consider when")
    # Short templates that still describe the place are kept.
    rotorua = get_destination("NZ-ROT")
    assert rotorua.specific_tradeoffs == rotorua.tradeoffs_to_check


def test_notes_on_how_a_tag_was_chosen_never_reach_guest_text():
    wollemi = get_destination("OC-WOLLEMI-NATIONAL-PARK")
    assert "hypothesis" in wollemi.editorial_rationale
    assert "hypothesis" not in wollemi.guest_rationale and "may suit travelers" not in wollemi.guest_rationale
    assert wollemi.guest_rationale.startswith("Consider a guided route")


def test_experiences_split_on_pipes_and_lose_booking_words():
    counts = catalog.Counter()
    items = catalog._experience_items("Booked cooking class|Date-confirmed Oaxaca hike|Guided walk, once confirmed", counts)
    assert [shown for _, shown in items] == ["Cooking class", "Oaxaca hike"]
    assert items[0][0] == "Booked cooking class"  # the catalog wording stays for matching
    sentence = "Explore rainforest, lakes and beach routes."
    assert [shown for _, shown in catalog._experience_items(sentence, counts)] == [sentence.rstrip(".")]


def test_a_row_with_run_together_words_shows_none_of_its_experiences():
    counts = catalog.Counter({"harbour": 5, "history": 5})
    assert catalog.is_run_together("harbourhistory", counts)
    assert catalog.is_run_together("TeAwaruaOPorirua", counts)
    assert not catalog.is_run_together("interpretation", counts)
    assert not catalog.is_run_together("McGovern", counts)
    assert not catalog.is_run_together("birdwatching", catalog.Counter({"bird": 5, "watching": 5}))
    assert not catalog.is_run_together("transatlantic", catalog.Counter({"trans": 5, "atlantic": 5}))
    assert catalog._experience_items("Local harbourhistory|Museum visit", counts) == ()


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
