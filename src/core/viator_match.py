"""
Attach Viator products to itinerary experiences (Viator Partner API, Full access).

For each planned experience, the nearest Viator destination to the stop's
base is searched for a product with the same name. A product is attached only
when its title genuinely matches (shared meaningful words), never by
similarity alone. It then supplies:

- a bookable Viator product (code, title, booking link with our partner ID,
  rating and review count);
- a per-person price: the price for the travel date from Viator's
  availability schedule when the guest has exact dates, otherwise Viator's
  "from" price;
- whether the product runs on that date according to its schedule.

A schedule is not a live availability check (that happens at booking), so an
experience is marked SCHEDULED, never CONFIRMED.
"""

import re
from datetime import date
from typing import Callable, Dict, List, Optional, Tuple

from src.core.geography import great_circle_km
from src.core.itinerary_geo import EXPERIENCE
from src.core.travel_time import point_of
from src.tools import viator

MAX_DESTINATION_KM = 60.0
# Viator destinations that are too broad to stand for a stop's base.
BROAD_DESTINATION_TYPES = {"COUNTRY", "REGION", "CONTINENT", "STATE", "PROVINCE"}
WEEKDAY_CODES = ("MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY")
ADULT_BANDS = ("ADULT", "TRAVELER")

_FILLER = {
    "a", "an", "and", "at", "by", "for", "from", "in", "of", "on", "the", "to", "with", "tour", "tours",
    "trip", "visit", "experience", "day", "half", "full", "private", "small", "group", "guided", "ticket",
    "tickets", "admission", "entry", "morning", "afternoon", "evening", "local",
}


def _tokens(text: str) -> set:
    return {token for token in re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).split()
            if token not in _FILLER and len(token) > 1}


def nearest_destination(point: Tuple[float, float], destinations: List[dict]) -> Optional[dict]:
    """The closest town- or city-level Viator destination within MAX_DESTINATION_KM."""
    best, best_km = None, MAX_DESTINATION_KM
    for destination in destinations:
        if destination.get("type") in BROAD_DESTINATION_TYPES:
            continue
        if destination.get("latitude") is None or destination.get("longitude") is None:
            continue
        km = great_circle_km(point[0], point[1], destination["latitude"], destination["longitude"])
        if km <= best_km:
            best, best_km = destination, km
    return best


def best_product(experience_name: str, products: List[dict], place_name: str = "") -> Optional[dict]:
    """A product whose title shares at least two meaningful words with the experience (one for one-word names)."""
    wanted = _tokens(experience_name) | _tokens(place_name)
    if not wanted:
        return None
    required = min(2, len(_tokens(experience_name)) or 1)
    scored = []
    for product in products:
        overlap = len(wanted & _tokens(product.get("title", "")))
        if overlap >= required:
            reviews = (product.get("reviews") or {}).get("totalReviews") or 0
            scored.append((overlap, reviews, product))
    if not scored:
        return None
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return scored[0][2]


def _in_season(season: dict, travel_date: date) -> bool:
    start = season.get("startDate")
    end = season.get("endDate")
    return (not start or date.fromisoformat(start) <= travel_date) and (not end or travel_date <= date.fromisoformat(end))


def schedule_on(schedule: dict, travel_date: date) -> dict:
    """
    {"runs": True | False | None, "adult_price": float | None, "currency": str}
    from an availability schedule: runs is None when the schedule says nothing.
    """
    weekday = WEEKDAY_CODES[travel_date.weekday()]
    iso = travel_date.isoformat()
    covered, runs, prices = False, False, []
    for item in schedule.get("bookableItems", []) or []:
        for season in item.get("seasons", []) or []:
            if not _in_season(season, travel_date):
                continue
            covered = True  # The date is in a season: no matching record means it doesn't run that day.
            for record in season.get("pricingRecords", []) or []:
                if weekday not in (record.get("daysOfWeek") or WEEKDAY_CODES):
                    continue
                if any(entry.get("date") == iso for entry in record.get("unavailableDates", []) or []):
                    continue
                timed = record.get("timedEntries")
                if timed and all(any(entry.get("date") == iso for entry in slot.get("unavailableDates", []) or [])
                                 for slot in timed):
                    continue
                runs = True
                for detail in record.get("pricingDetails", []) or []:
                    if detail.get("ageBand") in ADULT_BANDS:
                        price = ((detail.get("price") or {}).get("original") or {}).get("recommendedRetailPrice")
                        if price is not None:
                            prices.append(float(price))
    return {
        "runs": runs if covered else None,
        "adult_price": min(prices) if prices else None,
        "currency": schedule.get("currency"),
    }


def _product_info(product: dict) -> dict:
    reviews = product.get("reviews") or {}
    pricing = product.get("pricing") or {}
    return {
        "product_code": product.get("productCode"),
        "title": product.get("title"),
        "booking_url": product.get("productUrl"),
        "rating": reviews.get("combinedAverageRating"),
        "review_count": reviews.get("totalReviews"),
        "from_price": (pricing.get("summary") or {}).get("fromPrice"),
        "currency": pricing.get("currency") or viator.CURRENCY,
        "runs_on_date": None,
        "date_price": None,
    }


def attach_viator_products(
    days: List[dict],
    stops: List[dict],
    travel_dates: Optional[Dict[int, date]] = None,
    search: Callable = None,
    schedule: Callable = None,
    destinations: Optional[List[dict]] = None,
    usd_rate: Callable = None,
) -> List[dict]:
    """
    Attach Viator products to the experiences in `days` (in place). Returns
    adjustments with a guest note for each experience that is not scheduled
    on its planned date.
    """
    if not viator.viator_enabled() and search is None:
        return []
    search = search or viator.search_products
    schedule = schedule or viator.availability_schedule
    usd_rate = usd_rate or viator.usd_rate
    destinations = destinations if destinations is not None else viator.get_destinations()
    if not destinations:
        return []

    notes: List[dict] = []
    by_stop = {stop["stop"]: stop for stop in stops}
    viator_destination_for: Dict[int, Optional[dict]] = {}
    for day in days:
        stop = by_stop.get(day.get("stop", 1))
        base = point_of((stop or {}).get("base") or {})
        if base is None:
            continue
        if stop["stop"] not in viator_destination_for:
            viator_destination_for[stop["stop"]] = nearest_destination(base, destinations)
        viator_destination = viator_destination_for[stop["stop"]]
        if viator_destination is None:
            continue
        travel_date = (travel_dates or {}).get(day.get("day"))
        for item in day.get("activities", []):
            if item.get("item_type", EXPERIENCE) != EXPERIENCE:
                continue
            iso = travel_date.isoformat() if travel_date else None
            result = search(item.get("activity_name", ""), viator_destination["destination_id"], iso, iso)
            if "error" in result:
                continue
            product = best_product(item.get("activity_name", ""), result.get("products", []))
            if product is None:
                continue
            info = _product_info(product)
            price, source = info["from_price"], "viator_from_price"
            if travel_date and info["product_code"]:
                details = schedule(info["product_code"])
                if "error" not in details:
                    on_date = schedule_on(details, travel_date)
                    info["runs_on_date"] = on_date["runs"]
                    if on_date["runs"] is False:
                        notes.append({
                            "type": "not_scheduled",
                            "day": day.get("day"),
                            "item": item.get("activity_name", ""),
                            "replacement": None,
                            "travel_minutes": None,
                            "guest_note": (
                                f"Day {day.get('day')}: {info['title']} isn't scheduled on Viator for "
                                f"{travel_date.strftime('%A')} {travel_date.day} {travel_date.strftime('%B')}, "
                                "so choose another day or option when you book."
                            ),
                        })
                    elif on_date["runs"] and on_date["adult_price"] is not None:
                        rate = usd_rate(on_date["currency"])
                        if rate is not None:
                            info["date_price"] = round(on_date["adult_price"] * rate, 2)
                            price, source = info["date_price"], "viator_schedule"
            item["viator"] = info
            if price is not None:
                item["activity_cost"] = round(float(price), 2)
                item["price_source"] = source
            if info["runs_on_date"]:
                item["availability_status"] = "SCHEDULED"
    return notes
