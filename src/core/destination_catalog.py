"""
Load the destination catalog described by data/3. IMPORT_RULES.csv.

- AI_IMPORT_DESTINATIONS holds all 1,000 destinations ("Primary import"):
  the 72 original editorial candidates (CANDIDATE_ONLY) and 928
  location-enriched records (LOCATION_ENRICHED). Both may be ranked.
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

# Catalogue release v4 (Velari_Conservative_Catalogue_Release_Candidate_1000.xlsx);
# IMPORT_RULES.csv "Import verification": 1,000 rows, 1,000 unique IDs.
CATALOG_RELEASE = "v4"
CATALOG_SNAPSHOT_DATE = "2026-10-30"
EXPECTED_CANDIDATE_COUNT = 1000
EXPECTED_EDITORIAL_COUNT = 72
EXPECTED_BACKLOG_COUNT = 928

EDITORIAL_CANDIDATE = "CANDIDATE_ONLY"
LOCATION_ENRICHED = "LOCATION_ENRICHED"

# A clause repeated word for word on at least this many rows (10% of the
# catalog) is boilerplate. Shorter templates such as "Plan around weather
# changes and driving times" (26 rows) still describe the place and are kept.
BOILERPLATE_MIN_ROWS = 100
_BOILERPLATE_FIELDS = ("tradeoffs_to_check", "editorial_rationale")
# Staff notes on the 44 tourism-listing rows, never destination facts.
_STAFF_CLAUSES = (
    "verify dates, occupancy, price and access",
    "This is an editorial interpretation, not a guest-specific conclusion.",
)
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
)

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
    kept = " ".join(clause.strip() for clause in _clauses(text) if _clause_key(clause) not in boilerplate)
    return kept.strip().rstrip(";,: ").strip()


def _guest_rationale(text: str) -> str:
    return _STAFF_PREFIX.sub("", text).strip()


def _guest_wording(text: str) -> str:
    for pattern, replacement in _STAFF_PHRASES:
        text = pattern.sub(replacement, text)
    return text


def _to_destination(
    row: Dict[str, str],
    boilerplate: FrozenSet[str] = frozenset(),
    spellings: Optional[Dict[str, str]] = None,
) -> Destination:
    """`spellings`: {country_key: name} so one country is written one way (see country_spellings)."""
    themes = row.get("editorial_themes", "")
    paces = parse_paces(row.get("pace_context", ""))
    specific_tradeoffs = _guest_wording(_without_boilerplate(row.get("tradeoffs_to_check", ""), boilerplate))
    guest_rationale = _guest_rationale(_without_boilerplate(row.get("editorial_rationale", ""), boilerplate))
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
    seen, destinations = set(), []
    for row in rows:
        destination_id = row.get("destination_id", "")
        if not destination_id or destination_id in seen:
            logger.warning("Catalog row skipped: %s destination_id %r.", "missing" if not destination_id else "duplicate", destination_id)
            continue
        seen.add(destination_id)
        destinations.append(_to_destination(row, boilerplate, spellings))
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
