"""
Load the destination catalog described by data/3. IMPORT_RULES.csv.

- AI_IMPORT_DESTINATIONS is the candidate seed data ("Primary import").
- DISCOVERY_BACKLOG is loaded only for integrity checks and is never returned
  as a guest recommendation.
- No historical property or archetype sheet is part of this import.

Every catalog claim stays an editorial hypothesis pending source review; this
module only parses fields, it never upgrades a row's verification status.
The CSVs are located by suffix so the numeric filename prefixes
("1. ", "2. ", ...) can change without a code change.
"""

import csv
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional

from src.core.intake_mappings import CATALOG_THEME_TO_GOAL

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
CANDIDATES_FILE_SUFFIX = "AI_IMPORT_DESTINATIONS.csv"
BACKLOG_FILE_SUFFIX = "DISCOVERY_BACKLOG.csv"

# Declared in IMPORT_RULES.csv "Version and scope"; tests assert the files match.
CATALOG_SNAPSHOT_DATE = "2026-09-24"
EXPECTED_CANDIDATE_COUNT = 72
EXPECTED_BACKLOG_COUNT = 928

_CATALOG_PACES = {"gentle": "Gentle", "balanced": "Balanced", "active": "Active"}


@dataclass(frozen=True)
class Destination:
    destination_id: str
    destination: str
    country: str
    world_region: str
    latitude: Optional[float]
    longitude: Optional[float]
    goals: tuple
    raw_themes: str
    experience_context: str
    setting_context: str
    pace_context: str
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
    record: Dict[str, str] = field(default_factory=dict, compare=False, repr=False)

    def field_text(self, field_name: str) -> str:
        return self.record.get(field_name, "")


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


def parse_goals(raw_themes: str) -> tuple:
    """Normalize both catalog theme vocabularies into trip_goal codes, in catalog order."""
    goals: List[str] = []
    for token in re.split(r"[|;,]", raw_themes or ""):
        goal = CATALOG_THEME_TO_GOAL.get(token.strip().lower())
        if goal and goal not in goals:
            goals.append(goal)
    return tuple(goals)


def _catalog_pace(raw: str) -> Optional[str]:
    """Gentle/Balanced/Active, or None for "Guest-dependent; confirm desired pace"."""
    return _CATALOG_PACES.get((raw or "").strip().lower())


def _to_destination(row: Dict[str, str]) -> Destination:
    return Destination(
        destination_id=row["destination_id"],
        destination=row["destination"],
        country=row["country"],
        world_region=row["world_region"],
        latitude=_optional_float(row.get("latitude", "")),
        longitude=_optional_float(row.get("longitude", "")),
        goals=parse_goals(row.get("editorial_themes", "")),
        raw_themes=row.get("editorial_themes", ""),
        experience_context=row.get("experience_context", ""),
        setting_context=row.get("setting_context", ""),
        pace_context=row.get("pace_context", ""),
        catalog_pace=_catalog_pace(row.get("pace_context", "")),
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
        record=dict(row),
    )


@lru_cache(maxsize=1)
def load_destination_candidates() -> tuple:
    """All AI_IMPORT_DESTINATIONS rows, in file order. Immutable and cached."""
    return tuple(_to_destination(row) for row in _read_rows(CANDIDATES_FILE_SUFFIX))


@lru_cache(maxsize=1)
def load_discovery_backlog() -> tuple:
    """Geographic backlog rows. Excluded from guest recommendations."""
    return tuple(_read_rows(BACKLOG_FILE_SUFFIX))


@lru_cache(maxsize=1)
def _candidates_by_id() -> Dict[str, Destination]:
    return {destination.destination_id: destination for destination in load_destination_candidates()}


def get_destination(destination_id: str) -> Optional[Destination]:
    """Look up a candidate by id. Backlog ids deliberately resolve to None."""
    return _candidates_by_id().get(destination_id)


@lru_cache(maxsize=1)
def get_catalog_version() -> Dict[str, object]:
    return {
        "snapshot_date": CATALOG_SNAPSHOT_DATE,
        "candidate_count": len(load_destination_candidates()),
        "backlog_count": len(load_discovery_backlog()),
        "candidates_file": _find_data_file(CANDIDATES_FILE_SUFFIX).name,
    }
