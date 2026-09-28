"""Country-to-region validation for hard destination constraints."""

import re


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z]+", " ", (value or "").lower()).strip()


REGION_COUNTRIES = {
    "north america": {
        "antigua and barbuda", "bahamas", "barbados", "belize", "canada",
        "costa rica", "cuba", "dominica", "dominican republic", "el salvador",
        "grenada", "guatemala", "haiti", "honduras", "jamaica", "mexico",
        "nicaragua", "panama", "saint kitts and nevis", "saint lucia",
        "saint vincent and the grenadines", "trinidad and tobago",
        "united states", "united states of america", "usa",
    },
    "south america": {
        "argentina", "bolivia", "brazil", "chile", "colombia", "ecuador",
        "guyana", "paraguay", "peru", "suriname", "uruguay", "venezuela",
    },
    "europe": {
        "albania", "andorra", "austria", "belarus", "belgium",
        "bosnia and herzegovina", "bulgaria", "croatia", "cyprus", "czechia",
        "czech republic", "denmark", "estonia", "finland", "france", "germany",
        "greece", "hungary", "iceland", "ireland", "italy", "kosovo", "latvia",
        "liechtenstein", "lithuania", "luxembourg", "malta", "moldova", "monaco",
        "montenegro", "netherlands", "north macedonia", "norway", "poland",
        "portugal", "romania", "san marino", "serbia", "slovakia", "slovenia",
        "spain", "sweden", "switzerland", "ukraine", "united kingdom", "vatican city",
    },
    "asia": {
        "afghanistan", "armenia", "azerbaijan", "bahrain", "bangladesh", "bhutan",
        "brunei", "cambodia", "china", "georgia", "india", "indonesia", "iran",
        "iraq", "israel", "japan", "jordan", "kazakhstan", "kuwait", "kyrgyzstan",
        "laos", "lebanon", "malaysia", "maldives", "mongolia", "myanmar", "nepal",
        "north korea", "oman", "pakistan", "palestine", "philippines", "qatar",
        "saudi arabia", "singapore", "south korea", "sri lanka", "syria", "taiwan",
        "tajikistan", "thailand", "timor leste", "turkey", "turkmenistan",
        "united arab emirates", "uzbekistan", "vietnam", "yemen",
    },
    "africa": {
        "algeria", "angola", "benin", "botswana", "burkina faso", "burundi",
        "cabo verde", "cameroon", "central african republic", "chad", "comoros",
        "democratic republic of the congo", "djibouti", "egypt", "equatorial guinea",
        "eritrea", "eswatini", "ethiopia", "gabon", "gambia", "ghana", "guinea",
        "guinea bissau", "ivory coast", "kenya", "lesotho", "liberia", "libya",
        "madagascar", "malawi", "mali", "mauritania", "mauritius", "morocco",
        "mozambique", "namibia", "niger", "nigeria", "republic of the congo",
        "rwanda", "senegal", "seychelles", "sierra leone", "somalia", "south africa",
        "south sudan", "sudan", "tanzania", "togo", "tunisia", "uganda", "zambia",
        "zimbabwe",
    },
    "oceania": {
        "australia", "fiji", "kiribati", "marshall islands", "micronesia", "nauru",
        "new zealand", "palau", "papua new guinea", "samoa", "solomon islands",
        "tonga", "tuvalu", "vanuatu",
    },
}

REGION_ALIASES = {
    "n america": "north america",
    "north american": "north america",
    "s america": "south america",
    "south american": "south america",
    "european": "europe",
    "asian": "asia",
    "african": "africa",
    "australasia": "oceania",
}


def country_matches_region(country: str, preferred_region: str) -> bool | None:
    """Return True/False for known regions, or None when the constraint is unknown."""
    country_key = _normalize(country)
    region_key = REGION_ALIASES.get(_normalize(preferred_region), _normalize(preferred_region))
    if not country_key or not region_key:
        return None
    allowed = REGION_COUNTRIES.get(region_key)
    if allowed is not None:
        return country_key in allowed
    if country_key == region_key:
        return True
    return None


# Countries lying mostly south of the equator. Used only to flip seasons for
# destinations whose catalog row has no latitude.
SOUTHERN_HEMISPHERE_COUNTRIES = {
    "argentina", "australia", "bolivia", "botswana", "brazil", "chile", "eswatini",
    "fiji", "lesotho", "madagascar", "malawi", "mauritius", "mozambique", "namibia",
    "new zealand", "paraguay", "peru", "samoa", "seychelles", "south africa",
    "tanzania", "tonga", "uruguay", "vanuatu", "zambia", "zimbabwe",
}

EARTH_RADIUS_KM = 6371.0


def region_for_country(country: str) -> str | None:
    """Return the world region key (e.g. "europe") for a known country."""
    country_key = _normalize(country)
    for region, countries in REGION_COUNTRIES.items():
        if country_key in countries:
            return region
    return None


def great_circle_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Straight-line (haversine) distance. A lower bound on any real route."""
    from math import asin, cos, radians, sin, sqrt

    phi1, phi2 = radians(lat1), radians(lat2)
    delta_phi = radians(lat2 - lat1)
    delta_lambda = radians(lng2 - lng1)
    a = sin(delta_phi / 2) ** 2 + cos(phi1) * cos(phi2) * sin(delta_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * asin(sqrt(a))


def is_southern_hemisphere(country: str, latitude: float | None = None) -> bool:
    if latitude is not None:
        return latitude < 0
    return _normalize(country) in SOUTHERN_HEMISPHERE_COUNTRIES


_COUNTRY_ALIASES = {
    "usa": "united states",
    "us": "united states",
    "united states of america": "united states",
    "uk": "united kingdom",
    "czech republic": "czechia",
}


def same_country(first: str | None, second: str | None) -> bool:
    """Compare country names from different sources (catalog vs. map lookup)."""
    first_key = _normalize(first or "")
    second_key = _normalize(second or "")
    if not first_key or not second_key:
        return False
    return _COUNTRY_ALIASES.get(first_key, first_key) == _COUNTRY_ALIASES.get(second_key, second_key)
