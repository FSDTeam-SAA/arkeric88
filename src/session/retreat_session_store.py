"""
In-memory session store for POST /v2/retreat-recommendations, mirroring the
existing CitySessionStore pattern (src/session/city_session_store.py).

Per BACKEND_DEVELOPER_CHANGES.md "Persistence and Versioning": every session
records the questionnaire schema version, scoring version, answer-mapping
version and database size used to produce it, so a shortlist can always be
explained after the fact even if those versions later change.

NOTE: like the legacy session stores, this is process-memory only. It is NOT
durable across restarts and will NOT stay consistent across multiple API
workers/processes. BACKEND_DEVELOPER_CHANGES.md flags this as required future
work ("Persist sessions in a durable store before using multiple API
workers") -- swap this module for a real datastore (Redis/Postgres/etc.)
before running with more than one worker process.
"""

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional
from uuid import uuid4


@dataclass
class RetreatRecommendationSession:
    recommendation_session_id: str
    request: dict
    response: dict
    created_at: str
    schema_version: str
    scoring_version: str
    answer_mapping_version: str
    database_version: str


class RetreatSessionStore:
    _sessions: dict[str, RetreatRecommendationSession] = {}

    @classmethod
    def create(
        cls,
        request: dict,
        response: dict,
        schema_version: str,
        scoring_version: str,
        answer_mapping_version: str,
        database_version: str,
        recommendation_session_id: Optional[str] = None,
    ) -> RetreatRecommendationSession:
        session = RetreatRecommendationSession(
            recommendation_session_id=recommendation_session_id or str(uuid4()),
            request=deepcopy(request),
            response=deepcopy(response),
            created_at=datetime.now(timezone.utc).isoformat(),
            schema_version=schema_version,
            scoring_version=scoring_version,
            answer_mapping_version=answer_mapping_version,
            database_version=database_version,
        )
        cls._sessions[session.recommendation_session_id] = session
        return deepcopy(session)

    @classmethod
    def get(cls, recommendation_session_id: str) -> Optional[RetreatRecommendationSession]:
        session = cls._sessions.get(recommendation_session_id)
        return deepcopy(session) if session else None
