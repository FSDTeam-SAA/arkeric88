"""
Viator Partner API v2 client (affiliate, Full access).

Endpoints used (https://docs.viator.com/partner-api/technical/):
- GET  /destinations                      Viator destination IDs and centre points (cached per process)
- POST /search/freetext                   products matching an experience name in a destination
- GET  /availability/schedules/{code}     whether a product runs on a date, and its price that day
- POST /exchange-rates                    convert schedule prices (supplier currency) to USD

Viator asks partners to use /availability/check only right before booking,
so it is not called while building an itinerary. Every function returns
{"error": ...} instead of raising, and is a no-op without VIATOR_API_KEY.
"""

from functools import lru_cache
from typing import Optional

import requests

from src.config.config_env import settings

API_VERSION = "application/json;version=2.0"
TIMEOUT_SECONDS = 20
CURRENCY = "USD"


def viator_enabled() -> bool:
    return bool(settings.viator_api_key)


def _headers() -> dict:
    return {
        "exp-api-key": settings.viator_api_key,
        "Accept": API_VERSION,
        "Content-Type": API_VERSION,
        "Accept-Language": "en-US",
    }


def _get(path: str) -> dict:
    if not viator_enabled():
        return {"error": "Viator is not configured."}
    try:
        response = requests.get(f"{settings.viator_base_url}{path}", headers=_headers(), timeout=TIMEOUT_SECONDS)
        if response.status_code != 200:
            return {"error": f"HTTP {response.status_code}: {response.text[:300]}"}
        return response.json()
    except Exception as error:
        return {"error": str(error)}


def _post(path: str, body: dict) -> dict:
    if not viator_enabled():
        return {"error": "Viator is not configured."}
    try:
        response = requests.post(f"{settings.viator_base_url}{path}", json=body, headers=_headers(), timeout=TIMEOUT_SECONDS)
        if response.status_code != 200:
            return {"error": f"HTTP {response.status_code}: {response.text[:300]}"}
        return response.json()
    except Exception as error:
        return {"error": str(error)}


@lru_cache(maxsize=1)
def _destinations_cached() -> tuple:
    result = _get("/destinations")
    if "error" in result:
        raise RuntimeError(result["error"])  # not cached: lru_cache skips raised calls
    return tuple(
        {
            "destination_id": item.get("destinationId"),
            "name": item.get("name"),
            "type": item.get("type"),
            "latitude": (item.get("center") or {}).get("latitude"),
            "longitude": (item.get("center") or {}).get("longitude"),
        }
        for item in result.get("destinations", [])
        if item.get("destinationId") is not None
    )


def get_destinations() -> list:
    """All Viator destinations with their centre points ([] when unavailable)."""
    try:
        return list(_destinations_cached())
    except Exception:
        return []


def search_products(term: str, destination_id, start_date: Optional[str] = None,
                    end_date: Optional[str] = None, count: int = 10) -> dict:
    """Active products in a destination matching a free-text term, priced in USD."""
    filtering = {"destination": str(destination_id)}
    if start_date and end_date:
        filtering["dateRange"] = {"from": start_date, "to": end_date}
    result = _post("/search/freetext", {
        "searchTerm": term,
        "productFiltering": filtering,
        "searchTypes": [{"searchType": "PRODUCTS", "pagination": {"start": 1, "count": count}}],
        "currency": CURRENCY,
    })
    if "error" in result:
        return result
    return {"products": (result.get("products") or {}).get("results", [])}


def availability_schedule(product_code: str) -> dict:
    """Seasons, operating days, unavailable dates and per-age-band prices for a product."""
    return _get(f"/availability/schedules/{product_code}")


@lru_cache(maxsize=32)
def _usd_rate_cached(currency: str) -> float:
    result = _post("/exchange-rates", {"sourceCurrencies": [currency], "targetCurrencies": [CURRENCY]})
    for rate in result.get("rates", []) if "error" not in result else []:
        if rate.get("sourceCurrency") == currency and rate.get("targetCurrency") == CURRENCY and rate.get("rate"):
            return float(rate["rate"])
    raise RuntimeError(result.get("error", "No exchange rate."))


def usd_rate(currency: str) -> Optional[float]:
    """Viator's rate from `currency` to USD (1.0 for USD), or None when it cannot be fetched."""
    if not currency or currency == CURRENCY:
        return 1.0
    try:
        return _usd_rate_cached(currency)
    except Exception:
        return None


def clear_caches() -> None:
    """For tests."""
    _destinations_cached.cache_clear()
    _usd_rate_cached.cache_clear()
