"""
Shared orchestration between POST /v2/retreat-recommendations (raw ranked
properties) and POST /get_suggested_city (the same ranked properties, grouped
into city-level suggestions). Both routes run the identical deterministic
filter/scoring pipeline over the identical profile -- the city endpoint only
adds a grouping step on top; it never re-derives matches differently.
"""

from typing import List, Set, Tuple

from app.schemas.retreat_v2_schema import RetreatRecommendationRequest
from src.core.matching_profile import MatchingProfile, build_matching_profile
from src.core.retreat_ranker import RankedCandidate, RankingResult, rank_properties

# Large enough that grouping, plus several regenerate_suggested_city calls in a
# row, can keep surfacing new distinct cities without re-running the filter
# and scoring pass (which is cheap and deterministic regardless of pool size).
# Still well under the 150-row catalog -- this is a depth budget, not "return
# everything" -- and ties within it are broken by rank_properties' own stable
# (score, property_id) ordering.
DEFAULT_POOL_SIZE = 60


def build_ranked_pool(
    request: RetreatRecommendationRequest, pool_size: int = DEFAULT_POOL_SIZE
) -> Tuple[MatchingProfile, RankingResult]:
    """Run the deterministic matching pipeline once. No LLM calls happen here."""
    profile = build_matching_profile(request)
    ranking = rank_properties(profile, limit=pool_size)
    return profile, ranking


def _location_key(record: dict) -> Tuple[str, str]:
    return (
        (record.get("Region") or "").strip().lower(),
        (record.get("Country") or "").strip().lower(),
    )


def select_city_representatives(
    ranked: List[RankedCandidate],
    exclude_property_ids: Set[str] = frozenset(),
    limit: int = 5,
) -> List[RankedCandidate]:
    """
    Pick the single best-scoring property per distinct (Region, Country) so
    every returned "city" is backed by exactly one concrete, bookable
    property -- never a model-invented place name. `ranked` is already sorted
    best-first, so the first candidate seen for a location is its best.
    """
    seen_locations: Set[Tuple[str, str]] = set()
    representatives: List[RankedCandidate] = []
    for candidate in ranked:
        if candidate.property_id in exclude_property_ids:
            continue
        location_key = _location_key(candidate.record)
        if location_key in seen_locations:
            continue
        seen_locations.add(location_key)
        representatives.append(candidate)
        if len(representatives) >= limit:
            break
    return representatives
