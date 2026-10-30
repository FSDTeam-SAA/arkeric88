"""
Deterministic destination matching for the Velari intake.

Pipeline (IMPORT_RULES.csv), with no LLM anywhere in it:
1. Hard exclusions before ranking: must-avoid conflicts, straight-line
   distance beyond the guest's tolerance, and a verified nightly price above
   budget. Budget is never relaxed silently.
2. Weighted scoring: desired outcomes (primary), experience interests,
   setting, pace and travel fit. Adjustments, each with an explained
   tradeoff: prefer-avoid conflicts, unconfirmed must-avoid needs, travel
   effort against trip length, estimated price against budget, and the
   heuristic catalogue rows.
3. Diverse selection of 1-3 options: no near-duplicates (a country may repeat
   only for a clearly different place), varied settings.
4. Explanations tied to the guest's own answer labels and the specific
   catalog field that was checked, plus tradeoffs and unresolved facts.

Nothing here upgrades a catalog row's verification status: every suggestion
stays a CANDIDATE_ONLY editorial hypothesis until source review and live
checks happen.
"""

import hashlib
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Dict, Iterable, List, Optional, Set, Tuple

from src.core.destination_catalog import Destination, load_destination_candidates
from src.core.geography import country_key, great_circle_km, is_southern_hemisphere
from src.core.guest_text import dedupe, from_pipes, lower_first, natural_list
from src.core.intake_mappings import (
    BUDGET_ABOVE_PENALTY,
    BUDGET_STRETCH_PENALTY,
    ENVIRONMENT_KEYWORDS,
    ENVIRONMENT_LABELS,
    GOAL_EXPERIENCE_KEYWORDS,
    HIGH_IMPACT_DESTINATION_RESTRICTIONS,
    ITINERARY_LEVEL_RESTRICTIONS,
    LOCATION_ENRICHED_PENALTY,
    MATCH_LABELS,
    MAX_SUGGESTIONS_PER_COUNTRY,
    MOMENT_GOAL_SIGNALS,
    MOMENT_KEYWORDS,
    MOMENT_PACE_SIGNALS,
    PREFER_AVOID_PENALTY,
    PREFERRED_MOMENT_LABELS,
    RESTRICTION_CONFLICT_RULES,
    RESTRICTION_LABELS,
    RESTRICTION_RISK_SEASON,
    SAME_COUNTRY_MIN_SEPARATION_KM,
    SCORE_WEIGHTS,
    SUGGESTION_COUNT,
    TRAVEL_DISTANCE_LABELS,
    TRAVEL_DISTANCE_MAX_STRAIGHT_LINE_KM,
    TRAVEL_EFFORT_MAX_PENALTY,
    TRAVEL_EFFORT_MIN_NIGHTS,
    TRIP_GOAL_LABELS,
    TRIP_PACE_LABELS,
    CATALOG_PACE_ORDER,
    UNVERIFIED_MUST_AVOID_PENALTY,
)
from src.core.origin import Origin
from src.core.price_estimates import (
    FITS,
    LIKELY_ABOVE,
    MAY_STRETCH,
    UNKNOWN,
    PriceEstimate,
    budget_fit,
    load_price_estimates,
    price_estimate,
)
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
    # Orders equal scores; see _tie_breaker.
    tie_breaker: str = ""
    price: Optional[PriceEstimate] = None
    budget_fit: str = UNKNOWN
    # Fewest nights that make the journey worthwhile; None when distance is unknown.
    suggested_min_nights: Optional[int] = None
    # Catalog coordinates, or map-lookup ones for a row without any.
    point: Optional[Tuple[float, float]] = None

    @property
    def destination_id(self) -> str:
        return self.destination.destination_id

    @property
    def rank_key(self) -> tuple:
        return (-self.total_score, self.destination.is_location_enriched, self.tie_breaker, self.destination_id)


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
            "We check this for each restaurant, stay and activity when your itinerary is built; "
            "please confirm it with each provider too.",
        )

    rule = RESTRICTION_CONFLICT_RULES.get(restriction)
    if rule is None:  # "other" -- free-text need the catalog cannot check.
        return RestrictionCheck(
            restriction, label, severity, "unverified",
            "Tell us more about this need so we can plan around it for this destination.",
        )

    text = _combined_text(destination, rule["fields"])
    if not _matches(rule["patterns"], text):
        if restriction in HIGH_IMPACT_DESTINATION_RESTRICTIONS:
            return RestrictionCheck(
                restriction, label, severity, "unverified",
                "We don't have confirmed information about this here yet; we'll check it before you book.",
            )
        return RestrictionCheck(
            restriction, label, severity, "no_catalog_concern",
            "No known concern here; we'll still confirm it for your trip.",
        )

    concern = destination.specific_tradeoffs or destination.setting_context
    if (
        _matches(rule["seasonal"], text)
        and profile.travel_months
        and restriction in RESTRICTION_RISK_SEASON
        and not set(profile.travel_months) & set(_risk_months_for(restriction, destination))
    ):
        return RestrictionCheck(
            restriction, label, severity, "seasonal_outside_dates",
            f"{concern} is usually a concern outside your travel months; check conditions for your dates.",
        )
    return RestrictionCheck(
        restriction, label, severity, "conflict",
        f"Worth knowing: {concern}.",
    )


def _is_hard_exclusion(check: RestrictionCheck) -> bool:
    return check.severity == "must_avoid" and check.status == "conflict"


def _is_unverified_must_avoid(check: RestrictionCheck) -> bool:
    """Unknown high-impact suitability is not a pass: ranked lower and stated, never shown as suitable."""
    return (
        check.severity == "must_avoid"
        and check.status == "unverified"
        and check.restriction in HIGH_IMPACT_DESTINATION_RESTRICTIONS
    )


def check_distance(
    destination: Destination,
    profile: TripProfile,
    origin: Optional[Origin],
    looked_up: Optional[Tuple[float, float]] = None,
) -> DistanceCheck:
    """`looked_up`: map-lookup coordinates for a catalog row that has none."""
    limit_km = TRAVEL_DISTANCE_MAX_STRAIGHT_LINE_KM[profile.travel_distance]
    if limit_km is None:
        km = _straight_line_km(destination, origin, looked_up)
        if km is None and origin is not None and origin.has_coordinates:
            # Unmeasured while others are measured: half credit, as for any unknown
            # distance, so it can't outrank a measured option by escaping the effort check.
            return DistanceCheck("not_checked", None, "We couldn't place this destination on the map, so its distance from you was not checked.")
        if km is None:
            return DistanceCheck("no_limit", None, "You're open to anywhere; route and travel time not checked.")
        return DistanceCheck(
            "no_limit", km,
            f"About {km:,} km in a straight line; the actual route, mode and transfers have not been checked.",
        )
    if origin is None or not origin.provided:
        return DistanceCheck(
            "not_checked", None, "Add your departure city whenever you know it, and we'll check the distance from you.",
        )
    if not origin.has_coordinates:
        if (
            profile.travel_distance == "nearby"
            and origin.region
            and destination.world_region.strip().lower() != origin.region
        ):
            # The departure city wasn't matched on the map, but its country was
            # named: a different world region is certainly not "nearby".
            return DistanceCheck(
                "beyond_tolerance", None,
                f"In a different world region ({destination.world_region}) from your departure country.",
            )
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
        return DistanceCheck("not_checked", None, "We couldn't place this destination on the map, so its distance from you was not checked.")

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


def _straight_line_km(
    destination: Destination, origin: Optional[Origin], looked_up: Optional[Tuple[float, float]]
) -> Optional[int]:
    if origin is None or not origin.has_coordinates:
        return None
    latitude, longitude = destination.latitude, destination.longitude
    if (latitude is None or longitude is None) and looked_up is not None:
        latitude, longitude = looked_up
    if latitude is None or longitude is None:
        return None
    return round(great_circle_km(origin.latitude, origin.longitude, latitude, longitude))


def suggested_min_nights(km: float) -> int:
    """The fewest nights that make a journey of this straight-line length worthwhile."""
    for limit_km, nights in TRAVEL_EFFORT_MIN_NIGHTS:
        if limit_km is None or km <= limit_km:
            return nights
    return TRAVEL_EFFORT_MIN_NIGHTS[-1][1]


def _travel_points(check: DistanceCheck, nights: int) -> Tuple[float, float, Optional[int]]:
    """
    (travel points, travel-effort penalty, suggested minimum nights). A measured
    journey that is long for the trip earns part of the travel points and a
    penalty in proportion to the shortfall: 5 nights on a 9-night journey keeps
    5/9 of the points and loses 4/9 of the maximum penalty. It ranks lower; it
    is never excluded for it.
    """
    weight = SCORE_WEIGHTS["travel_distance"]
    if check.status not in {"no_limit", "within_straight_line_limit"}:
        return weight / 2, 0.0, None  # Distance unknown: half credit.
    if check.straight_line_km is None:
        return float(weight), 0.0, None
    minimum = suggested_min_nights(check.straight_line_km)
    ratio = min(1.0, nights / minimum)
    return weight * ratio, -TRAVEL_EFFORT_MAX_PENALTY * (1 - ratio), minimum


def _setting_category(destination: Destination) -> str:
    for text in (destination.setting_context, _combined_text(destination, _MATCH_TEXT_FIELDS)):
        for environment, fragments in ENVIRONMENT_KEYWORDS.items():
            if _matches(fragments, text):
                return environment
    return "other"


def _pace_fit(destination: Destination, profile: TripProfile) -> str:
    """Closest of the destination's paces ("Gentle|Balanced" suits both) to the guest's pace."""
    if not destination.catalog_paces:
        return "to_confirm"
    wanted = CATALOG_PACE_ORDER.index(profile.catalog_pace)
    gap = min(abs(CATALOG_PACE_ORDER.index(pace) - wanted) for pace in destination.catalog_paces)
    return {0: "match", 1: "adjacent"}.get(gap, "mismatch")


_PACE_POINTS = {"match": 1.0, "adjacent": 0.5, "to_confirm": 0.5, "mismatch": 0.0}


def _matched_moments(destination: Destination) -> Dict[str, bool]:
    text = _combined_text(destination, _MATCH_TEXT_FIELDS)
    matched = {}
    for moment, fragments in MOMENT_KEYWORDS.items():
        matched[moment] = (
            _matches(fragments, text)
            or bool(set(destination.catalog_paces) & MOMENT_PACE_SIGNALS.get(moment, set()))
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
            return None, Exclusion(destination.destination_id, f"must_avoid:{check.restriction}", check.detail)

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
    goal_share = sum(profile.goal_weight(goal) * destination.goal_strength(goal) for goal in profile.goals)
    moment_hits = _matched_moments(destination)
    matched_moments = [moment for moment in profile.moments if moment_hits[moment]]
    if not goal_share and not matched_moments:
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
    travel_points, travel_penalty, minimum_nights = _travel_points(distance, profile.nights)
    estimate = price_estimate(destination.destination_id)
    fit = budget_fit(estimate, profile.budget_per_night, profile.budget_open_ended)

    breakdown = {
        "desired_outcomes": SCORE_WEIGHTS["desired_outcomes"] * goal_share,
        "experience_interests": SCORE_WEIGHTS["experience_interests"] * len(matched_moments) / len(profile.moments),
        "settings": float(SCORE_WEIGHTS["settings"]) if (profile.surprise_me or matched_environments) else 0.0,
        "pace": SCORE_WEIGHTS["pace"] * _PACE_POINTS[pace_fit],
        "travel_distance": travel_points,
        "prefer_avoid_penalty": 0.0,
        "unverified_must_avoid_penalty": 0.0,
        "travel_effort_penalty": travel_penalty,
        "budget_penalty": {MAY_STRETCH: -float(BUDGET_STRETCH_PENALTY), LIKELY_ABOVE: -float(BUDGET_ABOVE_PENALTY)}.get(fit, 0.0),
        # IMPORT_RULES.csv "Destination matching confidence": heuristic tags rank lower.
        "evidence_penalty": -float(LOCATION_ENRICHED_PENALTY) if destination.is_location_enriched else 0.0,
    }

    tradeoffs: List[str] = []
    for check in checks:
        if check.severity == "prefer_avoid" and check.status == "conflict":
            breakdown["prefer_avoid_penalty"] -= PREFER_AVOID_PENALTY
            tradeoffs.append(
                f"You marked '{check.label}' as a preference, and it may be harder to keep to here, so this option ranks lower."
            )
        elif _is_unverified_must_avoid(check):
            breakdown["unverified_must_avoid_penalty"] -= UNVERIFIED_MUST_AVOID_PENALTY
            tradeoffs.append(
                f"You told us '{check.label}' is something you must plan around. We don't have confirmed information "
                "about it here yet, so this option ranks lower until we check each stay and activity for you."
            )
    if minimum_nights is not None and profile.nights < minimum_nights:
        tradeoffs.append(
            f"A long journey for a {profile.nights}-night trip: about {distance.straight_line_km:,} km from "
            f"{_home(profile)} in a straight line, so travel takes a large share of your time. It suits "
            f"{minimum_nights} nights or more best."
        )
    if fit == LIKELY_ABOVE:
        tradeoffs.append(
            f"Likely above your ${profile.budget_per_night:,.0f} nightly budget: stays here are estimated at about "
            f"${estimate.low:,.0f}-${estimate.high:,.0f} per room, per night (an estimate, not a quote)."
        )
    elif fit == MAY_STRETCH:
        tradeoffs.append(
            f"May stretch your ${profile.budget_per_night:,.0f} nightly budget: typical stays here are estimated at "
            f"about ${estimate.typical:,.0f} per room, per night, with some from ${estimate.low:,.0f}."
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
        price=estimate,
        budget_fit=fit,
        suggested_min_nights=minimum_nights,
        point=(
            (destination.latitude, destination.longitude)
            if destination.latitude is not None and destination.longitude is not None
            else looked_up
        ),
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
    if not load_price_estimates() and all(destination.verified_nightly_usd is None for destination in catalog):
        gaps.append(
            "No destination has a nightly price yet, so budget could not shape these suggestions. "
            "Your budget will be applied when live stays are checked for exact dates."
        )
    if profile.travel_distance != "anywhere":
        if origin is None or not origin.provided:
            gaps.append("No departure location yet, so travel distance was not checked.")
        elif not origin.has_coordinates:
            gaps.append(
                "Your departure point could not be matched on the map, so travel distance was not measured"
                + (f"; 'Nearby' was limited to {origin.region.title()}." if origin.region and profile.travel_distance == "nearby" else ".")
            )
        else:
            missing = sum(
                1 for destination in catalog
                if (destination.latitude is None or destination.longitude is None)
                and destination.destination_id not in looked_up
            )
            if missing:
                gaps.append(
                    f"{missing} destination{' was' if missing == 1 else 's were'} not located on the map, "
                    "so distance from you was not checked for them."
                )
    if season_months_are_assumed(profile):
        gaps.append("Season names were read as Northern-hemisphere months for heat and cold checks.")
    return gaps


def _answers_seed(profile: TripProfile) -> str:
    """The answers that shape a match, as one stable string."""
    return "|".join([
        ",".join(profile.goals), ",".join(sorted(profile.moments)), ",".join(sorted(profile.environments)),
        profile.pace, profile.travel_distance, profile.departure_location or "",
        ",".join(f"{code}={severity}" for code, severity in sorted(profile.restrictions.items())),
        profile.travel_timing, profile.travel_period or "", str(profile.nights),
    ])


def _tie_breaker(seed: str, destination_id: str) -> str:
    """
    Equal scores were once ordered by catalog ID, so the same few rows won
    every tie for every guest. A hash of the guest's answers and the ID
    varies the order between different answers while the same answers
    always give the same result.
    """
    return hashlib.sha256(f"{seed}|{destination_id}".encode("utf-8")).hexdigest()


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
    seed = _answers_seed(profile)
    ranked: List[ScoredDestination] = []
    exclusions: List[Exclusion] = []
    for destination in catalog:
        scored, exclusion = score_destination(destination, profile, origin, looked_up.get(destination.destination_id))
        if exclusion is not None:
            exclusions.append(exclusion)
        else:
            scored.tie_breaker = _tie_breaker(seed, destination.destination_id)
            ranked.append(scored)
    ranked.sort(key=lambda item: item.rank_key)
    return MatchResult(
        ranked=ranked,
        exclusions=exclusions,
        total_candidate_count=len(catalog),
        data_gaps=_data_gaps(profile, origin, catalog, looked_up),
    )


# ==================== SELECTION ====================

def _repeats(item: ScoredDestination, chosen: List[ScoredDestination]) -> bool:
    """
    True when `item` would make the shortlist repetitive: its country is
    already used twice, or it is in the same country as a pick it is close to
    or shares a setting with. Unknown coordinates count as close.
    """
    country = country_key(item.destination.country)
    same_country = [other for other in chosen if country_key(other.destination.country) == country]
    if not same_country:
        return False
    if len(same_country) >= MAX_SUGGESTIONS_PER_COUNTRY:
        return True
    for other in same_country:
        if other.setting_category == item.setting_category:
            return True
        if item.point is None or other.point is None or great_circle_km(*item.point, *other.point) < SAME_COUNTRY_MIN_SEPARATION_KM:
            return True
    return False


def select_diverse(
    ranked: List[ScoredDestination],
    exclude_ids: Set[str] = frozenset(),
    limit: int = SUGGESTION_COUNT,
) -> List[ScoredDestination]:
    """
    Best-first shortlist without near-duplicates and, where close in score,
    with varied settings. Two strong options may share a country when they
    are clearly different places (see _repeats); when too few qualify, fewer
    options are returned.
    """
    pool = [item for item in ranked if item.destination_id not in exclude_ids]
    chosen: List[ScoredDestination] = []
    used_settings: Set[str] = set()

    while pool and len(chosen) < limit:
        eligible = [item for item in pool if not _repeats(item, chosen)]
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
        used_settings.add(pick.setting_category)
        pool.remove(pick)

    chosen.sort(key=lambda item: item.rank_key)
    return chosen


# ==================== EXPLANATION ====================

_GENERIC_SETTINGS = {"regional destination", "destination", "region"}
_TAG_SPLIT = re.compile(r"\s*(?:\||,|;|\band\b)\s*", re.IGNORECASE)


def _tags(value: str) -> List[str]:
    """Catalog "Art|Nature|Food" or "Architecture, culture, and coast" -> ["art", "nature", "food"]."""
    return [tag.strip().lower() for tag in _TAG_SPLIT.split(value or "") if tag and tag.strip()]


def _rationale_phrase(destination: Destination) -> str:
    """
    The rationale's first sentence. Staff wording is already gone
    (Destination.guest_rationale); a sentence that starts with the place's
    name keeps its capital ("Shanghai offers ...").
    """
    first = re.split(r"(?<=[.!?])\s+", destination.guest_rationale.strip(), maxsplit=1)[0].rstrip(" .;,")
    if not first:
        return ""
    if first.split()[0] in destination.destination.split():
        return first
    return lower_first(first)


# Vowel letters with a consonant sound ("a university town") and the reverse ("an hour").
_A_BEFORE = ("uni", "use", "usu", "eu", "one", "once")
_AN_BEFORE = ("hour", "honest", "heir")


def _article(phrase: str) -> str:
    word = phrase.lower()
    if word.startswith(_AN_BEFORE):
        return "an"
    if word.startswith(_A_BEFORE):
        return "a"
    return "an" if word[:1] in "aeiou" else "a"


def _setting_phrase(destination: Destination) -> str:
    """The catalog setting in guest words ("town and jungle"), or "" when generic."""
    return natural_list([tag for tag in _tags(destination.setting_context) if tag not in _GENERIC_SETTINGS])


def _introduction(destination: Destination) -> str:
    """
    What the place is and what a stay there offers -- no matching language.
    A rationale that already describes the place by name is used as written.
    """
    rationale = _rationale_phrase(destination)
    if rationale and rationale.split()[0] in destination.destination.split():
        return f"{rationale}."
    experiences = natural_list(_tags(destination.experience_context))
    settings = _setting_phrase(destination)
    setting = f", in {_article(settings)} {settings} setting" if settings else ""
    if experiences:
        return f"{destination.destination} is known for {experiences}{setting}."
    if rationale:
        return f"{destination.destination}: {rationale}."
    return ""


def _home(profile: TripProfile) -> str:
    """The guest's own name for where they leave from ("Austin" from "Austin, Texas")."""
    return (profile.departure_location or "").split(",")[0].strip() or "your departure point"


def _moment_evidence(destination: Destination, moment: str) -> Optional[str]:
    """What at this destination backs a chosen moment: its own tags, else its pace."""
    tags = [tag for tag in _tags(destination.experience_context) if _matches(MOMENT_KEYWORDS[moment], tag)]
    if tags:
        return natural_list(tags[:2])
    paces = set(destination.catalog_paces) & MOMENT_PACE_SIGNALS.get(moment, set())
    if paces:
        return f"a {sorted(paces)[0].lower()} pace"
    return None


def _goal_experiences(destination: Destination, goal: str) -> List[str]:
    tags = _tags(destination.experience_context) + [
        tag for tag in _tags(destination.setting_context) if tag not in _GENERIC_SETTINGS
    ]
    return [tag for tag in dict.fromkeys(tags) if _matches(GOAL_EXPERIENCE_KEYWORDS[goal], tag)][:3]


def _goal_reasons(item: ScoredDestination) -> List[str]:
    """
    One line per group of selected goals, naming the destination's own
    experiences that support them ("For restoration and reflection: yoga,
    nature and jungle."). Goals backed by the same experiences share a line.
    """
    destination = item.destination
    groups: Dict[tuple, List[str]] = {}
    for goal in item.matched_goals:
        groups.setdefault(tuple(_goal_experiences(destination, goal)), []).append(goal)
    # The rationale backs a goal only when the introduction doesn't already say it.
    rationale = _rationale_phrase(destination)
    fallback = "" if rationale and rationale in _introduction(destination) else rationale
    lines = []
    for experiences, goals in groups.items():
        labels = natural_list([TRIP_GOAL_LABELS[goal].lower() for goal in goals])
        support = natural_list(experiences) or fallback
        lines.append(f"For {labels}: {support}." if support else f"Suited to {labels}.")
    return lines


def _reasons(item: ScoredDestination, profile: TripProfile) -> List[str]:
    """
    Guest-facing reasons tied to the guest's own answers, each saying something
    the introduction doesn't: which experiences support the selected goals,
    which chosen moments and setting it fits, and when the journey suits the
    trip length. Never catalog tags, pipe-separated lists or "our editors".
    """
    destination = item.destination
    reasons: List[str] = _goal_reasons(item)
    if item.matched_moments:
        parts = []
        for moment in item.matched_moments:
            label = PREFERRED_MOMENT_LABELS[moment].lower()
            evidence = _moment_evidence(destination, moment)
            parts.append(f"{label} ({evidence})" if evidence and evidence not in label else label)
        reasons.append(f"It has what you're drawn to: {'; '.join(parts)}.")
    if item.matched_environments:
        setting = _setting_phrase(destination)
        wanted = natural_list([ENVIRONMENT_LABELS[env].lower() for env in item.matched_environments])
        reasons.append(f"The {setting} setting suits your preference for {wanted}." if setting else f"It suits your preference for {wanted}.")
    elif item.pace_fit == "match" and destination.catalog_pace:
        reasons.append(f"Its {destination.catalog_pace.lower()} pace fits how you like to travel.")
    if (
        item.suggested_min_nights is not None
        and item.distance.straight_line_km is not None
        and item.suggested_min_nights <= profile.nights
        and item.distance.straight_line_km <= 4000
    ):
        reasons.append(
            f"About {item.distance.straight_line_km:,} km from {_home(profile)} in a straight line, "
            f"a comfortable distance for {profile.nights} nights."
        )
    return reasons[:4]


def match_label(score: float) -> str:
    return next(label for threshold, label in MATCH_LABELS if score >= threshold)


def _tradeoffs(item: ScoredDestination, profile: TripProfile) -> List[str]:
    """Each consideration once, in guest language."""
    destination = item.destination
    tradeoffs = list(item.tradeoffs)
    if destination.specific_tradeoffs:
        tradeoffs.insert(0, f"Worth knowing: {lower_first(destination.specific_tradeoffs.rstrip('.'))}.")
    missing_goals = [goal for goal in profile.goals if goal not in item.matched_goals]
    if missing_goals:
        missing = natural_list([TRIP_GOAL_LABELS[goal].lower() for goal in missing_goals], "or")
        matched = natural_list([TRIP_GOAL_LABELS[goal].lower() for goal in item.matched_goals])
        tradeoffs.append(
            f"Offers less for {missing} than for {matched}." if matched
            else f"Offers less for {missing} than your other options; it was chosen for the experiences you picked."
        )
    setting = _setting_phrase(destination)
    if not profile.surprise_me and not item.matched_environments and setting:
        wanted = ", ".join(ENVIRONMENT_LABELS[env].lower() for env in profile.environments)
        tradeoffs.append(f"Its {setting} setting isn't one you picked ({wanted}), so it ranks lower for setting.")
    if item.pace_fit == "mismatch":
        tradeoffs.append(
            f"It's usually a {destination.catalog_pace.lower()} destination, so we'd pace your days deliberately "
            f"to match your preferred pace ({TRIP_PACE_LABELS[profile.pace].lower()})."
        )
    for check in item.restriction_checks:
        if check.status == "seasonal_outside_dates":
            tradeoffs.append(f"{check.label}: {check.detail}")
    return dedupe(tradeoffs)


def _unresolved_facts(item: ScoredDestination, profile: TripProfile, origin: Optional[Origin]) -> List[str]:
    """
    Only what the guest needs to know or do, each once. Internal review status
    (source review, catalog price gaps, route checks) lives in `verification`.
    """
    facts = []
    if item.price is not None:
        facts.append(
            f"Estimated stays: about ${item.price.low:,.0f}-${item.price.high:,.0f} per room, per night "
            f"(typically ${item.price.typical:,.0f}). This is an estimate, not a quote; "
            + ("we'll confirm real prices for your dates before booking." if profile.has_exact_dates
               else "real prices are checked once you choose dates.")
        )
    elif profile.has_exact_dates:
        facts.append("Prices and availability will be confirmed for your dates before booking.")
    else:
        facts.append("Prices and availability are checked once you choose dates.")
    if profile.children and profile.child_ages is None:
        facts.append("Share the children's ages so we can check rates and age-suitable activities.")
    elif profile.children:
        facts.append("We'll check that each activity suits the children in your party.")
    if not profile.departure_known:
        facts.append("Add your departure city to see the travel distance and time from home.")
    for check in item.restriction_checks:
        if check.status in {"unverified", "itinerary_stage"}:
            label = check.label
            if check.restriction == "mobility_accessibility" and profile.accessibility_need_labels:
                label = f"{label} ({', '.join(profile.accessibility_need_labels).lower()})"
            facts.append(f"{label}: {check.detail}")
    return dedupe(facts)


def _internal_notes(item: ScoredDestination, profile: TripProfile, origin: Optional[Origin]) -> List[str]:
    """Review status kept for the team; never shown to guests."""
    destination = item.destination
    notes = ["Source review pending: catalog claims have not yet been checked against a destination-specific source."]
    if destination.verified_nightly_usd is None and item.price is None:
        notes.append(
            f"Nightly lodging cost is unknown, not yet compared with the ${profile.budget_per_night:,.0f} per room budget."
        )
    elif destination.verified_nightly_usd is None:
        notes.append(
            f"Budget fit ({item.budget_fit}) uses a modelled estimate (typical ${item.price.typical:,.0f} per room, "
            f"per night, two adults), not a live rate."
        )
        if profile.adults + profile.children > 2 * profile.rooms:
            notes.append("More than two guests per room: the two-adult estimate may understate the cost.")
    if not destination.catalog_paces:
        notes.append("Catalog pace depends on the plan.")
    if destination.is_location_enriched:
        notes.append("Location-enriched record: travel tags are heuristic and not destination-specific research.")
    if not profile.departure_known:
        notes.append("No departure location yet; route, mode and travel time have not been checked.")
    else:
        departure = profile.departure_location if origin is None or origin.status != "UNVERIFIED" else "the departure point"
        notes.append(f"Route from {departure} (mode, transfers and travel time) has not been checked.")
    return notes


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
        # Guest display: "Designed to help you feel: **{primary_feeling}**" -- every
        # selected goal, main goal first ("Restored and reflective").
        "primary_feeling": profile.feeling_label,
        "feelings": profile.feelings,
        # What the place is; "match_reasons" says why it fits. No sentence is in both.
        "description": _introduction(destination),
        "latitude": destination.latitude,
        "longitude": destination.longitude,
        "match_score": round(item.total_score),
        # Show this to guests instead of the number; the score is for staff.
        "match_label": match_label(item.total_score),
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
            # "editorial": one of the 72 edited candidates; "location_enriched":
            # one of the 928 records with heuristic travel tags.
            "evidence_tier": "location_enriched" if destination.is_location_enriched else "editorial",
            "source_review_status": destination.source_review_status,
            "live_lodging_status": destination.live_lodging_status,
            "verified_nightly_usd": destination.verified_nightly_usd,
            "budget_status": item.budget_fit if destination.verified_nightly_usd is None else "WITHIN_BUDGET",
            # Modelled estimate (USD per room, per night, two adults): never a rate.
            "price_estimate": (
                {
                    "low_usd": item.price.low,
                    "typical_usd": item.price.typical,
                    "high_usd": item.price.high,
                    "basis": "per room, per night, two adults",
                    "estimated_stay_total_usd": round(item.price.typical * profile.rooms * profile.nights),
                    "status": "ESTIMATE",
                }
                if item.price is not None else None
            ),
            "price_claim": "ESTIMATE" if item.price is not None else "NONE",
            "availability_claim": "NONE",
            "guest_display_gate": destination.guest_display_gate,
            # Internal review notes: for the team, never for guest display.
            "internal_notes": _internal_notes(item, profile, origin),
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
    if reason.startswith("must_avoid:"):
        restriction = reason.split(":", 1)[1]
        label = RESTRICTION_LABELS[restriction]
        explanation = f"'{label}' is marked as must avoid, and it is a known concern at {count} destinations."
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
    high_impact = [
        item for item in blocking
        if item["constraint"].split(":", 1)[-1] in HIGH_IMPACT_DESTINATION_RESTRICTIONS
    ]
    if high_impact:
        # A mandatory need is never something we ask the guest to downgrade.
        question = (
            "We couldn't find a destination that fits your other answers and is free of known concerns for your "
            "mobility or accessibility needs. Your requirement stays as it is: could another answer, such as the "
            "setting or travel distance, change?"
        )
    else:
        question = "No destination fits every answer. Which of these could flex?"
    return {
        "blocking_constraints": blocking,
        "question": question,
        "relaxed_automatically": False,
    }
