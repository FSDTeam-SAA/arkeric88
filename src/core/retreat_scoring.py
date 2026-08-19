"""
Deterministic hard filters and weighted scoring for a single retreat record
against a MatchingProfile.

Implements AI_DEVELOPER_CHANGES.md #2 ("Implement deterministic ranking"):
hard filters run first and unconditionally exclude a property; only surviving
properties are scored. Scoring never uses unweighted keyword counting -- every
component below is a named, documented weight, and the full breakdown is
returned so a shortlist can be audited (see RankedProperty.score_breakdown).

Bump SCORING_VERSION whenever a weight or filter rule changes.
"""

from dataclasses import dataclass, field
from typing import List, Optional

from src.core.answer_mappings import (
    PHYSICAL_INTENSITY_ORDER,
    SPIRITUALITY_ORDER,
    STRUCTURE_ORDER,
)
from src.core.matching_profile import MatchingProfile
from src.core.retreat_catalog import parse_avg_night, parse_best_season, split_multi_value

SCORING_VERSION = "2026-08-15.1"

# Weights sum to 100 so match_score reads as a 0-100 percentage.
WEIGHTS = {
    "archetype": 20,
    "transform_focus": 18,
    "emotional_tone": 8,
    "structure": 10,
    "physical_intensity": 8,
    "party_social": 10,
    "emotional_safety": 8,
    "nature": 8,
    "luxury": 5,
    "spirituality": 5,
}
assert sum(WEIGHTS.values()) == 100


@dataclass
class FilterResult:
    passed: bool
    reasons: List[str] = field(default_factory=list)


@dataclass
class ScoredProperty:
    record: dict
    total_score: float
    breakdown: dict
    warnings: List[str]


def _safe_float(value: str, default: float = 5.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _ordinal_distance_score(order: list, a: str, b: str, weight: float) -> float:
    if a not in order or b not in order:
        return weight * 0.5  # unknown value: neutral, not a penalty for missing data
    span = max(len(order) - 1, 1)
    distance = abs(order.index(a) - order.index(b))
    return weight * (1 - distance / span)


def apply_hard_filters(record: dict, profile: MatchingProfile) -> FilterResult:
    """Return whether `record` survives every hard constraint. See AI_DEVELOPER_CHANGES.md #2."""
    reasons: List[str] = []

    # Budget: an open-ended budget is a floor, not a ceiling, so it never excludes.
    if not profile.budget_open_ended:
        price = parse_avg_night(record)
        if price["amount"] is not None and price["amount"] > profile.budget_max:
            reasons.append(
                f"Avg Night ({price['raw']}) exceeds the {profile.budget_max:g}/night budget."
            )

    # Spirituality: the user's answer is a maximum acceptable level.
    record_spirituality = (record.get("Spirituality") or "").strip()
    if record_spirituality in SPIRITUALITY_ORDER:
        if SPIRITUALITY_ORDER.index(record_spirituality) > profile.spirituality_max_ordinal:
            reasons.append(
                f"Spirituality level '{record_spirituality}' exceeds the requested maximum."
            )

    # Season: only filters when the traveler gave specific months.
    if profile.travel_months:
        record_months = set(parse_best_season(record))
        if record_months and not (record_months & set(profile.travel_months)):
            reasons.append("Best Season does not overlap the requested travel months.")

    # Physical intensity: only the clearly incompatible pair is a hard filter --
    # a traveler who wants Gentle must never receive a Challenging property.
    # Other combinations (e.g. wanting Challenging but the property is Gentle)
    # are handled by scoring only, since a restorative property can still suit
    # someone open to more intensity.
    record_intensity = (record.get("Physical Intensity") or "").strip()
    if profile.physical_intensity_db_value == "Gentle" and record_intensity == "Challenging":
        reasons.append("Physical Intensity 'Challenging' is incompatible with a Gentle request.")

    # Setting and activity-restriction tags are not yet present in the workbook
    # (see BACKEND_DEVELOPER_CHANGES.md "Database Changes"), so they are
    # intentionally NOT hard-filtered here -- doing so would silently exclude
    # properties based on data that doesn't exist. They surface as warnings
    # and top-level data_gaps instead (see retreat_ranker.py).

    return FilterResult(passed=not reasons, reasons=reasons)


def score_property(record: dict, profile: MatchingProfile) -> ScoredProperty:
    breakdown = {}
    warnings: List[str] = []

    # Archetype fit
    archetypes = split_multi_value(record.get("Archetypes", ""))
    if profile.primary_archetype in archetypes:
        breakdown["archetype"] = WEIGHTS["archetype"]
    elif profile.secondary_archetype and profile.secondary_archetype in archetypes:
        breakdown["archetype"] = WEIGHTS["archetype"] * 0.6
    else:
        breakdown["archetype"] = 0.0

    # Transform Focus overlap
    record_focus = set(split_multi_value(record.get("Transform Focus", "")))
    requested_focus = set(profile.transform_focus)
    overlap_ratio = len(record_focus & requested_focus) / max(len(requested_focus), 1)
    breakdown["transform_focus"] = WEIGHTS["transform_focus"] * overlap_ratio

    # Emotional tone fit (fuzzy token match against free-text "Emotional Tone")
    tone_text = (record.get("Emotional Tone") or "").lower()
    if profile.desired_tones:
        matched_tones = sum(1 for tone in profile.desired_tones if tone in tone_text)
        breakdown["emotional_tone"] = WEIGHTS["emotional_tone"] * (
            matched_tones / len(profile.desired_tones)
        )
    else:
        breakdown["emotional_tone"] = WEIGHTS["emotional_tone"] * 0.5

    # Structure fit
    record_structure = (record.get("Structure") or "").strip()
    breakdown["structure"] = _ordinal_distance_score(
        STRUCTURE_ORDER, profile.structure_db_value, record_structure, WEIGHTS["structure"]
    )

    # Physical intensity fit
    record_intensity = (record.get("Physical Intensity") or "").strip()
    breakdown["physical_intensity"] = _ordinal_distance_score(
        PHYSICAL_INTENSITY_ORDER,
        profile.physical_intensity_db_value,
        record_intensity,
        WEIGHTS["physical_intensity"],
    )

    # Party / social suitability
    party_column_value = _safe_float(record.get(profile.party_score_column, ""))
    breakdown["party_social"] = WEIGHTS["party_social"] * (party_column_value / 10)
    if profile.party_fit_unverified:
        warnings.append(
            f"'{profile.party_type}' suitability is estimated from the property's overall "
            "Social score; the workbook has no dedicated Family/Small-Group Fit data yet."
        )

    # Emotional safety
    emotional_safety_value = _safe_float(record.get("Emotional Safety", ""))
    breakdown["emotional_safety"] = WEIGHTS["emotional_safety"] * (emotional_safety_value / 10)

    # Nature fit
    nature_value = _safe_float(record.get("Nature", ""))
    breakdown["nature"] = WEIGHTS["nature"] * (
        1 - abs(profile.nature_target - nature_value) / 10
    )

    # Luxury fit
    luxury_value = _safe_float(record.get("Luxury", ""))
    breakdown["luxury"] = WEIGHTS["luxury"] * (
        1 - abs(profile.luxury_target - luxury_value) / 10
    )

    # Spirituality closeness (distinct from the hard-filter ceiling above --
    # this rewards matching the *desired* level, not just staying under it).
    record_spirituality = (record.get("Spirituality") or "").strip()
    breakdown["spirituality"] = _ordinal_distance_score(
        SPIRITUALITY_ORDER, profile.spirituality_db_value, record_spirituality, WEIGHTS["spirituality"]
    )

    if profile.settings:
        warnings.append(
            "Setting preference could not be verified: the workbook has no Setting "
            "column yet, so this property was not filtered or scored on environment."
        )

    if profile.restrictions.codes or profile.restrictions.unresolved_text:
        warnings.append(
            "Activity restrictions could not be verified against structured property "
            "data yet; treat restriction_status as advisory only."
        )

    total_score = sum(breakdown.values())
    breakdown = {key: round(value, 2) for key, value in breakdown.items()}
    return ScoredProperty(
        record=record,
        total_score=round(total_score, 2),
        breakdown=breakdown,
        warnings=warnings,
    )
