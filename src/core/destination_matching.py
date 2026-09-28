"""
Deterministic destination matching for the Velari intake.

Pipeline (IMPORT_RULES.csv), with no LLM anywhere in it:
1. Hard exclusions before ranking: must-avoid conflicts, unknown high-impact
   suitability, straight-line distance beyond the guest's tolerance, and a
   verified nightly price above budget. Budget is never relaxed silently.
2. Weighted scoring: desired outcomes (primary), experience interests,
   setting, pace and travel-distance fit; prefer-avoid is a penalty with an
   explained tradeoff.
3. Diverse selection of 2-3 options (distinct countries, varied settings).
4. Explanations tied to the guest's own answer labels and the specific
   catalog field that was checked, plus tradeoffs and unresolved facts.

Nothing here upgrades a catalog row's verification status: every suggestion
stays a CANDIDATE_ONLY editorial hypothesis until source review and live
checks happen.
"""

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Dict, Iterable, List, Optional, Set, Tuple

from src.core.destination_catalog import Destination, load_destination_candidates
from src.core.geography import great_circle_km, is_southern_hemisphere
from src.core.intake_mappings import (
    ENVIRONMENT_KEYWORDS,
    ENVIRONMENT_LABELS,
    HIGH_IMPACT_DESTINATION_RESTRICTIONS,
    ITINERARY_LEVEL_RESTRICTIONS,
    MOMENT_GOAL_SIGNALS,
    MOMENT_KEYWORDS,
    MOMENT_PACE_SIGNALS,
    PREFER_AVOID_PENALTY,
    PREFERRED_MOMENT_LABELS,
    RESTRICTION_CONFLICT_RULES,
    RESTRICTION_LABELS,
    RESTRICTION_RISK_SEASON,
    SCORE_WEIGHTS,
    SUGGESTION_COUNT,
    TRAVEL_DISTANCE_LABELS,
    TRAVEL_DISTANCE_MAX_STRAIGHT_LINE_KM,
    TRIP_GOAL_LABELS,
    TRIP_PACE_LABELS,
    CATALOG_PACE_ORDER,
)
from src.core.origin import Origin
from src.core.trip_profile import TripProfile, season_months_are_assumed

# Score window within which a lower-ranked option may be preferred to vary
# the setting of the shortlist.
DIVERSITY_SCORE_WINDOW = 8.0

_MATCH_TEXT_FIELDS = ("setting_context", "experience_context", "editorial_rationale")


# ==================== RESULT TYPES ====================

@dataclass
class RestrictionCheck:
    restriction: str
    label: str
    severity: str
    # no_catalog_concern | conflict | seasonal_outside_dates | unverified | itinerary_stage
    status: str
    detail: str

    def to_dict(self) -> dict:
        return {
            "restriction": self.restriction,
            "label": self.label,
            "severity": self.severity,
            "status": self.status,
            "detail": self.detail,
        }


@dataclass
class DistanceCheck:
    # no_limit | within_straight_line_limit | beyond_tolerance | not_checked
    status: str
    straight_line_km: Optional[float]
    detail: str

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "straight_line_km": self.straight_line_km,
            "detail": self.detail,
            "route_status": "UNVERIFIED",
        }


@dataclass
class ScoredDestination:
    destination: Destination
    total_score: float
    breakdown: Dict[str, float]
    matched_goals: List[str]
    matched_moments: List[str]
    matched_environments: List[str]
    setting_category: str
    pace_fit: str
    restriction_checks: List[RestrictionCheck]
    distance: DistanceCheck
    tradeoffs: List[str] = field(default_factory=list)

    @property
    def destination_id(self) -> str:
        return self.destination.destination_id


@dataclass
class Exclusion:
    destination_id: str
    reason: str
    detail: str


@dataclass
class MatchResult:
    ranked: List[ScoredDestination]
    exclusions: List[Exclusion]
    total_candidate_count: int
    data_gaps: List[str]

    def excluded_by_reason(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for exclusion in self.exclusions:
            counts[exclusion.reason] = counts.get(exclusion.reason, 0) + 1
        return counts


# ==================== TEXT MATCHING ====================

@lru_cache(maxsize=None)
def _compile(fragments: tuple) -> re.Pattern:
    return re.compile(r"\b(?:" + "|".join(fragments) + r")\b", re.IGNORECASE)


def _matches(fragments: Iterable[str], text: str) -> bool:
    fragments = tuple(fragments)
    return bool(fragments) and bool(_compile(fragments).search(text or ""))


def _combined_text(destination: Destination, field_names: Iterable[str]) -> str:
    return " | ".join(destination.field_text(name) for name in field_names)


def _join_labels(labels: List[str]) -> str:
    if len(labels) <= 1:
        return "".join(labels)
    return ", ".join(labels[:-1]) + " and " + labels[-1]


# ==================== COMPONENT CHECKS ====================

def _risk_months_for(restriction: str, destination: Destination) -> List[int]:
    months = RESTRICTION_RISK_SEASON[restriction]
    if is_southern_hemisphere(destination.country, destination.latitude):
        return [((month + 5) % 12) + 1 for month in months]
    return list(months)


def check_restriction(
    restriction: str, severity: str, destination: Destination, profile: TripProfile
) -> RestrictionCheck:
    label = RESTRICTION_LABELS[restriction]

    if restriction in ITINERARY_LEVEL_RESTRICTIONS:
        return RestrictionCheck(
            restriction, label, severity, "itinerary_stage",
            "Can only be checked per restaurant, stay and activity; it will be applied as a firm "
            "constraint when the itinerary is built and must be confirmed with each provider.",
        )

    rule = RESTRICTION_CONFLICT_RULES.get(restriction)
    if rule is None:  # "other" -- free-text need the catalog cannot check.
        return RestrictionCheck(
            restriction, label, severity, "unverified",
            "This planning need could not be checked against catalog data.",
        )

    text = _combined_text(destination, rule["fields"])
    if not _matches(rule["patterns"], text):
        if restriction in HIGH_IMPACT_DESTINATION_RESTRICTIONS:
            return RestrictionCheck(
                restriction, label, severity, "unverified",
                "The catalog has no verified information about this for this destination.",
            )
        return RestrictionCheck(
            restriction, label, severity, "no_catalog_concern",
            "The catalog flags no concern; this has not been independently verified.",
        )

    concern = destination.tradeoffs_to_check or destination.setting_context
    if (
        _matches(rule["seasonal"], text)
        and profile.travel_months
        and restriction in RESTRICTION_RISK_SEASON
        and not set(profile.travel_months) & set(_risk_months_for(restriction, destination))
    ):
        return RestrictionCheck(
            restriction, label, severity, "seasonal_outside_dates",
            f"The catalog notes a seasonal concern ('{concern}') that falls outside your travel months; "
            "confirm conditions for your dates.",
        )
    return RestrictionCheck(
        restriction, label, severity, "conflict",
        f"The catalog flags this: '{concern}'.",
    )


def _is_hard_exclusion(check: RestrictionCheck) -> bool:
    if check.severity != "must_avoid":
        return False
    if check.status == "conflict":
        return True
    # "Unknown high-impact suitability is not a pass."
    return check.status == "unverified" and check.restriction in HIGH_IMPACT_DESTINATION_RESTRICTIONS


def check_distance(
    destination: Destination,
    profile: TripProfile,
    origin: Optional[Origin],
    looked_up: Optional[Tuple[float, float]] = None,
) -> DistanceCheck:
    """`looked_up`: map-lookup coordinates for a catalog row that has none."""
    limit_km = TRAVEL_DISTANCE_MAX_STRAIGHT_LINE_KM[profile.travel_distance]
    if limit_km is None:
        return DistanceCheck("no_limit", None, "You're open to anywhere; route and travel time not checked.")
    if origin is None or not origin.has_coordinates:
        return DistanceCheck("not_checked", None, "Your departure point could not be located, so distance was not checked.")
    latitude, longitude = destination.latitude, destination.longitude
    if (latitude is None or longitude is None) and looked_up is not None:
        latitude, longitude = looked_up
    if latitude is None or longitude is None:
        if (
            profile.travel_distance == "nearby"
            and origin.region
            and destination.world_region.strip().lower() != origin.region
        ):
            # No coordinates to measure, and a different world region: showing it
            # as "nearby" would present an unknown as a pass.
            return DistanceCheck(
                "beyond_tolerance", None,
                f"In a different world region ({destination.world_region}) from your departure point, "
                "and its distance could not be measured.",
            )
        return DistanceCheck("not_checked", None, "This catalog entry has no coordinates, so distance was not checked.")

    km = round(great_circle_km(origin.latitude, origin.longitude, latitude, longitude))
    if km > limit_km:
        return DistanceCheck(
            "beyond_tolerance", km,
            f"At least {km:,} km away in a straight line, beyond '{TRAVEL_DISTANCE_LABELS[profile.travel_distance]}'.",
        )
    return DistanceCheck(
        "within_straight_line_limit", km,
        f"About {km:,} km in a straight line; the actual route, mode and transfers have not been checked.",
    )


def _distance_points(check: DistanceCheck) -> float:
    weight = SCORE_WEIGHTS["travel_distance"]
    if check.status in {"no_limit", "within_straight_line_limit"}:
        return float(weight)
    return weight / 2  # Distance unknown: half credit.


def _setting_category(destination: Destination) -> str:
    for text in (destination.setting_context, _combined_text(destination, _MATCH_TEXT_FIELDS)):
        for environment, fragments in ENVIRONMENT_KEYWORDS.items():
            if _matches(fragments, text):
                return environment
    return "other"


def _pace_fit(destination: Destination, profile: TripProfile) -> str:
    if destination.catalog_pace is None:
        return "to_confirm"
    gap = abs(CATALOG_PACE_ORDER.index(destination.catalog_pace) - CATALOG_PACE_ORDER.index(profile.catalog_pace))
    return {0: "match", 1: "adjacent"}.get(gap, "mismatch")


_PACE_POINTS = {"match": 1.0, "adjacent": 0.5, "to_confirm": 0.5, "mismatch": 0.0}


def _matched_moments(destination: Destination) -> Dict[str, bool]:
    text = _combined_text(destination, _MATCH_TEXT_FIELDS)
    matched = {}
    for moment, fragments in MOMENT_KEYWORDS.items():
        matched[moment] = (
            _matches(fragments, text)
            or destination.catalog_pace in MOMENT_PACE_SIGNALS.get(moment, set())
            or bool(set(destination.goals) & MOMENT_GOAL_SIGNALS.get(moment, set()))
        )
    return matched


# ==================== SCORING ====================

def score_destination(
    destination: Destination,
    profile: TripProfile,
    origin: Optional[Origin],
    looked_up: Optional[Tuple[float, float]] = None,
) -> tuple:
    """Return (ScoredDestination | None, Exclusion | None). Hard filters run first."""
    checks = [
        check_restriction(code, severity, destination, profile)
        for code, severity in profile.restrictions.items()
    ]
    for check in checks:
        if _is_hard_exclusion(check):
            reason = "must_avoid" if check.status == "conflict" else "unverified_high_impact"
            return None, Exclusion(destination.destination_id, f"{reason}:{check.restriction}", check.detail)

    distance = check_distance(destination, profile, origin, looked_up)
    if distance.status == "beyond_tolerance":
        return None, Exclusion(destination.destination_id, "travel_distance", distance.detail)

    if (
        destination.verified_nightly_usd is not None
        and not profile.budget_open_ended
        and destination.verified_nightly_usd > profile.budget_per_night
    ):
        return None, Exclusion(
            destination.destination_id, "budget",
            f"Verified nightly cost ${destination.verified_nightly_usd:,.0f} exceeds your ${profile.budget_per_night:,.0f} budget.",
        )

    matched_goals = [goal for goal in profile.goals if goal in destination.goals]
    moment_hits = _matched_moments(destination)
    matched_moments = [moment for moment in profile.moments if moment_hits[moment]]
    if not matched_goals and not matched_moments:
        return None, Exclusion(
            destination.destination_id, "no_grounded_fit",
            "Matches none of your desired feelings or chosen moments.",
        )

    setting_text = _combined_text(destination, _MATCH_TEXT_FIELDS)
    matched_environments = (
        []
        if profile.surprise_me
        else [env for env in profile.environments if _matches(ENVIRONMENT_KEYWORDS[env], setting_text)]
    )
    pace_fit = _pace_fit(destination, profile)

    breakdown = {
        "desired_outcomes": SCORE_WEIGHTS["desired_outcomes"] * len(matched_goals) / len(profile.goals),
        "experience_interests": SCORE_WEIGHTS["experience_interests"] * len(matched_moments) / len(profile.moments),
        "settings": float(SCORE_WEIGHTS["settings"]) if (profile.surprise_me or matched_environments) else 0.0,
        "pace": SCORE_WEIGHTS["pace"] * _PACE_POINTS[pace_fit],
        "travel_distance": _distance_points(distance),
        "prefer_avoid_penalty": 0.0,
    }

    tradeoffs: List[str] = []
    for check in checks:
        if check.severity == "prefer_avoid" and check.status == "conflict":
            breakdown["prefer_avoid_penalty"] -= PREFER_AVOID_PENALTY
            tradeoffs.append(
                f"'{check.label}' is marked as prefer to avoid, and the catalog flags it here, so this option ranks lower."
            )

    total = max(0.0, round(sum(breakdown.values()), 2))
    breakdown = {key: round(value, 2) for key, value in breakdown.items()}

    return ScoredDestination(
        destination=destination,
        total_score=total,
        breakdown=breakdown,
        matched_goals=matched_goals,
        matched_moments=matched_moments,
        matched_environments=matched_environments,
        setting_category=_setting_category(destination),
        pace_fit=pace_fit,
        restriction_checks=checks,
        distance=distance,
        tradeoffs=tradeoffs,
    ), None


def _data_gaps(
    profile: TripProfile, origin: Optional[Origin], catalog: tuple, looked_up: Dict[str, Tuple[float, float]]
) -> List[str]:
    gaps: List[str] = []
    for goal in profile.goals:
        if not any(goal in destination.goals for destination in catalog):
            gaps.append(
                f"No candidate destination is tagged for {TRIP_GOAL_LABELS[goal]} yet, so ranking relied on your other answers."
            )
    if all(destination.verified_nightly_usd is None for destination in catalog):
        gaps.append(
            "No candidate has a verified nightly price yet, so budget could not filter destinations. "
            "Your budget will be applied when live stays are checked for exact dates."
        )
    if profile.travel_distance != "anywhere":
        if origin is None or not origin.has_coordinates:
            gaps.append("Your departure point could not be located, so travel distance was not checked.")
        else:
            missing = sum(
                1 for destination in catalog
                if (destination.latitude is None or destination.longitude is None)
                and destination.destination_id not in looked_up
            )
            if missing:
                gaps.append(f"{missing} candidates could not be located on the map, so their distance from you was not checked.")
    if season_months_are_assumed(profile):
        gaps.append("Season names were read as Northern-hemisphere months for heat and cold checks.")
    return gaps


def rank_destinations(
    profile: TripProfile,
    origin: Optional[Origin],
    looked_up_coordinates: Optional[Dict[str, Tuple[float, float]]] = None,
) -> MatchResult:
    """
    `looked_up_coordinates`: {destination_id: (lat, lng)} from map lookups for
    catalog rows without coordinates (see destination_places.py). Only used
    for the distance check; catalog coordinates always take precedence.
    """
    looked_up = looked_up_coordinates or {}
    catalog = load_destination_candidates()
    ranked: List[ScoredDestination] = []
    exclusions: List[Exclusion] = []
    for destination in catalog:
        scored, exclusion = score_destination(destination, profile, origin, looked_up.get(destination.destination_id))
        if exclusion is not None:
            exclusions.append(exclusion)
        else:
            ranked.append(scored)
    # destination_id breaks ties so ordering is stable and reproducible.
    ranked.sort(key=lambda item: (-item.total_score, item.destination_id))
    return MatchResult(
        ranked=ranked,
        exclusions=exclusions,
        total_candidate_count=len(catalog),
        data_gaps=_data_gaps(profile, origin, catalog, looked_up),
    )


# ==================== SELECTION ====================

def select_diverse(
    ranked: List[ScoredDestination],
    exclude_ids: Set[str] = frozenset(),
    limit: int = SUGGESTION_COUNT,
) -> List[ScoredDestination]:
    """Best-first shortlist with distinct countries and, where close in score, varied settings."""
    pool = [item for item in ranked if item.destination_id not in exclude_ids]
    chosen: List[ScoredDestination] = []
    used_countries: Set[str] = set()
    used_settings: Set[str] = set()

    while pool and len(chosen) < limit:
        eligible = [item for item in pool if item.destination.country not in used_countries]
        if not eligible:
            break
        pick = eligible[0]
        if pick.setting_category in used_settings:
            for alternative in eligible[1:]:
                if pick.total_score - alternative.total_score > DIVERSITY_SCORE_WINDOW:
                    break
                if alternative.setting_category not in used_settings:
                    pick = alternative
                    break
        chosen.append(pick)
        used_countries.add(pick.destination.country)
        used_settings.add(pick.setting_category)
        pool.remove(pick)

    # Fewer distinct countries than slots: fill with the next best options.
    for item in pool:
        if len(chosen) >= limit:
            break
        chosen.append(item)
    chosen.sort(key=lambda item: (-item.total_score, item.destination_id))
    return chosen


# ==================== EXPLANATION ====================

def _reasons(item: ScoredDestination, profile: TripProfile) -> List[str]:
    destination = item.destination
    reasons: List[str] = []
    if item.matched_goals:
        reasons.append(
            f"You chose {_join_labels(profile.goal_labels)}; our editors tag {destination.destination} for "
            f"{_join_labels([TRIP_GOAL_LABELS[goal] for goal in item.matched_goals])}. "
            "This is an editorial hypothesis, not a promised outcome."
        )
    if item.matched_moments:
        picked = _join_labels([f"'{PREFERRED_MOMENT_LABELS[moment]}'" for moment in item.matched_moments])
        reasons.append(f"You picked {picked}; the catalog describes it as: {destination.experience_context}.")
    if item.matched_environments:
        reasons.append(
            f"You're drawn to {_join_labels([ENVIRONMENT_LABELS[env] for env in item.matched_environments])}; "
            f"catalog setting: {destination.setting_context}."
        )
    elif item.pace_fit == "match":
        reasons.append(
            f"Its catalog pace ({destination.catalog_pace}) fits '{TRIP_PACE_LABELS[profile.pace]}'."
        )
    return reasons[:3]


def _tradeoffs(item: ScoredDestination, profile: TripProfile) -> List[str]:
    destination = item.destination
    tradeoffs = list(item.tradeoffs)
    if destination.tradeoffs_to_check:
        tradeoffs.insert(0, f"Check before booking: {destination.tradeoffs_to_check}.")
    missing_goals = [goal for goal in profile.goals if goal not in item.matched_goals]
    if missing_goals:
        tradeoffs.append(
            f"Not tagged for {_join_labels([TRIP_GOAL_LABELS[goal] for goal in missing_goals])} in the catalog."
        )
    if item.pace_fit == "mismatch":
        tradeoffs.append(
            f"Catalog pace is {destination.catalog_pace}, while you asked for '{TRIP_PACE_LABELS[profile.pace]}'; "
            "the itinerary would need to be paced deliberately."
        )
    for check in item.restriction_checks:
        if check.status == "seasonal_outside_dates":
            tradeoffs.append(f"{check.label}: {check.detail}")
    return tradeoffs


def _unresolved_facts(item: ScoredDestination, profile: TripProfile, origin: Optional[Origin]) -> List[str]:
    destination = item.destination
    facts = [
        "Source review pending: catalog claims have not yet been checked against a destination-specific source.",
    ]
    if profile.has_exact_dates:
        facts.append("Live stay availability and price have not been checked for your dates yet.")
    else:
        facts.append(
            "Availability and prices need exact check-in and check-out dates; month or flexible timing is inspiration only."
        )
    if destination.verified_nightly_usd is None:
        facts.append(
            f"Nightly lodging cost is unknown, not yet compared with your ${profile.budget_per_night:,.0f} "
            f"per room, per night budget ({profile.rooms} room{'s' if profile.rooms != 1 else ''})."
        )
    if profile.children and profile.child_ages is None:
        facts.append("Child ages are needed before live rates can be quoted.")
    if profile.children:
        facts.append("Suitability for children has not been verified.")
    if destination.catalog_pace is None:
        facts.append("The destination's pace depends on the plan; we'll shape it to your chosen pace.")
    departure = profile.departure_location if origin is None or origin.status != "UNVERIFIED" else "your departure point"
    facts.append(f"Route from {departure} (mode, transfers and travel time) has not been checked.")
    for check in item.restriction_checks:
        if check.status in {"unverified", "itinerary_stage"}:
            facts.append(f"{check.label}: {check.detail}")
    facts.append(
        "Entry requirements, safety and accessibility must be checked against current official guidance for your travel details."
    )
    return facts


def build_suggestion(item: ScoredDestination, profile: TripProfile, origin: Optional[Origin]) -> dict:
    """Guest-facing suggestion dict (before photo/coordinate enrichment)."""
    destination = item.destination
    reasons = _reasons(item, profile)
    tradeoffs = _tradeoffs(item, profile)
    return {
        "destination_id": destination.destination_id,
        "city_name": destination.destination,
        "country_name": destination.country,
        "world_region": destination.world_region,
        "number_of_days": profile.nights,
        "description": " ".join(reasons[:2]),
        "latitude": destination.latitude,
        "longitude": destination.longitude,
        "match_score": round(item.total_score),
        "score_breakdown": item.breakdown,
        "match_reasons": reasons,
        "tradeoffs": tradeoffs,
        "unresolved_facts": _unresolved_facts(item, profile, origin),
        # Kept for clients that read the pre-intake `warnings` field.
        "warnings": tradeoffs,
        "restriction_checks": [check.to_dict() for check in item.restriction_checks],
        "distance_check": item.distance.to_dict(),
        "verification": {
            "candidate_status": destination.candidate_status,
            "source_review_status": destination.source_review_status,
            "live_lodging_status": destination.live_lodging_status,
            "verified_nightly_usd": destination.verified_nightly_usd,
            "budget_status": "UNKNOWN" if destination.verified_nightly_usd is None else "WITHIN_BUDGET",
            "price_claim": "NONE",
            "availability_claim": "NONE",
            "guest_display_gate": destination.guest_display_gate,
        },
        "evidence": {
            "destination_source_url": destination.destination_source_url,
            "source_scope": destination.source_scope,
            "source_last_checked_utc": destination.source_last_checked_utc,
            "editorial_evidence_limit": destination.editorial_evidence_limit,
        },
    }


# ==================== NO VALID RESULT ====================

_FLEX_FIELDS = {
    "travel_distance": "travel_distance",
    "budget": "budget_per_night",
    "no_grounded_fit": "trip_goals",
}


def _blocking_constraint(reason: str, count: int, profile: TripProfile) -> dict:
    if reason.startswith(("must_avoid:", "unverified_high_impact:")):
        kind, restriction = reason.split(":", 1)
        label = RESTRICTION_LABELS[restriction]
        if kind == "unverified_high_impact":
            explanation = (
                f"'{label}' is marked as something you must plan around, and {count} destinations have no "
                "verified information about it yet. We won't treat unknown as suitable."
            )
        else:
            explanation = f"'{label}' is marked as must avoid, and the catalog flags it at {count} destinations."
        return {
            "constraint": reason,
            "field": f"restriction_severity_{restriction}",
            "excluded_count": count,
            "explanation": explanation,
        }
    if reason == "travel_distance":
        explanation = (
            f"'{TRAVEL_DISTANCE_LABELS[profile.travel_distance]}' ruled out {count} destinations that are "
            "farther than that from your departure point, or in another world region with no measurable distance."
        )
    elif reason == "budget":
        explanation = f"{count} destinations have a verified nightly cost above your budget."
    else:
        explanation = f"{count} destinations match none of your desired feelings or chosen moments."
    return {
        "constraint": reason,
        "field": _FLEX_FIELDS.get(reason, reason),
        "excluded_count": count,
        "explanation": explanation,
    }


def build_no_valid_result(result: MatchResult, profile: TripProfile) -> dict:
    """IMPORT_RULES.csv "No valid result": name the blocking constraints and ask which may flex."""
    counts = sorted(result.excluded_by_reason().items(), key=lambda item: (-item[1], item[0]))
    blocking = [_blocking_constraint(reason, count, profile) for reason, count in counts]
    high_impact = [item for item in blocking if item["constraint"].startswith("unverified_high_impact:")]
    if high_impact:
        question = (
            "Could you tell us more about what you need (in 'Other planning needs'), so we can check specific "
            "stays and activities? Or, if it's workable, mark it as 'I'd prefer to avoid this' and we'll show "
            "options with the checks still to do."
        )
    else:
        question = "No destination fits every answer. Which of these could flex?"
    return {
        "blocking_constraints": blocking,
        "question": question,
        "relaxed_automatically": False,
    }
