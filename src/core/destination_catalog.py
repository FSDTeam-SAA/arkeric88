"""
Load the destination catalog described by data/3. IMPORT_RULES.csv.

- AI_IMPORT_DESTINATIONS holds all 1,500 destinations ("Primary import"):
  572 source-linked candidates (CANDIDATE_ONLY: the 72 original and 500 added)
  and 928 location-enriched records (LOCATION_ENRICHED). Both may be ranked.
- DISCOVERY_BACKLOG is loaded only for integrity checks: every backlog row is
  now imported into AI_IMPORT_DESTINATIONS (import_status ENRICHED_IN_IMPORT).
- No historical property or archetype sheet is part of this import.

Every catalog claim stays an editorial hypothesis pending source review; this
module only parses fields, it never upgrades a row's verification status.
The CSVs are located by suffix so the numeric filename prefixes
("1. ", "2. ", ...) can change without a code change.

The 928 enriched rows share template sentences ("Confirm on-the-ground
experiences, guest accessibility, suitable dates and any required
transfers.") that say nothing about the place. Clauses repeated word for word
across many rows are treated as boilerplate: they are kept in the raw record
but left out of matching and guest text, so a generic reminder about
"transfers" or "accessibility" never reads as a destination-specific concern.
"""

import csv
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Dict, FrozenSet, List, Optional

from src.core.geography import country_key, country_spellings
from src.core.intake_mappings import (
    CATALOG_THEME_SECONDARY_GOALS,
    CATALOG_THEME_TO_GOAL,
    SECONDARY_GOAL_STRENGTH,
)

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
CANDIDATES_FILE_SUFFIX = "AI_IMPORT_DESTINATIONS.csv"
BACKLOG_FILE_SUFFIX = "DISCOVERY_BACKLOG.csv"

# Catalogue release v6 (Velari_AI_IMPORT_DESTINATIONS_1500_UPDATED.xlsx);
# IMPORT_RULES.csv "Import verification": 1,500 rows, 1,500 unique IDs.
CATALOG_RELEASE = "v6"
CATALOG_SNAPSHOT_DATE = "2026-11-12"
EXPECTED_CANDIDATE_COUNT = 1500
EXPECTED_EDITORIAL_COUNT = 572
EXPECTED_BACKLOG_COUNT = 928

EDITORIAL_CANDIDATE = "CANDIDATE_ONLY"
LOCATION_ENRICHED = "LOCATION_ENRICHED"

# A clause repeated word for word on at least this many rows (about 7% of the
# catalog) is boilerplate. Shorter templates such as "Plan around weather
# changes and driving times" (26 rows) still describe the place and are kept.
BOILERPLATE_MIN_ROWS = 100
_BOILERPLATE_FIELDS = ("tradeoffs_to_check", "editorial_rationale")
# Staff notes on the 44 tourism-listing rows, never destination facts.
_STAFF_CLAUSES = (
    "verify dates, occupancy, price and access",
    "This is an editorial interpretation, not a guest-specific conclusion.",
)
# The 49 protected-area rows end with a note on how the tag was chosen; the
# theme word changes, so it never repeats often enough to count as boilerplate.
_STAFF_CLAUSE = re.compile(r"^this setting may suit travell?ers seeking|^emotional fit is a hypothesis", re.IGNORECASE)
_CLAUSE_BREAK = re.compile(r"(?<=[.;])\s+")
# Staff wording on the 44 tourism-listing rows: "Consider when guest seeks
# inspiration and discovery: architecture, culture, and coast."
_STAFF_PREFIX = re.compile(r"^\s*consider when (?:the )?guest seeks [^:]*:\s*", re.IGNORECASE)
# "an immersion-led city break rather than a quiet retreat" describes what the
# place is NOT; keyword matching must not read "quiet" as a feature.
_CONTRAST_CLAUSE = re.compile(r"\b(?:rather than|instead of|not suited to)\b[^.;]*", re.IGNORECASE)
# Editor shorthand in two tradeoffs ("Driving and wine focus may not suit
# guest") reads as a judgment about the person; guests see neutral wording.
_STAFF_PHRASES = (
    (re.compile(r"\bmay not suit (?:the |every )?guests?\b", re.IGNORECASE), "may not suit everyone"),
    (re.compile(r"\bguests seeking\b", re.IGNORECASE), "travelers seeking"),
    # The 44 tourism-listing rows write these in lower case.
    *((re.compile(rf"\b{word.lower()}\b"), word) for word in (
        "Japanese", "Swiss", "Tasmanian", "Mediterranean", "Andean", "Pacific", "Atlantic", "Guanacaste", "South Island",
    )),
)
# experience_context words that describe a booking step, not the place. Read as
# a claim ("Booked gallery tour", "Date-confirmed hike") they would state
# something nobody has checked, so guests see the experience without them.
_OPERATIONAL_PREFIX = re.compile(
    r"^(?:(?:(?:date|season|operator|route|language|condition|ability|host|companion|age|advance|community)"
    r"-(?:confirmed|approved|matched|cleared|specific|eligible|arranged|planned|paced|focused)"
    r"|booked|prebooked|reserved|prearranged|confirmed|eligible|requested|approved|verified"
    r"|selected|planned|arranged|paced|matched|dated)\s+)+",
    re.IGNORECASE,
)
_BOOKING_CLAIM = re.compile(r"\b(?:confirmed|booked|prebooked|reserved|prearranged|approved|verified)\b", re.IGNORECASE)
_WORD = re.compile(r"[^\W\d_]+")
# Real compounds start like this ("intercultural", "transatlantic").
_COMPOUND_PREFIX = re.compile(r"^(?:inter|intra|trans|palaeo|paleo|multi|counter)", re.IGNORECASE)

_CATALOG_PACES = {"gentle": "Gentle", "balanced": "Balanced", "active": "Active"}


@dataclass(frozen=True)
class Destination:
    destination_id: str
    destination: str
    country: str
    world_region: str
    latitude: Optional[float]
    longitude: Optional[float]
    # Goals the themes support fully, in catalog order.
    goals: tuple
    raw_themes: str
    experience_context: str
    setting_context: str
    pace_context: str
    # The single catalog pace, or None when the row gives several or none.
    catalog_pace: Optional[str]
    editorial_rationale: str
    tradeoffs_to_check: str
    destination_source_url: str
    source_scope: str
    source_last_checked_utc: str
    editorial_evidence_limit: str
    candidate_status: str
    source_review_status: str
    live_lodging_status: str
    verified_nightly_usd: Optional[float]
    guest_display_gate: str
    # Goals supported only partially (Explorer -> adventure).
    secondary_goals: tuple = ()
    # Every pace the row allows ("Gentle|Balanced" -> both).
    catalog_paces: tuple = ()
    # Tradeoffs and rationale without boilerplate or staff wording.
    specific_tradeoffs: str = ""
    guest_rationale: str = ""
    # Each experience as (catalog wording for matching, wording guests may see).
    experience_items: tuple = ()
    record: Dict[str, str] = field(default_factory=dict, compare=False, repr=False)
    match_fields: Dict[str, str] = field(default_factory=dict, compare=False, repr=False)

    def field_text(self, field_name: str) -> str:
        """Text used for keyword matching: boilerplate and contrast clauses removed."""
        return self.match_fields.get(field_name, self.record.get(field_name, ""))

    def goal_strength(self, goal: str) -> float:
        if goal in self.goals:
            return 1.0
        if goal in self.secondary_goals:
            return SECONDARY_GOAL_STRENGTH
        return 0.0

    @property
    def is_location_enriched(self) -> bool:
        return self.candidate_status == LOCATION_ENRICHED


def _find_data_file(suffix: str) -> Path:
    matches = sorted(DATA_DIR.glob(f"*{suffix}"))
    if not matches:
        raise FileNotFoundError(f"No file ending in '{suffix}' found in {DATA_DIR}.")
    if len(matches) > 1:
        raise RuntimeError(f"Multiple files ending in '{suffix}' found in {DATA_DIR}: {matches}")
    return matches[0]


def _read_rows(suffix: str) -> List[Dict[str, str]]:
    with _find_data_file(suffix).open(encoding="utf-8-sig", newline="") as handle:
        return [
            {key.strip(): (value or "").strip() for key, value in row.items() if key}
            for row in csv.DictReader(handle)
            if any((value or "").strip() for value in row.values())
        ]


def _optional_float(raw: str) -> Optional[float]:
    """Blank means unknown -- never zero (IMPORT_RULES.csv "Budget")."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return float(raw.replace(",", "").replace("$", ""))
    except ValueError:
        return None


def _theme_tokens(raw_themes: str) -> List[str]:
    return [token.strip().lower() for token in re.split(r"[|;,]", raw_themes or "") if token.strip()]


def parse_goals(raw_themes: str) -> tuple:
    """Normalize both catalog theme vocabularies into trip_goal codes, in catalog order."""
    goals: List[str] = []
    for token in _theme_tokens(raw_themes):
        goal = CATALOG_THEME_TO_GOAL.get(token)
        if goal and goal not in goals:
            goals.append(goal)
    return tuple(goals)


def parse_secondary_goals(raw_themes: str) -> tuple:
    """Goals a theme supports only partially, minus any the row supports fully."""
    full = set(parse_goals(raw_themes))
    secondary: List[str] = []
    for token in _theme_tokens(raw_themes):
        for goal in CATALOG_THEME_SECONDARY_GOALS.get(token, ()):
            if goal not in full and goal not in secondary:
                secondary.append(goal)
    return tuple(secondary)


def parse_paces(raw: str) -> tuple:
    """"Gentle|Balanced" -> ("Gentle", "Balanced"); "Guest-dependent; confirm desired pace" -> ()."""
    paces: List[str] = []
    for token in re.split(r"[|/;,]", raw or ""):
        pace = _CATALOG_PACES.get(token.strip().lower())
        if pace and pace not in paces:
            paces.append(pace)
    return tuple(paces)


def _clause_key(clause: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", clause.lower()).strip()


def _clauses(text: str) -> List[str]:
    return [clause for clause in _CLAUSE_BREAK.split(text or "") if clause.strip()]


def boilerplate_clauses(rows: List[Dict[str, str]], min_rows: int = BOILERPLATE_MIN_ROWS) -> FrozenSet[str]:
    """Clause keys that repeat verbatim across at least `min_rows` rows."""
    counts: Counter = Counter()
    for row in rows:
        keys = {_clause_key(clause) for name in _BOILERPLATE_FIELDS for clause in _clauses(row.get(name, ""))}
        counts.update(key for key in keys if key)
    repeated = {key for key, count in counts.items() if count >= min_rows}
    return frozenset(repeated | {_clause_key(clause) for clause in _STAFF_CLAUSES})


def _without_boilerplate(text: str, boilerplate: FrozenSet[str]) -> str:
    kept = " ".join(
        clause.strip() for clause in _clauses(text)
        if _clause_key(clause) not in boilerplate and not _STAFF_CLAUSE.match(clause.strip())
    )
    return kept.strip().rstrip(";,: ").strip()


def _guest_rationale(text: str) -> str:
    return _STAFF_PREFIX.sub("", text).strip()


def _guest_wording(text: str) -> str:
    for pattern, replacement in _STAFF_PHRASES:
        text = pattern.sub(replacement, text)
    return text


def word_counts(rows: List[Dict[str, str]]) -> Counter:
    """How often each word appears across the fields guests can read about a place."""
    counts: Counter = Counter()
    for row in rows:
        for name in ("destination", "experience_context", "editorial_rationale", "tradeoffs_to_check", "setting_context"):
            counts.update(word.lower() for word in _WORD.findall(row.get(name, "")))
    return counts


def is_run_together(word: str, counts: Counter) -> bool:
    """
    True for a word the source lost its spaces in ("TeAwaruaOPorirua",
    "harbourhistory"): mixed case inside the word, or a rare long word made of
    two words the catalog uses on their own. Shorter compounds ("candlelight",
    "birdwatching") are left alone. A name written in mixed case ("VanDusen")
    is also caught; its row then shows no experience text, which is safe.
    """
    if re.search(r"[a-z][A-Z]", word) and not re.match(r"^(?:Mc|Mac)[A-Z]", word):
        return True
    lowered = word.lower()
    if len(lowered) < 13 or counts[lowered] > 2 or _COMPOUND_PREFIX.match(lowered):
        return False
    return len(lowered) >= 17 or any(
        counts[lowered[:cut]] >= 2 and counts[lowered[cut:]] >= 2 for cut in range(4, len(lowered) - 3)
    )


def _experience_items(raw: str, counts: Counter) -> tuple:
    """
    "Booked cooking class|Monte Alban" -> pairs of (catalog wording, guest
    wording). Split on pipes only: a row may hold one full sentence with commas.
    A row with run-together words shows none of them until the source is fixed.
    """
    entries = [entry.strip() for entry in (raw or "").split("|") if entry.strip()]
    if any(is_run_together(word, counts) for entry in entries for word in _WORD.findall(entry)):
        return ()
    items = []
    for entry in entries:
        shown = _OPERATIONAL_PREFIX.sub("", entry).strip().rstrip(".")
        if shown and not _BOOKING_CLAIM.search(shown):
            items.append((entry, shown[0].upper() + shown[1:]))
    return tuple(items)


def _to_destination(
    row: Dict[str, str],
    boilerplate: FrozenSet[str] = frozenset(),
    spellings: Optional[Dict[str, str]] = None,
    counts: Optional[Counter] = None,
) -> Destination:
    """`spellings`: {country_key: name} so one country is written one way (see country_spellings)."""
    themes = row.get("editorial_themes", "")
    paces = parse_paces(row.get("pace_context", ""))
    specific_tradeoffs = _guest_wording(_without_boilerplate(row.get("tradeoffs_to_check", ""), boilerplate))
    guest_rationale = _guest_wording(_guest_rationale(_without_boilerplate(row.get("editorial_rationale", ""), boilerplate)))
    return Destination(
        destination_id=row["destination_id"],
        destination=row["destination"],
        country=(spellings or {}).get(country_key(row["country"]), row["country"]),
        world_region=row["world_region"],
        latitude=_optional_float(row.get("latitude", "")),
        longitude=_optional_float(row.get("longitude", "")),
        goals=parse_goals(themes),
        raw_themes=themes,
        experience_context=row.get("experience_context", ""),
        setting_context=row.get("setting_context", ""),
        pace_context=row.get("pace_context", ""),
        catalog_pace=paces[0] if len(paces) == 1 else None,
        editorial_rationale=row.get("editorial_rationale", ""),
        tradeoffs_to_check=row.get("tradeoffs_to_check", ""),
        destination_source_url=row.get("destination_source_url", ""),
        source_scope=row.get("source_scope", ""),
        source_last_checked_utc=row.get("source_last_checked_utc", ""),
        editorial_evidence_limit=row.get("editorial_evidence_limit", ""),
        candidate_status=row.get("candidate_status", ""),
        source_review_status=row.get("source_review_status", ""),
        live_lodging_status=row.get("live_lodging_status", ""),
        verified_nightly_usd=_optional_float(row.get("verified_nightly_usd", "")),
        guest_display_gate=row.get("guest_display_gate", ""),
        secondary_goals=parse_secondary_goals(themes),
        catalog_paces=paces,
        specific_tradeoffs=specific_tradeoffs,
        guest_rationale=guest_rationale,
        experience_items=_experience_items(row.get("experience_context", ""), counts or Counter()),
        record=dict(row),
        match_fields={
            "tradeoffs_to_check": specific_tradeoffs,
            "editorial_rationale": _CONTRAST_CLAUSE.sub("", guest_rationale),
        },
    )


@lru_cache(maxsize=1)
def load_destination_candidates() -> tuple:
    """
    All AI_IMPORT_DESTINATIONS rows, in file order. Immutable and cached.
    A repeated destination_id keeps its first row: a catalogue update must
    never create a duplicate or silently replace an existing destination.
    """
    rows = _read_rows(CANDIDATES_FILE_SUFFIX)
    boilerplate = boilerplate_clauses(rows)
    spellings = country_spellings(row.get("country", "") for row in rows)
    counts = word_counts(rows)
    seen, destinations = set(), []
    for row in rows:
        destination_id = row.get("destination_id", "")
        if not destination_id or destination_id in seen:
            logger.warning("Catalog row skipped: %s destination_id %r.", "missing" if not destination_id else "duplicate", destination_id)
            continue
        seen.add(destination_id)
        destinations.append(_to_destination(row, boilerplate, spellings, counts))
    return tuple(destinations)


@lru_cache(maxsize=1)
def load_discovery_backlog() -> tuple:
    """Geographic source rows; each one is now imported into the main catalog."""
    return tuple(_read_rows(BACKLOG_FILE_SUFFIX))


@lru_cache(maxsize=1)
def _candidates_by_id() -> Dict[str, Destination]:
    return {destination.destination_id: destination for destination in load_destination_candidates()}


def get_destination(destination_id: str) -> Optional[Destination]:
    """Look up an imported destination by id."""
    return _candidates_by_id().get(destination_id)


@lru_cache(maxsize=1)
def get_catalog_version() -> Dict[str, object]:
    candidates = load_destination_candidates()
    return {
        "release": CATALOG_RELEASE,
        "snapshot_date": CATALOG_SNAPSHOT_DATE,
        "candidate_count": len(candidates),
        "editorial_count": sum(not destination.is_location_enriched for destination in candidates),
        "location_enriched_count": sum(destination.is_location_enriched for destination in candidates),
        "without_coordinates_count": sum(destination.latitude is None or destination.longitude is None for destination in candidates),
        "backlog_count": len(load_discovery_backlog()),
        "candidates_file": _find_data_file(CANDIDATES_FILE_SUFFIX).name,
    }
