"""
Orchestrates the deterministic matching pipeline: load the catalog, apply hard
filters, score survivors, and return a stably-ordered shortlist plus the audit
metadata (excluded_count, total_candidate_count, data_gaps) the v2 response
contract requires.

No LLM calls happen in this module. See retreat_explanation.py for the
strictly-limited step where the model is allowed to add match_reasons/warnings
text on top of this module's output (AI_DEVELOPER_CHANGES.md #5).
"""

from dataclasses import dataclass
from typing import List

from src.core.matching_profile import MatchingProfile
from src.core.retreat_catalog import load_retreat_catalog, make_property_id
from src.core.retreat_scoring import apply_hard_filters, score_property


@dataclass
class RankedCandidate:
    property_id: str
    record: dict
    total_score: float
    breakdown: dict
    warnings: List[str]


@dataclass
class RankingResult:
    ranked: List[RankedCandidate]
    excluded_count: int
    total_candidate_count: int
    data_gaps: List[str]


def _data_gaps(profile: MatchingProfile) -> List[str]:
    gaps = []
    if profile.settings:
        gaps.append(
            "Setting is not yet a database field (see BACKEND_DEVELOPER_CHANGES.md "
            "'Database Changes'); the requested setting(s) were recorded but could not "
            "be used to filter or score properties."
        )
    if profile.restrictions.codes or profile.restrictions.unresolved_text:
        gaps.append(
            "Activity/Restriction Tags are not yet a database field; restrictions were "
            "extracted from free text but could not be verified against any property."
        )
    if profile.party_fit_unverified:
        gaps.append(
            "Family Fit / Small Group Fit are not yet database fields; party suitability "
            "was estimated from the property's overall Social score."
        )
    return gaps


def rank_properties(profile: MatchingProfile, limit: int = 5) -> RankingResult:
    catalog = load_retreat_catalog()
    total_candidate_count = len(catalog)
    excluded_count = 0
    scored: List[RankedCandidate] = []

    for record in catalog:
        filter_result = apply_hard_filters(record, profile)
        if not filter_result.passed:
            excluded_count += 1
            continue
        scored_property = score_property(record, profile)
        scored.append(
            RankedCandidate(
                property_id=make_property_id(record),
                record=record,
                total_score=scored_property.total_score,
                breakdown=scored_property.breakdown,
                warnings=scored_property.warnings,
            )
        )

    # Secondary sort key (property_id) guarantees a stable, reproducible order
    # for tied scores regardless of dict/catalog iteration order.
    scored.sort(key=lambda candidate: (-candidate.total_score, candidate.property_id))

    return RankingResult(
        ranked=scored[:limit],
        excluded_count=excluded_count,
        total_candidate_count=total_candidate_count,
        data_gaps=_data_gaps(profile),
    )
