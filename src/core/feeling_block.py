"""
"The feeling behind your journey" block shown near the top of every itinerary.

Rules (client brief for sample itineraries):
- Show the traveler's SELECTED desired feeling(s) in bold. The headline is
  built here from their trip_goals, never inferred from destination tags.
- Two or three natural sentences on how this destination and at least two
  experiences actually in the itinerary support that feeling, using their
  interests, pace, setting and companions where relevant.
- Describe an opportunity or intention; never promise how they will feel.
- If the itinerary cannot support the feeling, the caller revises the plan
  or flags the mismatch before it is displayed.

The model only writes the sentences and says which experiences they rely on.
Everything it returns is validated here: sentence count, promise language,
and that every referenced experience exists in the final itinerary. If the
model is unavailable or keeps failing validation, the block keeps the
headline and a deterministic intention sentence and is marked
"not_assessed" -- it never claims an alignment nobody checked.
"""

import re
from datetime import datetime, timezone
from typing import Callable, List, Optional

from src.core.data_processor import ProcessData
from src.core.destination_places import lookup_name
from src.core.intake_mappings import (
    TRIP_GOAL_DEFINITIONS,
    TRIP_GOAL_FEELING_WORDS,
    TRIP_PACE_LABELS,
    TRIP_PROMPT_LABELS,
)
from src.core.trip_profile import TripProfile

FEELING_BLOCK_TITLE = "The feeling behind your journey"
FEELING_BLOCK_NOTE = "Describes an intention for the trip, not a promise of how you will feel."
MIN_SUPPORTING_EXPERIENCES = 2
# The client's sample descriptors run 30-45 words. The model is asked for
# per-part targets; the MAX_* limits (with some slack) fail validation.
TARGET_INTENTION_WORDS = 15
TARGET_NARRATIVE_WORDS = 45
MAX_INTENTION_WORDS = 22
MAX_NARRATIVE_WORDS = 55

DISPLAY_GUIDANCE = {
    "aligned": "Show the block at the top of the itinerary.",
    "revised": "Show the block at the top of the itinerary.",
    "mismatch": (
        "Do not present this itinerary as supporting the chosen feeling. Show the mismatch detail and "
        "offer to regenerate the plan or revisit the chosen feeling."
    ),
    "not_assessed": (
        "Show only the headline and intention; the link to specific experiences was not written or checked."
    ),
}
MAX_ATTEMPTS = 3

# Promise language the brief rules out ("never promise that the traveler will
# feel a particular way").
_PROMISE_PATTERNS = re.compile(
    r"\byou(?:'ll|’ll| will)\s+(?:feel|be|find yourself|leave|return|come home)\b"
    r"|\bwill\s+(?:make|leave|help)\s+you\s+(?:feel|be)\b"
    r"|\bguarantee\w*|\bpromis\w*|\bensures?\b"
    r"|\bwill\s+(?:restore|heal|transform|change|relax|recharge)\s+you\b"
    # Outcome clauses: "so your week feels centered", "so you feel calm",
    # "leaving you feeling rested".
    r"|\bso\s+(?:that\s+)?(?:you|your\s+\w+)\s+(?:will\s+)?feels?\b"
    r"|\bleav\w*\s+you\s+feeling\b",
    re.IGNORECASE,
)

# Client sample descriptors, given to the model as style references only.
_STYLE_EXAMPLES = """
Kyoto -- THE FEELING: REFLECTIVE
You want time to slow down and hear yourself think. Kyoto's gardens, quiet walks, and thoughtful rituals give you space to take in a new place while reconnecting with your own thoughts.

Mexico City -- THE FEELING: INSPIRED
You're looking for fresh ideas and a change of perspective. Mexico City's art, food, and creative streets invite you to follow your curiosity and come home with something new to think about.
""".strip()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sentences(text: str) -> List[str]:
    return [part for part in re.split(r"(?<=[.!?])\s+", (text or "").strip()) if part]


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def feeling_words(profile: TripProfile) -> List[str]:
    return [TRIP_GOAL_FEELING_WORDS[goal] for goal in profile.goals]


def headline(profile: TripProfile) -> str:
    return "THE FEELING: " + " & ".join(word.upper() for word in feeling_words(profile))


def fallback_intention(profile: TripProfile) -> str:
    """One sentence built only from the guest's own selections."""
    wants = [TRIP_GOAL_DEFINITIONS[goal] for goal in profile.goals]
    wants = [want[0].lower() + want[1:] for want in wants]
    return "You want " + ", and ".join(wants) + "."


def _markdown(profile: TripProfile, intention: str, narrative: Optional[str]) -> str:
    body = " ".join(part for part in (intention, narrative) if part)
    return f"**{headline(profile)}**\n{body}"


def _build_prompt(
    profile: TripProfile,
    destination_name: str,
    country: str,
    experiences: List[dict],
    rejection: Optional[str] = None,
) -> str:
    chosen = "; ".join(
        f"{TRIP_GOAL_FEELING_WORDS[goal]} -- {TRIP_GOAL_DEFINITIONS[goal]}" for goal in profile.goals
    )
    listed = "\n".join(
        f"- E{number} (day {item['day']}): {item['activity_name']} -- {item['activity_description']}"
        for number, item in enumerate(experiences, start=1)
    )
    retry = f"\nYOUR PREVIOUS ANSWER WAS REJECTED: {rejection}. Fix that.\n" if rejection else ""
    return f"""
You write the "{FEELING_BLOCK_TITLE}" block for an itinerary that is already built.

THE FEELING THE TRAVELER CHOSE (use exactly this; never substitute a feeling suggested by the destination):
{chosen}

TRAVELER ANSWERS:
- Moments they enjoy: {', '.join(profile.moment_labels)}
- Preferred settings: {', '.join(profile.environment_labels)}
- Pace: {TRIP_PACE_LABELS[profile.pace]}
- Travelling as: {profile.party_phrase()}
- What prompted the trip: {TRIP_PROMPT_LABELS[profile.trip_prompt]}

DESTINATION: {destination_name}, {country}

EXPERIENCES IN THE ITINERARY (refer to them by ID):
{listed or '- (none)'}
{retry}
WRITE:
1. "intention": ONE short sentence (at most {TARGET_INTENTION_WORDS} words) reflecting what the traveler told us they want, starting with "You want" or "You're looking for". Plain, warm words.
2. "narrative": ONE or TWO sentences (at most {TARGET_NARRATIVE_WORDS} words in total) on how {destination_name} and at least {MIN_SUPPORTING_EXPERIENCES} of the listed experiences give the traveler room for that feeling. Name {destination_name}; describe the experiences naturally in everyday words (e.g. "a morning ridge walk" rather than a business name) instead of listing them. Use their interests, pace, setting and companions where relevant.
   Do not repeat or rephrase the intention, and never write the IDs (E1...) in the text.
3. Describe an opportunity or intention. NEVER promise an outcome: no "you will feel", "you'll feel", "will make you", "guarantee", "ensure".
4. "supporting_experience_ids": the IDs (like "E1") of the experiences your narrative relies on, at least {MIN_SUPPORTING_EXPERIENCES}.
5. If the listed experiences do not genuinely support the chosen feeling, do not stretch: set "supports_feeling" to false and explain what is missing in "mismatch_reason".

STYLE REFERENCE (tone and length; do not copy):
{_STYLE_EXAMPLES}

RESPONSE FORMAT (JSON ONLY, no preamble):
{{"intention": "...", "narrative": "...", "supporting_experience_ids": ["E1", "E2"], "supports_feeling": true, "mismatch_reason": ""}}
"""


def _validate(
    payload: dict, profile: TripProfile, destination_name: str, country: str, experiences: List[dict]
) -> tuple:
    """Return (result, error). result is a dict of validated fields, or None."""
    if not isinstance(payload, dict):
        return None, "the answer was not a JSON object"
    if payload.get("supports_feeling") is False:
        reason = str(payload.get("mismatch_reason") or "").strip()
        return {
            "supports_feeling": False,
            "mismatch_reason": reason or "The planned experiences do not clearly support the chosen feeling.",
        }, None

    intention = str(payload.get("intention") or "").strip()
    narrative = str(payload.get("narrative") or "").strip()
    if len(_sentences(intention)) != 1:
        return None, "intention must be exactly one sentence"
    narrative_count = len(_sentences(narrative))
    if not 1 <= narrative_count <= 2:
        return None, "narrative must be one or two sentences (two or three in total with the intention)"
    if len(intention.split()) > MAX_INTENTION_WORDS:
        return None, f"intention must be at most {TARGET_INTENTION_WORDS} words"
    if len(narrative.split()) > MAX_NARRATIVE_WORDS:
        return None, f"narrative must be at most {TARGET_NARRATIVE_WORDS} words"
    if _PROMISE_PATTERNS.search(f"{intention} {narrative}"):
        return None, "it promised how the traveler will feel; describe an opportunity instead"
    if re.search(r"\bE\d+\b", f"{intention} {narrative}"):
        return None, "experience IDs like E1 must only appear in supporting_experience_ids, never in the text"
    if re.match(r"\s*you(?:'re|’re| are)? (?:want|looking for)\b", narrative, re.IGNORECASE):
        return None, "the narrative must not restate the intention; start it with the destination or the experiences"
    place = _normalize(lookup_name(destination_name).split(",")[0])
    if place not in _normalize(narrative) and _normalize(country) not in _normalize(narrative):
        return None, f"the narrative must name {destination_name}"

    supporting = []
    for reference in payload.get("supporting_experience_ids") or []:
        # Accept "E2", "e2" or a copied line such as "E2 (day 1): ...".
        match = re.match(r"\s*E(\d+)\b", str(reference), re.IGNORECASE)
        index = int(match.group(1)) - 1 if match else -1
        if 0 <= index < len(experiences) and experiences[index] not in supporting:
            supporting.append(experiences[index])
    if len(supporting) < MIN_SUPPORTING_EXPERIENCES:
        return None, (
            f"supporting_experience_ids must list at least {MIN_SUPPORTING_EXPERIENCES} IDs from the experience list"
        )
    return {
        "supports_feeling": True,
        "intention": intention,
        "narrative": narrative,
        "supporting_experiences": [
            {"day": item["day"], "activity_name": item["activity_name"]} for item in supporting
        ],
    }, None


def _block(profile: TripProfile, intention: str, narrative: Optional[str], supporting: list,
           status: str, detail: str) -> dict:
    return {
        "title": FEELING_BLOCK_TITLE,
        "feelings": [
            {"code": goal, "label": TRIP_GOAL_FEELING_WORDS[goal]} for goal in profile.goals
        ],
        "headline": headline(profile),
        "intention": intention,
        "narrative": narrative,
        "markdown": _markdown(profile, intention, narrative),
        "supporting_experiences": supporting,
        "note": FEELING_BLOCK_NOTE,
        "alignment": {
            # aligned | revised | mismatch | not_assessed
            "status": status,
            "detail": detail,
            "checked_at_utc": _now(),
            "display_guidance": DISPLAY_GUIDANCE[status],
        },
    }


def assess_feeling_block(
    profile: TripProfile,
    destination_name: str,
    country: str,
    experiences: List[dict],
    ai_response_fn: Callable[[str], str],
) -> dict:
    """
    Build the block for a finished itinerary. `experiences` are its non-meal
    activities as {"day", "activity_name", "activity_description"}.
    Returns the block; alignment.status is "aligned", "mismatch" or
    "not_assessed" (the caller marks a successful revision as "revised").
    """
    rejection: Optional[str] = None
    for _ in range(MAX_ATTEMPTS):
        try:
            raw = ai_response_fn(_build_prompt(profile, destination_name, country, experiences, rejection))
            payload = ProcessData.EnsureDict(raw)
        except Exception as error:  # Model or parsing failure -- try again, then fall back.
            rejection = f"the answer could not be read as JSON ({type(error).__name__})"
            continue
        result, rejection = _validate(payload, profile, destination_name, country, experiences)
        if result is None:
            continue
        if not result["supports_feeling"]:
            return _block(
                profile, fallback_intention(profile), None, [], "mismatch", result["mismatch_reason"]
            )
        return _block(
            profile,
            result["intention"],
            result["narrative"],
            result["supporting_experiences"],
            "aligned",
            "The narrative references experiences that are in this itinerary.",
        )

    return _block(
        profile,
        fallback_intention(profile),
        None,
        [],
        "not_assessed",
        f"The link between the itinerary and the chosen feeling could not be written and checked ({rejection}).",
    )


def revision_note(block: dict) -> str:
    """Instruction for re-planning when the itinerary does not support the feeling."""
    words = " & ".join(item["label"] for item in block["feelings"])
    return (
        f"The previous draft did not support the traveler's chosen feeling ({words}): "
        f"{block['alignment']['detail']} Include at least {MIN_SUPPORTING_EXPERIENCES} experiences that clearly "
        "give room for that feeling, while keeping every restriction, the pace and the party in mind."
    )
