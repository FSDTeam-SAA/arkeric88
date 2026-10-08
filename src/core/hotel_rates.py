"""Provider-neutral live hotel enrichment.

Google Places remains Velari's discovery and emotional-fit source.  This
module only maps that already-selected property to a supplier and attaches
verified, short-lived shopping data.  No model is asked to create a price or
availability claim.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
import logging
import math
import time
from typing import Any, Optional, Protocol

import requests

from src.config.config_env import settings
from src.session.session_persistence import load_hotel_mapping, save_hotel_mapping

LOGGER = logging.getLogger(__name__)
LITEAPI_PROVIDER = "liteapi"


class HotelProviderError(RuntimeError):
    """A controlled provider error suitable for a guest-facing fallback."""


class HotelRateProvider(Protocol):
    name: str

    def match_hotel(self, google_hotel: dict) -> dict: ...

    def get_rates(self, hotel_id: str, request: "HotelRateRequest") -> dict: ...

    def prebook(self, offer_id: str) -> dict: ...


@dataclass(frozen=True)
class HotelRateRequest:
    checkin: str
    checkout: str
    guest_nationality: str
    currency: str
    occupancies: list[dict]
    nights: int


def build_occupancies(
    *, adults: int, children: int, rooms: int, child_ages: Optional[list[int]], room_occupancies: Optional[list[dict]],
) -> list[dict]:
    """Return LiteAPI's per-room occupancy payload without inventing a split."""
    if children and child_ages is None:
        raise ValueError("Child ages are required before live hotel rates can be requested.")
    if rooms == 1:
        return [{"adults": adults, "children": list(child_ages or [])}]
    if not room_occupancies:
        raise ValueError("Room-by-room occupancy is required for a multi-room live hotel search.")
    if len(room_occupancies) != rooms:
        raise ValueError("Room-by-room occupancy must contain one entry for every room.")
    result = []
    for room in room_occupancies:
        room_adults = int(room.get("adults", 0))
        room_children = list(room.get("children", []))
        if room_adults < 1 or any(not isinstance(age, int) or age < 0 or age > 17 for age in room_children):
            raise ValueError("Every room needs at least one adult and valid child ages.")
        result.append({"adults": room_adults, "children": room_children})
    if sum(room["adults"] for room in result) != adults or sum(len(room["children"]) for room in result) != children:
        raise ValueError("Room-by-room occupancy must match the travel party.")
    return result


def request_from_profile(profile, currency: str = "USD") -> HotelRateRequest:
    if not profile.has_exact_dates:
        raise ValueError("Exact check-in and check-out dates are required for live hotel rates.")
    if not profile.guest_nationality:
        raise ValueError("Guest nationality is required for live hotel rates.")
    occupancies = build_occupancies(
        adults=profile.adults,
        children=profile.children,
        rooms=profile.rooms,
        child_ages=profile.child_ages,
        room_occupancies=profile.hotel_room_occupancies,
    )
    return HotelRateRequest(
        checkin=profile.check_in_date.isoformat(),
        checkout=profile.check_out_date.isoformat(),
        guest_nationality=profile.guest_nationality,
        currency=currency,
        occupancies=occupancies,
        nights=profile.nights,
    )


def _normalise(value: Any) -> str:
    return "".join(character for character in str(value or "").lower() if character.isalnum())


def _number(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coordinates(value: dict) -> tuple[Optional[float], Optional[float]]:
    location = value.get("location") or value.get("coordinates") or {}
    latitude = value.get("latitude", value.get("lat", location.get("latitude", location.get("lat"))))
    longitude = value.get("longitude", value.get("lng", location.get("longitude", location.get("lng"))))
    return _number(latitude), _number(longitude)


def _distance_meters(left: tuple[Optional[float], Optional[float]], right: tuple[Optional[float], Optional[float]]) -> Optional[float]:
    if None in (*left, *right):
        return None
    lat1, lon1, lat2, lon2 = map(math.radians, (*left, *right))
    return 6371000 * 2 * math.asin(math.sqrt(
        math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    ))


def _address_text(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(str(item) for item in value.values() if item)
    return str(value or "")


def _price(value: Any) -> tuple[Optional[float], Optional[str]]:
    if isinstance(value, list):
        value = value[0] if value else {}
    if not isinstance(value, dict):
        return None, None
    return _number(value.get("amount")), value.get("currency")


def _rate_total(rate: dict) -> tuple[Optional[float], Optional[str]]:
    retail = rate.get("retailRate") or {}
    return _price(retail.get("total") if isinstance(retail, dict) else retail)


def _fees(rates: list[dict]) -> tuple[list[dict], list[dict], str]:
    included, payable = [], []
    seen: set[tuple] = set()
    declared = False
    for rate in rates:
        values = rate.get("taxesAndFees")
        if values is None:
            continue
        declared = True
        for fee in values if isinstance(values, list) else []:
            item = {
                "description": fee.get("description") or "Tax or fee",
                "amount": _number(fee.get("amount")),
                "currency": fee.get("currency"),
                "included": bool(fee.get("included")),
            }
            key = tuple(item.items())
            if key not in seen:
                seen.add(key)
                (included if item["included"] else payable).append(item)
    if not declared:
        return included, payable, "Supplier did not return a fee breakdown; the rate total is used without adding taxes again."
    return included, payable, "Included charges are already in the rate total; payable-at-property charges are separate."


def _rate_summary(rate: dict) -> dict:
    total, currency = _rate_total(rate)
    cancellation = rate.get("cancellationPolicies") or {}
    return {
        "rate_id": rate.get("rateId"),
        "occupancy_number": rate.get("occupancyNumber"),
        "room_name": rate.get("name"),
        "max_occupancy": rate.get("maxOccupancy"),
        "adults": rate.get("adultCount"),
        "children": rate.get("childCount"),
        "meal_plan": rate.get("boardName") or rate.get("boardType"),
        "remarks": rate.get("remarks") or [],
        "total_price": total,
        "currency": currency,
        "refundable": cancellation.get("refundableTag") == "RFN",
        "refundability": cancellation.get("refundableTag"),
        "cancellation_conditions": cancellation.get("cancelPolicyInfos") or [],
        "hotel_remarks": cancellation.get("hotelRemarks") or [],
    }


def normalise_rates(payload: dict, nights: int) -> dict:
    """Map LiteAPI's offer tree to a supplier-neutral, display-safe shape."""
    hotels = payload.get("data") or []
    normalised_hotels = []
    for hotel in hotels:
        offers = []
        for raw_offer in hotel.get("roomTypes") or []:
            rates = raw_offer.get("rates") or []
            total, currency = _price(raw_offer.get("offerRetailRate"))
            if total is None:
                totals = [_rate_total(rate)[0] for rate in rates]
                total = sum(amount for amount in totals if amount is not None) if rates and all(amount is not None for amount in totals) else None
                currency = currency or next((_rate_total(rate)[1] for rate in rates if _rate_total(rate)[1]), None)
            included, payable, disclosure = _fees(rates)
            offers.append({
                "offer_id": raw_offer.get("offerId"),
                "supplier": raw_offer.get("supplier"),
                "total_price": total,
                "currency": currency,
                "price_per_night": round(total / nights, 2) if total is not None and nights else None,
                "refundable": all((rate.get("cancellationPolicies") or {}).get("refundableTag") == "RFN" for rate in rates) if rates else None,
                "rates": [_rate_summary(rate) for rate in rates],
                "included_taxes_and_fees": included,
                "payable_at_property": payable,
                "fee_disclosure": disclosure,
            })
        offers.sort(key=lambda item: (item["total_price"] is None, item["total_price"] or 0))
        normalised_hotels.append({"hotel_id": hotel.get("hotelId"), "offers": offers, "available": bool(offers)})
    return {"hotels": normalised_hotels, "checked_at_utc": datetime.now(timezone.utc).isoformat()}


class LiteAPIProvider:
    """Nuitee Connect adapter.  All supplier-specific field handling stays here."""

    name = LITEAPI_PROVIDER

    def __init__(self, api_key: str, base_url: str, booking_base_url: str, session: Optional[requests.Session] = None):
        if not api_key:
            raise ValueError("LiteAPI requires a server-side API key.")
        self.base_url = base_url.rstrip("/")
        self.booking_base_url = booking_base_url.rstrip("/")
        self.session = session or requests.Session()
        self.headers = {"X-API-Key": api_key, "Accept": "application/json"}

    def _request(self, method: str, url: str, *, retryable: bool, **kwargs) -> dict:
        attempts = 2 if retryable else 1
        for attempt in range(attempts):
            try:
                response = self.session.request(method, url, headers=self.headers, **kwargs)
                if response.status_code < 400:
                    return response.json()
                if response.status_code not in (429, 500, 502, 503, 504) or attempt + 1 == attempts:
                    LOGGER.warning("LiteAPI request failed: method=%s status=%s", method, response.status_code)
                    raise HotelProviderError("Hotel inventory is temporarily unavailable.")
            except (requests.RequestException, ValueError) as error:
                if attempt + 1 == attempts:
                    LOGGER.warning("LiteAPI request failed: method=%s error=%s", method, type(error).__name__)
                    raise HotelProviderError("Hotel inventory is temporarily unavailable.") from error
            time.sleep(0.15 * (attempt + 1))
        raise HotelProviderError("Hotel inventory is temporarily unavailable.")

    def _candidate_score(self, google_hotel: dict, candidate: dict) -> tuple[float, Optional[float]]:
        name_score = SequenceMatcher(None, _normalise(google_hotel.get("name")), _normalise(candidate.get("name") or candidate.get("hotelName"))).ratio()
        google_address = set(_normalise(google_hotel.get("address"))[i:i + 4] for i in range(max(len(_normalise(google_hotel.get("address"))) - 3, 0)))
        candidate_address = set(_normalise(_address_text(candidate.get("address")))[i:i + 4] for i in range(max(len(_normalise(_address_text(candidate.get("address")))) - 3, 0)))
        address_score = len(google_address & candidate_address) / len(google_address | candidate_address) if google_address and candidate_address else 0.0
        distance = _distance_meters(_coordinates(google_hotel.get("coords") or {}), _coordinates(candidate))
        proximity = max(0.0, 1 - distance / 2000) if distance is not None else 0.0
        evidence = 1 if distance is not None else 0
        score = name_score * 0.70 + address_score * 0.20 + proximity * 0.10
        # A name-only match is intentionally not enough to price a different property.
        if evidence == 0 and address_score < 0.25:
            score *= 0.6
        return score, distance

    def match_hotel(self, google_hotel: dict) -> dict:
        place_id = google_hotel.get("place_id")
        if place_id:
            saved = load_hotel_mapping(place_id, self.name)
            if saved:
                return {**saved, "mapping_source": "cache"}
        coords = google_hotel.get("coords") or {}
        params: dict[str, Any] = {"hotelName": google_hotel.get("name"), "limit": 10, "timeout": 3}
        if coords.get("lat") is not None and coords.get("lng") is not None:
            params.update({"latitude": coords["lat"], "longitude": coords["lng"], "radius": 2000})
        data = self._request("GET", f"{self.base_url}/data/hotels", params=params, timeout=5, retryable=True)
        candidates = data.get("data") or data.get("hotels") or []
        scored = []
        for candidate in candidates:
            candidate_id = candidate.get("id") or candidate.get("hotelId")
            if not candidate_id:
                continue
            score, distance = self._candidate_score(google_hotel, candidate)
            scored.append((score, distance, candidate, candidate_id))
        if not scored:
            return {"status": "UNMATCHED", "reason": "No supplier property was found for this Google Places hotel."}
        score, distance, candidate, candidate_id = max(scored, key=lambda item: item[0])
        # Require a strong name plus geographic/address corroboration.
        close_enough = distance is not None and distance <= 1500
        name_similarity = SequenceMatcher(None, _normalise(google_hotel.get("name")), _normalise(candidate.get("name") or candidate.get("hotelName"))).ratio()
        if score < 0.82 or name_similarity < 0.82 or not close_enough:
            return {"status": "UNMATCHED", "reason": "No supplier property could be matched with sufficient confidence."}
        match = {
            "status": "MATCHED",
            "provider": self.name,
            "hotel_id": candidate_id,
            "confidence": round(score, 3),
            "distance_meters": round(distance) if distance is not None else None,
            "provider_name": candidate.get("name") or candidate.get("hotelName"),
            "provider_address": _address_text(candidate.get("address")),
        }
        if place_id:
            save_hotel_mapping(place_id, self.name, candidate_id, match)
        return {**match, "mapping_source": "live"}

    def get_rates(self, hotel_id: str, request: HotelRateRequest) -> dict:
        payload = {
            "hotelIds": [hotel_id], "checkin": request.checkin, "checkout": request.checkout,
            "currency": request.currency, "guestNationality": request.guest_nationality,
            "occupancies": request.occupancies, "timeout": 6, "maxRatesPerHotel": 500,
            "roomMapping": True, "includeHotelData": True,
        }
        response = self._request("POST", f"{self.base_url}/hotels/rates", json=payload, timeout=10, retryable=True)
        return normalise_rates(response, request.nights)

    def prebook(self, offer_id: str) -> dict:
        if not offer_id:
            raise ValueError("An offer ID is required for prebooking.")
        # This validates an offer only.  It neither collects payment nor books.
        return self._request(
            "POST", f"{self.booking_base_url}/rates/prebook", json={"offerId": offer_id, "usePaymentSdk": False},
            params={"timeout": 120}, timeout=125, retryable=False,
        )


def get_liteapi_provider() -> Optional[LiteAPIProvider]:
    if not settings.liteapi_enabled or not settings.liteapi_api_key:
        return None
    return LiteAPIProvider(settings.liteapi_api_key, settings.liteapi_base_url, settings.liteapi_booking_base_url)


def enrich_hotel_with_live_rates(hotel: dict, request: HotelRateRequest, provider: Optional[HotelRateProvider] = None) -> dict:
    """Return a copy of a Google-selected hotel enriched or clearly not live."""
    enriched = dict(hotel)
    provider = provider or get_liteapi_provider()
    if provider is None:
        enriched["live_rate_status"] = "NOT_CONFIGURED"
        return enriched
    try:
        mapping = provider.match_hotel(enriched)
        enriched["provider_mapping"] = mapping
        if mapping.get("status") != "MATCHED":
            enriched.update(live_rate_status="UNMATCHED", availability_status="UNMATCHED")
            return enriched
        rates = provider.get_rates(mapping["hotel_id"], request)
        hotel_rates = rates["hotels"][0] if rates["hotels"] else {"offers": [], "available": False}
        enriched["live_rates"] = {**hotel_rates, "checked_at_utc": rates["checked_at_utc"], "provider": provider.name}
        if not hotel_rates["available"]:
            enriched.update(live_rate_status="UNAVAILABLE", availability_status="UNAVAILABLE")
            return enriched
        offer = hotel_rates["offers"][0]
        enriched.update(
            live_rate_status="LIVE", price_status="LIVE", availability_status="AVAILABLE",
            live_total=offer["total_price"], live_currency=offer["currency"],
            nightly_usd=round(offer["total_price"] / request.nights / len(request.occupancies), 2) if offer["total_price"] is not None else None,
        )
        return enriched
    except (HotelProviderError, ValueError) as error:
        LOGGER.info("Live hotel rates unavailable: provider=%s reason=%s", getattr(provider, "name", "unknown"), type(error).__name__)
        enriched.update(live_rate_status="UNAVAILABLE", availability_status="UNAVAILABLE")
        return enriched
