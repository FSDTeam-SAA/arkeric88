"""
Price breakdown calculated only from the itinerary the guest can see
(client brief, Priority 4).

Lines: accommodation (nightly rate x nights x rooms per stay), experiences
and dining (per person x guests), local transportation and transfers (for
the whole party), and taxes/fees/gratuities (stated as not included). Every
amount is an estimate. The total is the exact sum of the lines and is
withheld, with a reason, when a required line has no price.
"""

from typing import List, Optional

from src.core.itinerary_geo import EXPERIENCE, MEAL, TRANSFER

CURRENCY = "USD"
# Assumed cost of getting around by taxi or ride-hail, for the whole party.
LOCAL_TRANSPORT_USD_PER_KM = 0.9
TRANSFER_USD_PER_KM = 0.9

# Per-person meal estimates at a moderate restaurant, scaled by the
# restaurant's Google price level when it has one.
MEAL_BASE_COST = {"Breakfast": 18.0, "Lunch": 30.0, "Dinner": 55.0}
DINING_LEVEL_MULTIPLIER = {
    "PRICE_LEVEL_FREE": 0.0,
    "PRICE_LEVEL_INEXPENSIVE": 0.6,
    "PRICE_LEVEL_MODERATE": 1.0,
    "PRICE_LEVEL_EXPENSIVE": 1.7,
    "PRICE_LEVEL_VERY_EXPENSIVE": 2.6,
}

WHAT_MAY_VARY = (
    "Estimates only. Final prices depend on your dates, live availability, the rooms you book, "
    "menu choices, taxes, fees and gratuities."
)


def meal_cost(meal: str, price_level: Optional[str] = None) -> float:
    base = MEAL_BASE_COST.get(meal, MEAL_BASE_COST["Lunch"])
    return round(base * DINING_LEVEL_MULTIPLIER.get(price_level or "", 1.0), 2)


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'s' if count != 1 else ''}"


def _round(amount: Optional[float]) -> Optional[float]:
    return None if amount is None else round(float(amount), 2)


def build_price_breakdown(stops: List[dict], days: List[dict], party_size: int, rooms: int, nights: int) -> dict:
    party_size, rooms = max(int(party_size or 1), 1), max(int(rooms or 1), 1)
    items = [item for day in days for item in day.get("activities", [])]

    # Accommodation: one detail row per stay.
    stays, accommodation, missing = [], 0.0, []
    for stop in stops:
        nightly = stop.get("nightly_usd")
        hotel_name = (stop.get("hotel") or {}).get("name", stop["base_area"])
        subtotal = None if nightly is None else nightly * stop["nights"] * rooms
        stays.append({
            "stay": hotel_name,
            "base_area": stop["base_area"],
            "nightly_rate": _round(nightly),
            "nights": stop["nights"],
            "rooms": rooms,
            "subtotal": _round(subtotal),
            "basis": "per room, per night",
        })
        if subtotal is None:
            missing.append(f"no nightly price for {hotel_name}")
        else:
            accommodation += subtotal

    experiences_pp = sum(float(item.get("activity_cost") or 0) for item in items if item.get("item_type", EXPERIENCE) == EXPERIENCE)
    dining_pp = sum(float(item.get("activity_cost") or 0) for item in items if item.get("item_type") == MEAL)

    local_km = sum(
        float(item.get("distance_from_previous_km") or 0) + float(item.get("return_to_base_km") or 0)
        for item in items if item.get("item_type") not in (TRANSFER,)
    )
    transfer_km = sum(float(item.get("transfer_km") or 0) for item in items if item.get("item_type") == TRANSFER)
    transport = local_km * LOCAL_TRANSPORT_USD_PER_KM + transfer_km * TRANSFER_USD_PER_KM

    guests = _plural(party_size, "guest")
    lines = [
        {
            "category": "accommodation",
            "label": "Accommodation",
            "amount": None if missing else _round(accommodation),
            "basis": f"Nightly rate × nights × {_plural(rooms, 'room')}",
            "details": stays,
        },
        {
            "category": "experiences",
            "label": "Experiences",
            "amount": _round(experiences_pp * party_size),
            "per_person": _round(experiences_pp),
            "basis": f"Per person × {guests}",
        },
        {
            "category": "dining",
            "label": "Dining",
            "amount": _round(dining_pp * party_size),
            "per_person": _round(dining_pp),
            "basis": f"Per person × {guests}",
        },
        {
            "category": "local_transportation",
            "label": "Local transportation and transfers",
            "amount": _round(transport),
            "basis": f"For the whole party (about {round(local_km + transfer_km)} km by road)",
        },
        {
            "category": "taxes_fees_gratuities",
            "label": "Taxes, fees and gratuities",
            "amount": None,
            "basis": "Not included; confirmed when you book",
        },
    ]
    required = [line for line in lines if line["category"] != "taxes_fees_gratuities"]
    total = None if any(line["amount"] is None for line in required) else _round(sum(line["amount"] for line in required))
    return {
        "currency": CURRENCY,
        "status": "ESTIMATED",
        "applies_to": f"Full trip for {guests} in {_plural(rooms, 'room')}, {_plural(nights, 'night')}",
        "lines": lines,
        "total": total,
        "total_label": "Estimated trip total (before taxes, fees and gratuities)",
        "total_withheld_reason": (
            None if total is not None
            else "We can't show a reliable total yet: " + "; ".join(missing) + "."
        ),
        "what_may_vary": WHAT_MAY_VARY,
    }
