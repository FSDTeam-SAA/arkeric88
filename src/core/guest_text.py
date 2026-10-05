"""
Guest-facing wording rules shared by the itinerary and destination steps.

Nothing shown to a guest may contain backend values (PRICE_LEVEL_MODERATE,
"approximately" markers, source-review statuses, pipe-separated catalog
tags) or generic filler ("a convenient stop near the route"). Internal data
stays in the response's internal fields; these helpers produce the words a
guest reads.
"""

import re
from typing import Iterable, List, Optional

# Google Places price levels -> a label a guest understands without technical
# knowledge. PRICE_LEVEL_LUXURY is an internal estimate label used for stays.
PRICE_LEVEL_LABELS = {
    "PRICE_LEVEL_FREE": "Free",
    "PRICE_LEVEL_INEXPENSIVE": "$ · Budget-friendly",
    "PRICE_LEVEL_MODERATE": "$$ · Moderate",
    "PRICE_LEVEL_EXPENSIVE": "$$$ · Upscale",
    "PRICE_LEVEL_VERY_EXPENSIVE": "$$$$ · Luxury",
    "PRICE_LEVEL_LUXURY": "$$$$ · Luxury",
}

_PRICE_LEVEL_TOKEN = re.compile(r"PRICE_LEVEL_[A-Z_]+")
_APPROXIMATELY = re.compile(r"\s*\((?:approximately|approx\.?)\)|\bapproximately\b\s*", re.IGNORECASE)

# Phrases that say nothing about why an item was chosen for this guest.
_GENERIC_REASON = re.compile(
    r"\bconvenient\b|\bnear the (?:day'?s )?(?:planned )?route\b|\bnice (?:place|spot|stop)\b"
    r"|\bgreat (?:place|spot|stop)\b|\bpopular (?:place|spot|stop)\b|\bmust[- ]see\b"
    r"|\ba (?:spot|place) of your choice\b",
    re.IGNORECASE,
)
MIN_REASON_WORDS = 8

# Words that only make sense inside our catalog or validation pipeline.
_INTERNAL_WORDING = re.compile(
    r"\bour editors? tag\b|\beditorial hypothesis\b|\bsource review\b|\bcatalog\b|\bcandidate_only\b"
    r"|\bnot_checked\b|\bunverified\b|\|",
    re.IGNORECASE,
)


def price_indication(level: Optional[str]) -> Optional[str]:
    """Guest label for a price level, or None when there is nothing meaningful to show."""
    match = _PRICE_LEVEL_TOKEN.search(str(level or ""))
    return PRICE_LEVEL_LABELS.get(match.group(0)) if match else None


def clean(text: Optional[str]) -> str:
    """Remove backend markers from a string that will be shown to a guest."""
    if not text:
        return ""
    text = _APPROXIMATELY.sub(" ", str(text))
    text = _PRICE_LEVEL_TOKEN.sub(lambda match: PRICE_LEVEL_LABELS.get(match.group(0), ""), text)
    text = re.sub(r"\s+([.,;:])", r"\1", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def has_internal_wording(text: Optional[str]) -> bool:
    return bool(text) and bool(_INTERNAL_WORDING.search(text) or _PRICE_LEVEL_TOKEN.search(text))


def is_generic_reason(text: Optional[str]) -> bool:
    """True when a recommendation reason is missing, too short or filler."""
    if not text or not str(text).strip():
        return True
    text = str(text).strip()
    return len(text.split()) < MIN_REASON_WORDS or bool(_GENERIC_REASON.search(text)) or has_internal_wording(text)


def natural_list(items: Iterable[str], conjunction: str = "and") -> str:
    """["food", "art", "culture"] -> "food, art and culture"."""
    items = [str(item).strip() for item in items if str(item).strip()]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return f"{', '.join(items[:-1])} {conjunction} {items[-1]}"


def from_pipes(value: Optional[str]) -> List[str]:
    """Catalog "Food|Art|Culture" -> ["food", "art", "culture"] for use inside a sentence."""
    return [part.strip().lower() for part in str(value or "").split("|") if part.strip()]


def lower_first(text: str) -> str:
    text = (text or "").strip()
    return text[:1].lower() + text[1:] if text else text


def minutes_phrase(minutes: Optional[int]) -> str:
    """15 -> "about 15 minutes", 80 -> "about 1 hr 20 min"."""
    if minutes is None:
        return "an unconfirmed travel time"
    minutes = max(1, int(round(minutes)))
    if minutes < 60:
        return f"about {minutes} minute{'s' if minutes != 1 else ''}"
    hours, rest = divmod(minutes, 60)
    return f"about {hours} hr {rest} min" if rest else f"about {hours} hr"


def dedupe(notes: Iterable[str]) -> List[str]:
    """Each note once, in first-seen order, ignoring case and trailing punctuation."""
    seen, result = set(), []
    for note in notes:
        note = clean(note)
        key = note.lower().rstrip(".!")
        if note and key not in seen:
            seen.add(key)
            result.append(note)
    return result
