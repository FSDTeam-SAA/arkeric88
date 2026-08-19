"""
Versioned retreat-recommendation API described in BACKEND_DEVELOPER_CHANGES.md.

POST /v2/retreat-recommendations replaces "recommend a city, then guess a
hotel" with "rank the 150 retreat properties directly." It is a new endpoint,
not a repurposing of /get_suggested_city -- the legacy endpoint and its
questionnaire (app/schemas/city_body.QuestionAnswers) are untouched so any
existing client keeps working (see FRONTEND_DEVELOPER_CHANGES.md "Do not
submit the new questionnaire to the existing city-suggestion endpoint").
"""

from uuid import uuid4

from fastapi import APIRouter, HTTPException

from app.schemas.retreat_v2_schema import (
    ExtractedRestrictions as ExtractedRestrictionsSchema,
    QUESTIONNAIRE_SCHEMA_VERSION,
    RankedProperty,
    RetreatRecommendationRequest,
    RetreatRecommendationResponse,
    ScoreBreakdown,
)
from src.core.answer_mappings import ANSWER_MAPPING_VERSION
from src.core.retreat_catalog import get_database_version, parse_avg_night, parse_best_season
from src.core.retreat_explanation import generate_match_explanations
from src.core.retreat_matching_orchestrator import build_ranked_pool
from src.core.retreat_scoring import SCORING_VERSION
from src.session.retreat_session_store import RetreatSessionStore

router = APIRouter(prefix="/v2", tags=["retreat-recommendations"])

# Number of properties returned directly by this endpoint. POST /get_suggested_city
# runs the identical build_ranked_pool() pipeline with a much larger pool size
# (see retreat_matching_orchestrator.DEFAULT_POOL_SIZE) so it has enough depth
# to group into several distinct cities and support regeneration -- this
# endpoint stays a small, direct property shortlist.
DEFAULT_RECOMMENDATION_COUNT = 5


def _restriction_status(profile) -> str:
    if not profile.restrictions.codes and not profile.restrictions.unresolved_text:
        return "not_applicable"
    return "unverified"


def _build_ranked_property(candidate, explanation: dict, restriction_status: str) -> RankedProperty:
    record = candidate.record
    price = parse_avg_night(record)
    return RankedProperty(
        property_id=candidate.property_id,
        property_name=record.get("Property Name", ""),
        country=record.get("Country", ""),
        region=record.get("Region", ""),
        settings=[],
        match_score=round(candidate.total_score),
        score_breakdown=ScoreBreakdown(**candidate.breakdown),
        match_reasons=explanation["match_reasons"],
        warnings=[*candidate.warnings, *explanation["warnings"]],
        restriction_status=restriction_status,
        avg_night=price["amount"],
        avg_night_is_lower_bound=price["is_lower_bound"],
        avg_night_raw=price["raw"],
        budget_tier=record.get("Budget Tier", ""),
        program_cost=record.get("Program Cost", ""),
        best_season=parse_best_season(record),
        best_season_raw=record.get("Best Season", ""),
    )


@router.post("/retreat-recommendations", response_model=RetreatRecommendationResponse)
async def get_retreat_recommendations(request: RetreatRecommendationRequest):
    """
    Rank the retreat database against the 15-question profile and return a
    shortlist of properties (not cities). See AI_DEVELOPER_CHANGES.md and
    API_V2_DOCS.md for the full filter/scoring rules and a sample response.
    """
    try:
        profile, ranking = build_ranked_pool(request, pool_size=DEFAULT_RECOMMENDATION_COUNT)
        explanations = generate_match_explanations(ranking.ranked)
        restriction_status = _restriction_status(profile)

        recommendations = [
            _build_ranked_property(candidate, explanations[candidate.property_id], restriction_status)
            for candidate in ranking.ranked
        ]

        recommendation_session_id = str(uuid4())
        response = RetreatRecommendationResponse(
            recommendation_session_id=recommendation_session_id,
            schema_version=QUESTIONNAIRE_SCHEMA_VERSION,
            scoring_version=SCORING_VERSION,
            answer_mapping_version=ANSWER_MAPPING_VERSION,
            database_version=get_database_version(),
            recommendations=recommendations,
            excluded_count=ranking.excluded_count,
            total_candidate_count=ranking.total_candidate_count,
            extracted_restrictions=ExtractedRestrictionsSchema(
                codes=profile.restrictions.codes,
                accessibility_needs=profile.restrictions.accessibility_needs,
                unresolved_text=profile.restrictions.unresolved_text,
            ),
            data_gaps=ranking.data_gaps,
        )

        RetreatSessionStore.create(
            request=request.model_dump(mode="json"),
            response=response.model_dump(mode="json"),
            schema_version=QUESTIONNAIRE_SCHEMA_VERSION,
            scoring_version=SCORING_VERSION,
            answer_mapping_version=ANSWER_MAPPING_VERSION,
            database_version=get_database_version(),
            recommendation_session_id=recommendation_session_id,
        )
        return response
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


@router.get("/retreat-recommendations/{recommendation_session_id}", response_model=RetreatRecommendationResponse)
async def get_retreat_recommendation_session(recommendation_session_id: str):
    """Retrieve a previously computed shortlist by its recommendation_session_id."""
    session = RetreatSessionStore.get(recommendation_session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Recommendation session not found.")
    return session.response
