"""Read and rank the structured retreat workbook without an external RAG service."""

from functools import lru_cache
from pathlib import Path
import re
from zipfile import ZipFile
from xml.etree import ElementTree


WORKBOOK_PATH = Path(__file__).resolve().parents[2] / "data" / "Retreat Master Database.xlsx"
XML_NAMESPACE = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def _normalize(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _column_index(cell_reference: str) -> int:
    letters = re.match(r"[A-Z]+", cell_reference or "A")
    index = 0
    for letter in (letters.group(0) if letters else "A"):
        index = index * 26 + ord(letter) - ord("A") + 1
    return index - 1


def _cell_value(cell, shared_strings: list[str]) -> str:
    value = cell.find("m:v", XML_NAMESPACE)
    if cell.attrib.get("t") == "inlineStr":
        return "".join(
            text.text or "" for text in cell.findall(".//m:t", XML_NAMESPACE)
        )
    if value is None:
        return ""
    if cell.attrib.get("t") == "s":
        return shared_strings[int(value.text)]
    return value.text or ""


@lru_cache(maxsize=1)
def load_retreat_catalog() -> list[dict]:
    """Load the first worksheet into dictionaries using Python's standard library."""
    if not WORKBOOK_PATH.exists():
        return []

    with ZipFile(WORKBOOK_PATH) as workbook:
        shared_root = ElementTree.fromstring(workbook.read("xl/sharedStrings.xml"))
        shared_strings = [
            "".join(text.text or "" for text in item.findall(".//m:t", XML_NAMESPACE))
            for item in shared_root.findall("m:si", XML_NAMESPACE)
        ]
        sheet_root = ElementTree.fromstring(workbook.read("xl/worksheets/sheet1.xml"))

    rows: list[list[str]] = []
    for row in sheet_root.findall(".//m:row", XML_NAMESPACE):
        values: list[str] = []
        for cell in row.findall("m:c", XML_NAMESPACE):
            column = _column_index(cell.attrib.get("r", "A1"))
            while len(values) <= column:
                values.append("")
            values[column] = _cell_value(cell, shared_strings)
        rows.append(values)

    if len(rows) < 4:
        return []
    # The source workbook includes a Primary Modalities header but its data rows
    # omit that column, so remove the empty header to preserve the financial fields.
    headers = rows[2][:26] + rows[2][27:]
    return [
        {
            headers[index].strip(): value.strip()
            for index, value in enumerate(row)
            if index < len(headers) and headers[index].strip()
        }
        for row in rows[3:]
        if len(row) > 1 and row[1].strip()
    ]


def _nightly_price(record: dict) -> float | None:
    match = re.search(r"[\d,]+", record.get("Avg Night", ""))
    return float(match.group(0).replace(",", "")) if match else None


# ==================== V2 MATCHING HELPERS ====================
# Stable identifiers and structured-field parsing used by the deterministic
# retreat-matching engine (src/core/retreat_scoring.py, retreat_ranker.py).
# The workbook has no dedicated "Property ID" column yet (see
# BACKEND_DEVELOPER_CHANGES.md), so IDs are derived from the "#" row number,
# which is stable for the life of a loaded workbook because load_retreat_catalog
# is cached and the row order never changes without a code deploy.

MONTH_ABBREVIATIONS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


@lru_cache(maxsize=1)
def get_database_version() -> str:
    """
    Lightweight fingerprint (row count + workbook mtime) so every recommendation
    session can record which snapshot of the retreat database produced it, per
    BACKEND_DEVELOPER_CHANGES.md "Record the database version ... used for each
    shortlist." This is not a substitute for a real migration/version table --
    it is a lower-effort stand-in until the workbook is replaced by a proper
    database with schema versioning.
    """
    row_count = len(load_retreat_catalog())
    try:
        mtime = int(WORKBOOK_PATH.stat().st_mtime)
    except OSError:
        mtime = 0
    return f"retreat-master-{row_count}rows-{mtime}"


def make_property_id(record: dict) -> str:
    """Build a stable, human-auditable property id from the workbook row number."""
    raw_number = re.sub(r"\D", "", str(record.get("#", "")).strip())
    return f"retreat_{int(raw_number):03d}" if raw_number else "retreat_unknown"


@lru_cache(maxsize=1)
def _catalog_by_property_id() -> dict[str, dict]:
    return {make_property_id(record): record for record in load_retreat_catalog()}


def get_retreat_by_property_id(property_id: str) -> dict | None:
    """Look up a single catalog record by its stable property id."""
    return _catalog_by_property_id().get(property_id)


def split_multi_value(raw: str) -> list[str]:
    """Split a comma-separated workbook cell (e.g. Archetypes, Transform Focus) into tokens."""
    return [token.strip() for token in (raw or "").split(",") if token.strip()]


def parse_avg_night(record: dict) -> dict:
    """
    Parse the "Avg Night" cell into a structured, auditable shape.

    Returns:
        {
          "amount": float | None,         # None when price is not numeric (Donation)
          "is_lower_bound": bool,          # True for "$2,500+" style values
          "is_donation": bool,
          "raw": str,                      # original workbook text, always preserved
        }
    """
    raw = (record.get("Avg Night") or "").strip()
    is_donation = raw.lower().startswith("donation")
    match = re.search(r"[\d,]+", raw)
    amount = float(match.group(0).replace(",", "")) if match and not is_donation else None
    return {
        "amount": amount,
        "is_lower_bound": "+" in raw and amount is not None,
        "is_donation": is_donation,
        "raw": raw,
    }


def parse_best_season(record: dict) -> list[int]:
    """
    Normalize the "Best Season" text into a sorted list of month numbers (1-12).

    "Year-round" and "Monthly" both resolve to all 12 months: "Year-round" means the
    property welcomes guests continuously, and the source "Monthly" values in this
    workbook describe month-to-month program availability rather than a narrower
    seasonal window, so for the purposes of a season *filter* they are equivalent.
    The original text is never discarded by callers -- this function only produces
    the derived month list used for matching.
    Ranges that cross the year boundary (e.g. "Nov-Apr") are unwrapped correctly.
    """
    raw = (record.get("Best Season") or "").strip()
    if not raw or raw.lower() in {"year-round", "year round", "monthly"}:
        return list(range(1, 13))
    months: set[int] = set()
    for chunk in raw.split(","):
        words = re.findall(r"[A-Za-z]+", chunk)
        if len(words) >= 2:
            start = MONTH_ABBREVIATIONS.get(words[0][:3].lower())
            end = MONTH_ABBREVIATIONS.get(words[1][:3].lower())
            if start and end:
                if start <= end:
                    months.update(range(start, end + 1))
                else:
                    months.update(range(start, 13))
                    months.update(range(1, end + 1))
        elif len(words) == 1:
            single = MONTH_ABBREVIATIONS.get(words[0][:3].lower())
            if single:
                months.add(single)
    return sorted(months)


def _geography_score(record: dict, location: str) -> int:
    target = _normalize(location)
    if not target:
        return 0
    property_name = _normalize(record.get("Property Name"))
    region = _normalize(record.get("Region"))
    country = _normalize(record.get("Country"))
    if target == region or target == country:
        return 160
    if target in region or region in target:
        return 145
    if target in property_name:
        return 135
    if target in country or country in target:
        return 120
    return 0


def find_retreat_candidates(
    city_name: str,
    preferred_region: str = "",
    facility_terms: str = "",
    nightly_budget: float | None = None,
    limit: int = 5,
) -> list[dict]:
    """Rank retreats by geography first, facilities/profile second, and budget third."""
    catalog = load_retreat_catalog()
    if not catalog:
        return []

    city_matches = [item for item in catalog if _geography_score(item, city_name) > 0]
    region_matches = [
        item for item in catalog if _geography_score(item, preferred_region) > 0
    ]
    # The chosen destination is most specific, then the requested broader region.
    pool = city_matches or region_matches
    if not pool:
        return []
    desired_tokens = set(_normalize(facility_terms).split())

    def score(record: dict) -> tuple[float, float, float]:
        geography = max(
            _geography_score(record, city_name),
            _geography_score(record, preferred_region),
        )
        searchable_fields = (
            "Core Philosophy",
            "Primary Category",
            "Emotional Tone",
            "Structure",
            "Physical Intensity",
            "Transform Focus",
            "Archetypes",
            "Primary Modalities",
            "Diagnostics",
            "Spa",
            "Nutrition",
            "Mindfulness",
            "Fitness",
        )
        haystack = _normalize(" ".join(record.get(field, "") for field in searchable_fields))
        facility_fit = float(sum(1 for token in desired_tokens if token in haystack))
        price = _nightly_price(record)
        if nightly_budget and price is not None:
            budget_fit = 20 if price <= nightly_budget else -min(20, (price - nightly_budget) / nightly_budget * 20)
        else:
            budget_fit = 0
        return geography, facility_fit, budget_fit

    return sorted(pool, key=score, reverse=True)[:limit]


def retreat_facilities(record: dict) -> list[str]:
    """Expose the workbook's useful wellness offerings as a compact list."""
    facilities = []
    modalities = record.get("Primary Modalities", "").strip()
    if modalities:
        facilities.append(modalities)
    for field in ("Diagnostics", "Spa", "Nutrition", "Mindfulness", "Fitness"):
        value = record.get(field, "").strip()
        if value and _normalize(value) not in {"no", "none", "0"}:
            facilities.append(f"{field}: {value}")
    return facilities
