"""
Monthly climate normals from recorded weather (Open-Meteo historical archive).

Used by the travel-date recommendation: for a known point (the chosen
destination, or the departure area for a nearby trip) it returns, per month,
the average daily high and low and the average number of rainy days over the
last CLIMATE_YEARS full years. Free, no API key. Failures are never fatal:
callers get {"error": ...} and fall back to general seasonality.
"""

from datetime import date
from functools import lru_cache
from statistics import mean

import requests

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
CLIMATE_YEARS = 5
RAINY_DAY_MM = 1.0


def _summarize(daily: dict) -> dict:
    by_month: dict = {}
    for day, high, low, rain in zip(
        daily.get("time", []),
        daily.get("temperature_2m_max", []),
        daily.get("temperature_2m_min", []),
        daily.get("precipitation_sum", []),
    ):
        if high is None or low is None:
            continue
        year, month = int(day[:4]), int(day[5:7])
        record = by_month.setdefault(month, {"highs": [], "lows": [], "rain_days": {}})
        record["highs"].append(high)
        record["lows"].append(low)
        record["rain_days"].setdefault(year, 0)
        if rain is not None and rain >= RAINY_DAY_MM:
            record["rain_days"][year] += 1
    months = {}
    for month, record in sorted(by_month.items()):
        months[month] = {
            "avg_high_c": round(mean(record["highs"]), 1),
            "avg_low_c": round(mean(record["lows"]), 1),
            "rainy_days": round(mean(record["rain_days"].values()), 1),
        }
    return months


@lru_cache(maxsize=256)
def _monthly_climate(latitude: float, longitude: float, last_year: int) -> dict:
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "start_date": f"{last_year - CLIMATE_YEARS + 1}-01-01",
        "end_date": f"{last_year}-12-31",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum",
        "timezone": "auto",
    }
    try:
        response = requests.get(ARCHIVE_URL, params=params, timeout=30)
        if response.status_code != 200:
            return {"error": f"HTTP {response.status_code}"}
        months = _summarize(response.json().get("daily", {}))
    except Exception as error:
        return {"error": str(error)}
    if len(months) < 12:
        return {"error": "Incomplete climate record."}
    return {
        "source": "Open-Meteo historical weather archive",
        "period": f"{params['start_date'][:4]}-{last_year}",
        "months": months,
    }


def monthly_climate(latitude: float, longitude: float) -> dict:
    """Per-month normals for a point, cached per ~10 km cell."""
    result = _monthly_climate(round(latitude, 1), round(longitude, 1), date.today().year - 1)
    return dict(result)
