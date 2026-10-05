"""
Recommend specific check-in / check-out dates for flexible or month/season
timing (client brief: "Flexible dates").

Dates are never random. Every possible check-in in the guest's window is
scored on four explicit factors, each with a guest-facing reason:

1. Weather fit -- recorded monthly climate (average highs and rainy days) for
   the chosen destination, or the departure area on a nearby trip, compared
   with the guest's settings, moments and heat/cold restrictions. Without a
   known place it falls back to general seasonality for the hemisphere, and
   says so. Tropical latitudes are left neutral until a destination is known.
2. Crowds and peak periods -- public holidays in the departure country,
   Christmas-New Year, Easter and the summer school break. Guests seeking
   rest or quiet avoid them; families with children may prefer school breaks.
3. Day-of-week shape -- weekend trips for short stays, midweek for restorative
   stays, Saturday-to-Saturday for full weeks, weekend nights for nightlife or
   time with others.
4. Timing -- sooner is better when the guest needs a break; enough lead time
   to arrange travel always.

The result is a suggestion, not a quote: availability and prices for the
dates are never checked here, and the response says so.
"""

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable, Dict, List, Optional, Tuple

from dateutil.easter import easter

from src.core.intake_mappings import ENVIRONMENT_LABELS, TRAVEL_PERIOD_TO_MONTHS, SEASON_PERIODS
from src.core.trip_profile import TripProfile

# Time to arrange travel before check-in, by how far the guest will travel.
LEAD_DAYS = {"nearby": 7, "manageable_flight": 21, "anywhere": 30}
FLEXIBLE_HORIZON_DAYS = 180
ALTERNATIVE_MIN_GAP_DAYS = 7
MAX_ALTERNATIVES = 2
TROPICS_LATITUDE = 23.5

AVAILABILITY_NOTE = (
    "These dates are a suggestion based on your answers. We haven't checked live availability or prices for them, "
    "so we can't say they're available or the lowest-priced option."
)

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September",
          "October", "November", "December")

# Comfortable daytime highs (°C) for each setting.
IDEAL_HIGHS = {
    "coast": (24, 31), "mountains": (15, 25), "forest_jungle": (22, 31), "desert": (20, 32),
    "countryside": (17, 26), "small_town": (17, 27), "vibrant_city": (16, 26), "surprise_me": (18, 28),
}
SETTING_PHRASES = {
    "coast": "time by the coast", "mountains": "mountain days", "forest_jungle": "time in the forest",
    "desert": "desert landscapes", "countryside": "the countryside", "small_town": "small-town wandering",
    "vibrant_city": "exploring a city", "surprise_me": "time outdoors",
}
# General seasonality (Northern-Hemisphere month -> 0..1) when no climate record is available.
SEASONAL_FIT = {
    "coast":         [.2, .2, .3, .5, .7, .9, 1, 1, .85, .6, .3, .2],
    "mountains":     [.5, .5, .4, .5, .7, .9, 1, 1, .9, .7, .4, .5],
    "forest_jungle": [.7] * 12,
    "desert":        [.8, .9, 1, .9, .6, .2, .1, .1, .5, .9, 1, .8],
    "countryside":   [.4, .4, .6, .9, 1, .9, .8, .8, 1, .9, .5, .4],
    "small_town":    [.5, .5, .6, .9, 1, .9, .8, .8, 1, .9, .6, .5],
    "vibrant_city":  [.6, .6, .7, .9, 1, .8, .6, .6, 1, .9, .7, .6],
    "surprise_me":   [.6, .6, .7, .8, .9, .9, .9, .9, .9, .8, .7, .6],
}
SOUTHERN_SEASON_SHIFT = 6

QUIET_GOALS = {"restoration", "reflection"}
QUIET_FEELINGS = {"stretched_thin", "disconnected"}
TOGETHER_PROMPTS = {"time_with_someone", "celebrating"}
URGENT_PROMPTS = {"need_a_break"}


# ==================== WINDOW ====================

@dataclass
class DateWindow:
    earliest_check_in: date
    latest_check_out: date
    source: str  # "guest_range" | "month_season" | "flexible_default"
    note: str = ""


def _season_months(period: str, southern: bool) -> List[int]:
    months = list(TRAVEL_PERIOD_TO_MONTHS[period])
    if southern and period in SEASON_PERIODS:
        months = [((month - 1 + SOUTHERN_SEASON_SHIFT) % 12) + 1 for month in months]
    return months


def _month_end(day: date) -> date:
    next_month = date(day.year + (day.month == 12), day.month % 12 + 1, 1)
    return next_month - timedelta(days=1)


def _next_month_block(months: List[int], start: date) -> Tuple[date, date]:
    """First run of consecutive selected months starting on or after `start` (handles Dec -> Jan)."""
    day = start
    for _ in range(400):
        if day.month in months:
            break
        day = _month_end(day) + timedelta(days=1)
    block_start, block_end = day, _month_end(day)
    while (block_end + timedelta(days=1)).month in months and block_end < day + timedelta(days=100):
        block_end = _month_end(block_end + timedelta(days=1))
    return block_start, block_end


def resolve_window(
    profile: TripProfile,
    today: date,
    earliest: Optional[date] = None,
    latest: Optional[date] = None,
    southern: bool = False,
) -> DateWindow:
    """The range the dates must sit in: the guest's own range, their month/season, or the next six months."""
    lead = LEAD_DAYS.get(profile.travel_distance, 21)
    soonest = today + timedelta(days=lead)
    nights = profile.nights
    if profile.travel_period:
        block_start, block_end = _next_month_block(_season_months(profile.travel_period, southern), soonest)
        window_start = max(block_start, earliest or block_start, soonest)
        window_end = min(block_end + timedelta(days=1), latest or (block_end + timedelta(days=1)))
        note = ""
        if (window_end - window_start).days < nights:
            window_end = window_start + timedelta(days=nights)
            note = "Your trip is longer than the selected period, so the stay runs past its end."
        return DateWindow(window_start, window_end, "month_season", note)
    if earliest or latest:
        window_start = max(earliest or soonest, today + timedelta(days=1))
        window_end = latest or (window_start + timedelta(days=FLEXIBLE_HORIZON_DAYS))
        note = ""
        if window_start < soonest:
            note = "Your range starts soon, so dates that leave time to arrange travel are preferred."
        return DateWindow(window_start, window_end, "guest_range", note)
    return DateWindow(soonest, soonest + timedelta(days=FLEXIBLE_HORIZON_DAYS), "flexible_default",
                      "You're flexible, so we looked at the next six months.")


# ==================== FACTORS ====================

@dataclass
class Factor:
    key: str
    weight: float
    score: float
    reason: Optional[str] = None       # shown when the factor supports the dates
    consideration: Optional[str] = None  # shown when it counts against them


@dataclass
class Candidate:
    check_in: date
    check_out: date
    factors: List[Factor] = field(default_factory=list)

    @property
    def score(self) -> float:
        total = sum(factor.weight for factor in self.factors)
        return sum(factor.weight * factor.score for factor in self.factors) / total if total else 0.0


def _nights(check_in: date, nights: int) -> List[date]:
    return [check_in + timedelta(days=offset) for offset in range(nights)]


def _main_month(dates: List[date]) -> int:
    counts: Dict[int, int] = {}
    for day in dates:
        counts[day.month] = counts.get(day.month, 0) + 1
    return max(counts, key=counts.get)


def _ideal_range(profile: TripProfile) -> Tuple[float, float]:
    ranges = [IDEAL_HIGHS[environment] for environment in profile.environments if environment in IDEAL_HIGHS] \
        or [IDEAL_HIGHS["surprise_me"]]
    low = sum(item[0] for item in ranges) / len(ranges)
    high = sum(item[1] for item in ranges) / len(ranges)
    if "beaches_water" in profile.moments:
        low, high = max(low, 24), max(high, 30)
    if "movement_adventure" in profile.moments:
        high = min(high, 28)
    if "avoid_extreme_heat" in profile.restrictions:
        high = min(high, 28 if profile.restrictions["avoid_extreme_heat"] == "must_avoid" else 30)
    if "avoid_cold_weather" in profile.restrictions:
        low = max(low, 18 if profile.restrictions["avoid_cold_weather"] == "must_avoid" else 15)
    return low, max(high, low + 2)


def _comfort(normals: dict, profile: TripProfile) -> float:
    low, high = _ideal_range(profile)
    average_high = normals["avg_high_c"]
    distance = max(low - average_high, average_high - high, 0)
    heat_rule = profile.restrictions.get("avoid_extreme_heat")
    cold_rule = profile.restrictions.get("avoid_cold_weather")
    step = 0.12 if (heat_rule == "must_avoid" and average_high > high) or (cold_rule == "must_avoid" and average_high < low) else 0.08
    temperature = max(0.0, 1 - distance * step)
    wet_tolerance = 4 if "forest_jungle" in profile.environments else 0
    rain = min(1.0, max(0.2, 1 - (normals["rainy_days"] - 6 - wet_tolerance) * 0.07))
    return 0.65 * temperature + 0.35 * rain


def _settings_phrase(profile: TripProfile) -> str:
    phrases = [SETTING_PHRASES[environment] for environment in profile.environments if environment in SETTING_PHRASES]
    return " and ".join(phrases[:2]) or "your trip"


def weather_factor(dates: List[date], profile: TripProfile, climate: Optional[dict], place_label: Optional[str],
                   latitude: Optional[float], southern: bool) -> Factor:
    month = _main_month(dates)
    month_name = MONTHS[month - 1]
    if climate and climate.get("months"):
        normals = climate["months"][month]
        score = sum(_comfort(climate["months"][day.month], profile) for day in dates) / len(dates)
        facts = f"{month_name} in {place_label} averages highs around {round(normals['avg_high_c'])}°C " \
                f"with about {round(normals['rainy_days'])} rainy days in the month"
        if score >= 0.7:
            return Factor("weather", 0.40, score, reason=f"{facts}, comfortable weather for {_settings_phrase(profile)}.")
        return Factor("weather", 0.40, score,
                      consideration=f"{facts}, so pack for changeable weather." if normals["rainy_days"] > 12
                      else f"{facts}, outside the most comfortable range for {_settings_phrase(profile)}.")
    if latitude is not None and abs(latitude) < TROPICS_LATITUDE:
        return Factor("weather", 0.0, 0.5,
                      consideration="Tropical weather depends on the local wet and dry seasons; we'll check them once you choose a destination.")
    if profile.travel_distance == "anywhere" and place_label is None:
        return Factor("weather", 0.0, 0.5,
                      consideration="Weather depends on where you go; we'll fine-tune it once you choose a destination.")
    hemisphere = "Southern" if southern else "Northern"
    environments = [environment for environment in profile.environments if environment in SEASONAL_FIT] or ["surprise_me"]

    def seasonal(day: date) -> float:
        index = ((day.month - 1 + (SOUTHERN_SEASON_SHIFT if southern else 0)) % 12)
        value = sum(SEASONAL_FIT[environment][index] for environment in environments) / len(environments)
        if "avoid_extreme_heat" in profile.restrictions and index in (5, 6, 7):
            value *= 0.4
        if "avoid_cold_weather" in profile.restrictions and index in (11, 0, 1):
            value *= 0.4
        return value

    score = sum(seasonal(day) for day in dates) / len(dates)
    if score >= 0.7:
        return Factor("weather", 0.20, score,
                      reason=f"{month_name} is usually a good season in the {hemisphere} Hemisphere for {_settings_phrase(profile)}; "
                             "we'll confirm the weather once you choose a destination.")
    return Factor("weather", 0.20, score,
                  consideration=f"{month_name} is usually not the best season for {_settings_phrase(profile)} in the "
                                f"{hemisphere} Hemisphere.")


def peak_days(years: List[int], country_code: Optional[str], southern: bool,
              holiday_lookup: Optional[Callable[[str, List[int]], Dict[date, str]]] = None) -> Dict[date, str]:
    """Busy travel days: national holidays (+/- 1 day), Christmas-New Year, Easter and the summer school break."""
    peaks: Dict[date, str] = {}
    for year in years:
        day = date(year, 12, 20)
        while day <= date(year + 1, 1, 2):
            peaks[day] = "the Christmas and New Year holidays"
            day += timedelta(days=1)
        easter_day = easter(year)
        for offset in range(-3, 2):  # Thursday - Monday
            peaks[easter_day + timedelta(days=offset)] = "the Easter weekend"
        school = (date(year, 12, 15), date(year + 1, 1, 31)) if southern else (date(year, 7, 1), date(year, 8, 31))
        day = school[0]
        while day <= school[1]:
            peaks.setdefault(day, "the summer school holidays")
            day += timedelta(days=1)
    if country_code and holiday_lookup:
        try:
            national = holiday_lookup(country_code, years)
        except Exception:
            national = {}
        for day, name in national.items():
            for offset in (-1, 0, 1):
                peaks.setdefault(day + timedelta(days=offset), f"a public holiday ({name})")
    return peaks


def _quiet_seeker(profile: TripProfile) -> bool:
    return bool(QUIET_GOALS & set(profile.goals) or "quiet_privacy" in profile.moments
                or QUIET_FEELINGS & set(profile.recent_feelings))


def crowd_factor(dates: List[date], profile: TripProfile, peaks: Dict[date, str]) -> Factor:
    hits = [peaks[day] for day in dates if day in peaks]
    overlap = len(hits) / len(dates)
    main = max(set(hits), key=hits.count) if hits else None
    if profile.children:
        in_school_break = main == "the summer school holidays"
        return Factor("crowds", 0.15, 0.6 + 0.4 * in_school_break,
                      reason="It falls in the summer school holidays, so the whole family can travel." if in_school_break else None,
                      consideration=None if in_school_break else "Check that these dates suit the children's school calendar.")
    if _quiet_seeker(profile):
        if not hits:
            return Factor("crowds", 0.25, 1.0, reason="It stays clear of public holidays and school breaks, when popular places are at their busiest.")
        return Factor("crowds", 0.25, 1 - overlap, consideration=f"It overlaps {main}, so expect busier places.")
    if not hits:
        return Factor("crowds", 0.15, 1.0, reason="It avoids the busiest holiday periods.")
    return Factor("crowds", 0.15, 1 - 0.5 * overlap, consideration=f"It overlaps {main}, so expect busier places.")


def weekday_factor(check_in: date, nights: int, profile: TripProfile) -> Factor:
    stay = _nights(check_in, nights)
    weekdays = {day.weekday() for day in stay}
    start = WEEKDAYS[check_in.weekday()]
    end = WEEKDAYS[(check_in + timedelta(days=nights)).weekday()]
    together = bool(TOGETHER_PROMPTS & {profile.trip_prompt}) or "connection" in profile.goals or "celebration" in profile.goals
    if "music_nightlife" in profile.moments or together:
        weekend_nights = {4, 5} <= weekdays
        why = ("so your evenings out fall on a Friday and Saturday" if "music_nightlife" in profile.moments
               else "so the people you're travelling with can join without taking much time off")
        return Factor("weekday", 0.15, 1.0 if weekend_nights else 0.4,
                      reason=f"A {start} check-in includes a full weekend, {why}." if weekend_nights else None)
    if nights <= 3:
        preferred = {4: 1.0, 3: 0.9, 5: 0.7}
        reason = f"A {start}-to-{end} stay uses the weekend, so you need fewer days off."
    elif nights >= 7:
        preferred = {5: 1.0, 6: 0.9, 4: 0.7}
        reason = f"A {start}-to-{end} stay is the simplest way to plan a full week away."
    elif _quiet_seeker(profile):
        preferred = {6: 1.0, 0: 1.0, 1: 0.8}
        reason = f"A {start}-to-{end} stay sits mostly midweek, when popular spots are usually calmer."
    else:
        preferred = {3: 0.9, 4: 0.9, 6: 0.8}
        reason = f"A {start}-to-{end} stay balances a weekend with quieter weekdays."
    score = preferred.get(check_in.weekday(), 0.3)
    return Factor("weekday", 0.15, score, reason=reason if score >= 0.8 else None)


def timing_factor(check_in: date, window: DateWindow, profile: TripProfile, today: date) -> Factor:
    span = max((window.latest_check_out - window.earliest_check_in).days, 1)
    position = (check_in - window.earliest_check_in).days / span
    lead = LEAD_DAYS.get(profile.travel_distance, 21)
    if (check_in - today).days < lead:
        return Factor("timing", 0.10, 0.3, consideration="It leaves little time to arrange travel.")
    if URGENT_PROMPTS & {profile.trip_prompt} or "stretched_thin" in profile.recent_feelings:
        return Factor("timing", 0.10, 1 - position,
                      reason="It's one of the sooner good options, since you told us you need a break." if position < 0.35 else None)
    return Factor("timing", 0.05, 0.6)


# ==================== RECOMMENDATION ====================

@dataclass
class DateContext:
    """What the scoring knows beyond the guest's answers."""
    today: date
    southern: bool = False
    latitude: Optional[float] = None
    climate: Optional[dict] = None
    place_label: Optional[str] = None
    country_code: Optional[str] = None
    holiday_lookup: Optional[Callable[[str, List[int]], Dict[date, str]]] = None


def score_candidates(profile: TripProfile, window: DateWindow, context: DateContext) -> List[Candidate]:
    nights = profile.nights
    last_check_in = window.latest_check_out - timedelta(days=nights)
    years = sorted({window.earliest_check_in.year - 1, window.earliest_check_in.year, window.latest_check_out.year})
    peaks = peak_days(years, context.country_code, context.southern, context.holiday_lookup)
    candidates = []
    day = window.earliest_check_in
    while day <= last_check_in:
        stay = _nights(day, nights)
        candidates.append(Candidate(day, day + timedelta(days=nights), [
            weather_factor(stay, profile, context.climate, context.place_label, context.latitude, context.southern),
            crowd_factor(stay, profile, peaks),
            weekday_factor(day, nights, profile),
            timing_factor(day, window, profile, context.today),
        ]))
        day += timedelta(days=1)
    return candidates


# A clearly better-weather option is offered when the best overall dates trade weather for quiet.
WEATHER_OPTION_MARGIN = 0.15


def _factor_score(candidate: Candidate, key: str) -> float:
    return next((factor.score for factor in candidate.factors if factor.key == key and factor.weight), 0.0)


def pick(candidates: List[Candidate]) -> List[Tuple[str, Candidate]]:
    """
    (label, dates): the best overall, the best-weather dates when they are
    clearly better on weather, then the next best -- all starting at least a
    week apart.
    """
    ranked = sorted(candidates, key=lambda candidate: (-round(candidate.score, 4), candidate.check_in))
    chosen: List[Tuple[str, Candidate]] = [("Best overall", ranked[0])]

    def apart(candidate: Candidate) -> bool:
        return all(abs((candidate.check_in - other.check_in).days) >= ALTERNATIVE_MIN_GAP_DAYS for _, other in chosen)

    best_weather = _factor_score(ranked[0], "weather")
    by_weather = sorted(ranked, key=lambda candidate: (-round(_factor_score(candidate, "weather"), 4), -candidate.score))
    for candidate in by_weather:
        if _factor_score(candidate, "weather") < best_weather + WEATHER_OPTION_MARGIN:
            break
        if apart(candidate):
            chosen.append(("Best weather", candidate))
            break
    for candidate in ranked:
        if len(chosen) > MAX_ALTERNATIVES:
            break
        if apart(candidate):
            chosen.append(("Another good option", candidate))
    return chosen


def describe(candidate: Candidate, label: str) -> dict:
    ordered = sorted(candidate.factors, key=lambda factor: -factor.weight * factor.score)
    return {
        "label": label,
        "check_in": candidate.check_in.isoformat(),
        "check_out": candidate.check_out.isoformat(),
        "nights": (candidate.check_out - candidate.check_in).days,
        "check_in_weekday": WEEKDAYS[candidate.check_in.weekday()],
        "check_out_weekday": WEEKDAYS[candidate.check_out.weekday()],
        "match_score": round(candidate.score * 100),
        "reasons": [factor.reason for factor in ordered if factor.reason],
        "considerations": [factor.consideration for factor in candidate.factors if factor.consideration],
        "factor_scores": {factor.key: round(factor.score, 2) for factor in candidate.factors},
    }


def recommend_dates(profile: TripProfile, window: DateWindow, context: DateContext) -> dict:
    candidates = score_candidates(profile, window, context)
    if not candidates:
        raise ValueError(
            f"The selected range ({window.earliest_check_in.isoformat()} to {window.latest_check_out.isoformat()}) "
            f"is shorter than {profile.nights} nights."
        )
    (best_label, best), *alternatives = pick(candidates)
    return {
        "recommended": describe(best, best_label),
        "alternatives": [describe(candidate, label) for label, candidate in alternatives],
        "window": {
            "earliest_check_in": window.earliest_check_in.isoformat(),
            "latest_check_out": window.latest_check_out.isoformat(),
            "source": window.source,
            "note": window.note,
        },
        "nights": profile.nights,
        "availability_note": AVAILABILITY_NOTE,
        "status": "SUGGESTED_NOT_CHECKED",
        "basis": {
            "climate": (
                {"place": context.place_label, "source": context.climate.get("source"), "period": context.climate.get("period")}
                if context.climate and context.climate.get("months") else None
            ),
            "hemisphere": "southern" if context.southern else "northern",
            "public_holidays_country": context.country_code,
            "candidates_scored": len(candidates),
            "settings": [ENVIRONMENT_LABELS[environment] for environment in profile.environments],
        },
    }


# ==================== GUEST SUMMARY ====================

# Claims the suggestion cannot make before live availability and prices are checked.
_FORBIDDEN_CLAIMS = re.compile(
    r"\bavailab\w*|\bcheap\w*|\blowest\b|\bbest (?:price|rate|deal)s?\b|\bdeals?\b|\bdiscount\w*"
    r"|\bguarantee\w*|\bsave money\b|\bbargain\w*|\bsold out\b",
    re.IGNORECASE,
)
SUMMARY_MAX_WORDS = 60


def date_phrase(iso_day: str) -> str:
    day = date.fromisoformat(iso_day)
    return f"{WEEKDAYS[day.weekday()]} {day.day} {MONTHS[day.month - 1]}"


def fallback_summary(result: dict) -> str:
    best = result["recommended"]
    lead = f"We suggest {date_phrase(best['check_in'])} to {date_phrase(best['check_out'])}."
    return " ".join([lead, *best["reasons"][:2]])


def _summary_prompt(result: dict, profile: TripProfile, rejection: Optional[str]) -> str:
    best = result["recommended"]
    facts = "\n".join(f"- {reason}" for reason in best["reasons"]) or "- (no specific facts)"
    retry = f"\nYOUR PREVIOUS ANSWER WAS REJECTED: {rejection}. Fix that.\n" if rejection else ""
    return f"""
Write a short, warm note for a traveler explaining why these travel dates suit their trip.

DATES: {date_phrase(best['check_in'])} to {date_phrase(best['check_out'])} ({best['nights']} nights)
WHAT THE TRAVELER HOPES FOR: {', '.join(profile.goal_labels)}; enjoys {', '.join(profile.moment_labels)}.
FACTS YOU MAY USE (use only these; do not invent weather, events or prices):
{facts}
{retry}
RULES:
- One or two sentences, at most {SUMMARY_MAX_WORDS} words, starting with the dates.
- Never say or imply the dates are available, booked, cheap, the cheapest, a deal or the best price.
- No scores, no promises about how the traveler will feel.

RESPONSE FORMAT (JSON ONLY): {{"summary": "..."}}
"""


def write_summary(result: dict, profile: TripProfile, ai_response_fn: Callable[[str], str], attempts: int = 2) -> Tuple[str, str]:
    """(summary, source): an AI-written note checked against the rules, or the factual fallback."""
    from src.core.data_processor import ProcessData

    rejection: Optional[str] = None
    for _ in range(attempts):
        try:
            payload = ProcessData.EnsureDict(ai_response_fn(_summary_prompt(result, profile, rejection)))
        except Exception as error:  # Model or parsing failure -- retry, then fall back.
            rejection = f"the answer could not be read as JSON ({type(error).__name__})"
            continue
        summary = str(payload.get("summary") or "").strip() if isinstance(payload, dict) else ""
        sentences = [part for part in re.split(r"(?<=[.!?])\s+", summary) if part]
        if not summary:
            rejection = "the summary was empty"
        elif not 1 <= len(sentences) <= 2:
            rejection = "use one or two sentences"
        elif len(summary.split()) > SUMMARY_MAX_WORDS:
            rejection = f"use at most {SUMMARY_MAX_WORDS} words"
        elif _FORBIDDEN_CLAIMS.search(summary):
            rejection = "it implied availability or price; describe why the dates suit the trip instead"
        else:
            return summary, "ai"
    return fallback_summary(result), "fallback"
