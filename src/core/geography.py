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
