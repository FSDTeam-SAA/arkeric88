"""
Modelled accommodation price estimates (PRICE_ESTIMATES_1000), joined to the
catalogue by destination_id.

The 21-column destination import is not touched: the estimates live in their
own file (data/*PRICE_ESTIMATES_1000.csv, exported from the workbook sheet of
the same name) and are looked up by ID. Values are USD per room, per night,
for two adults in one room. They are modelled from regional benchmarks, not
hotel quotes, so they only ever rank destinations and are always shown as
estimates; live LiteAPI rates replace them when they exist.

The file is optional. Without it every destination's budget fit is UNKNOWN,
as before. Column names are matched loosely ("low_usd_per_night",
"Typical nightly (USD)", ...) because the sheet is maintained by hand.
"""

import csv
import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional

from src.core.destination_catalog import DATA_DIR, load_destination_candidates

logger = logging.getLogger(__name__)

PRICE_ESTIMATES_FILE_SUFFIX = "PRICE_ESTIMATES_1000.csv"

# Budget fit labels, also used as verification.budget_status.
FITS = "ESTIMATE_FITS"
MAY_STRETCH = "ESTIMATE_MAY_STRETCH"
LIKELY_ABOVE = "ESTIMATE_LIKELY_ABOVE"
UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class PriceEstimate:
    low: float
    typical: float
    high: float


def _price_file() -> Optional[Path]:
    matches = sorted(DATA_DIR.glob(f"*{PRICE_ESTIMATES_FILE_SUFFIX}"))
    if len(matches) > 1:
        raise RuntimeError(f"Multiple files ending in '{PRICE_ESTIMATES_FILE_SUFFIX}' found in {DATA_DIR}: {matches}")
    return matches[0] if matches else None


def _column(headers: List[str], *, any_of: tuple, none_of: tuple = ()) -> Optional[str]:
    """The header naming a price column, preferring one that mentions USD/night/price."""
    candidates = [
        header for header in headers
        if any(word in header.lower() for word in any_of) and not any(word in header.lower() for word in none_of)
    ]
    priced = [header for header in candidates if re.search(r"usd|night|price|rate|cost|\$", header, re.IGNORECASE)]
    return (priced or candidates or [None])[0]


def _amount(raw: Optional[str]) -> Optional[float]:
    try:
        value = float(re.sub(r"[^0-9.]", "", raw or ""))
    except ValueError:
        return None
    return value if value > 0 else None


def parse_price_rows(rows: List[Dict[str, str]]) -> Dict[str, PriceEstimate]:
    if not rows:
        return {}
    headers = list(rows[0])
    id_column = next((header for header in headers if header.strip().lower() == "destination_id"), None)
    low = _column(headers, any_of=("low", "min"), none_of=("high",))
    typical = _column(headers, any_of=("typical", "median", "mid", "average"))
    high = _column(headers, any_of=("high", "max"), none_of=("low",))
    if not (id_column and low and typical and high):
        raise ValueError(
            "The price estimates file needs destination_id plus low, typical and high nightly columns; "
            f"found {headers}."
        )
    estimates: Dict[str, PriceEstimate] = {}
    for row in rows:
        destination_id = (row.get(id_column) or "").strip()
        values = [_amount(row.get(column)) for column in (low, typical, high)]
        if not destination_id or None in values:
            logger.warning("Price estimate skipped for %r: missing value.", destination_id)
            continue
        low_value, typical_value, high_value = sorted(values)
        if destination_id in estimates:
            logger.warning("Price estimate skipped: duplicate destination_id %r.", destination_id)
            continue
        estimates[destination_id] = PriceEstimate(low_value, typical_value, high_value)
    return estimates


@lru_cache(maxsize=1)
def load_price_estimates() -> Dict[str, PriceEstimate]:
    path = _price_file()
    if path is None:
        return {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = [{(key or "").strip(): (value or "").strip() for key, value in row.items() if key} for row in csv.DictReader(handle)]
    estimates = parse_price_rows(rows)
    known = {destination.destination_id for destination in load_destination_candidates()}
    unknown = sorted(set(estimates) - known)
    if unknown:
        logger.warning("%d price estimates name no catalogue destination, e.g. %s.", len(unknown), unknown[:3])
    missing = len(known - set(estimates))
    if missing:
        logger.warning("%d catalogue destinations have no price estimate.", missing)
    return {destination_id: estimate for destination_id, estimate in estimates.items() if destination_id in known}


def price_estimate(destination_id: str) -> Optional[PriceEstimate]:
    return load_price_estimates().get(destination_id)


def budget_fit(estimate: Optional[PriceEstimate], budget_per_night: float, open_ended: bool) -> str:
    """Compare a per-room, per-night estimate with the guest's per-room, per-night budget."""
    if estimate is None or not budget_per_night:
        return UNKNOWN
    if open_ended or estimate.typical <= budget_per_night:
        return FITS
    if estimate.low <= budget_per_night:
        return MAY_STRETCH
    return LIKELY_ABOVE


def coverage() -> Dict[str, object]:
    path = _price_file()
    return {
        "file": path.name if path else None,
        "destinations_with_estimates": len(load_price_estimates()),
    }
