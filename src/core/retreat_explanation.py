"""
Generate human-facing match_reasons/warnings text for an already-ranked
shortlist. This is the ONLY place an LLM is used in the v2 matching flow, and
per AI_DEVELOPER_CHANGES.md #5 its responsibility is deliberately narrow: it
receives nothing but the selected properties' stored facts and the deterministic
score breakdown, and may only produce short explanatory strings. It cannot
change which properties were selected, their order, or any structured field
(price, season, facilities, settings, restriction compatibility) -- those are
computed exclusively by retreat_ranker.py / retreat_scoring.py before this
module ever runs.

If the model call fails or returns something that doesn't validate, a fully
deterministic template fallback is used instead so the endpoint never depends
on LLM availability to return a usable response.
"""

import json
from typing import Dict, List

from src.core.data_processor import ProcessData
from src.core.retreat_ranker import RankedCandidate

_FACT_FIELDS = (
    "Property Name", "Country", "Region", "Core Philosophy", "Primary Category",
    "Emotional Tone", "Structure", "Spirituality", "Physical Intensity",
    "Transform Focus", "Archetypes", "Nature", "Luxury", "Social", "Emotional Safety",
    "Budget Tier", "Avg Night", "Program Cost", "Best Season", "All-Inc?",
)

_BREAKDOWN_LABELS = {
    "archetype": "your traveler archetype",
    "transform_focus": "your transform-focus goals",
    "emotional_tone": "the emotional tone you're seeking",
    "structure": "your preferred amount of daily structure",
    "physical_intensity": "your preferred physical intensity",
    "party_social": "your travel-party type",
    "emotional_safety": "emotional safety",
    "nature": "your nature preference",
    "luxury": "your luxury preference",
    "spirituality": "your spirituality preference",
}


def _fact_sheet(candidate: RankedCandidate) -> dict:
    facts = {field: candidate.record.get(field, "") for field in _FACT_FIELDS}
    return {
        "property_id": candidate.property_id,
        "facts": facts,
        "score_breakdown": candidate.breakdown,
        "match_score": candidate.total_score,
    }


def _build_prompt(candidates: List[RankedCandidate]) -> str:
    fact_sheets = [_fact_sheet(candidate) for candidate in candidates]
    return f"""
You write short, honest explanations for an already-decided wellness retreat
shortlist. You did NOT select or rank these properties -- a deterministic
scoring system already did that. Your only job is to explain, in plain
language, why each property scored as it did and to flag any real trade-offs.

STRICT RULES:
1. Use ONLY the facts and score_breakdown given below for each property_id.
   Never invent or restate a price, season, facility, setting, or restriction
   compatibility that is not explicitly present in "facts".
2. Do not introduce any property_id that is not in the input.
3. match_reasons: 2-3 short sentences per property, grounded in the highest
   score_breakdown components and the matching facts.
4. warnings: 0-3 short sentences flagging honest trade-offs (e.g. a lower
   score_breakdown component, or a facts value that only loosely fits). Use an
   empty list if there is nothing notable.
5. Never claim spiritual, budget, or restriction "safety" beyond what facts show.

PROPERTIES:
{json.dumps(fact_sheets, indent=2)}

RESPONSE FORMAT (JSON ONLY, one entry per property_id above, no preamble):
{{
  "<property_id>": {{"match_reasons": ["...", "..."], "warnings": ["..."]}}
}}
"""


def _deterministic_reasons(candidate: RankedCandidate) -> List[str]:
    top_components = sorted(candidate.breakdown.items(), key=lambda item: item[1], reverse=True)[:2]
    reasons = []
    for key, _value in top_components:
        label = _BREAKDOWN_LABELS.get(key, key)
        reasons.append(f"{candidate.record.get('Property Name', 'This property')} scores strongly on {label}.")
    if not reasons:
        reasons.append(f"{candidate.record.get('Property Name', 'This property')} is the closest overall match available.")
    return reasons


def _validate_llm_payload(raw_text: str, candidates: List[RankedCandidate]) -> Dict[str, dict] | None:
    try:
        payload = ProcessData.EnsureDict(raw_text)
    except ValueError:
        return None
    valid_ids = {candidate.property_id for candidate in candidates}
    result: Dict[str, dict] = {}
    for property_id, entry in payload.items():
        if property_id not in valid_ids or not isinstance(entry, dict):
            continue
        reasons = entry.get("match_reasons", [])
        warnings = entry.get("warnings", [])
        if not isinstance(reasons, list) or not all(isinstance(item, str) for item in reasons):
            continue
        if not isinstance(warnings, list) or not all(isinstance(item, str) for item in warnings):
            continue
        result[property_id] = {
            "match_reasons": reasons[:3],
            "warnings": warnings[:3],
        }
    if len(result) != len(candidates):
        return None
    return result


def generate_match_explanations(
    candidates: List[RankedCandidate],
    ai_response_fn=None,
) -> Dict[str, dict]:
    """
    Returns {property_id: {"match_reasons": [...], "warnings": [...]}} for every
    candidate. `ai_response_fn` defaults to src.service.chat_services.get_ai_response
    (imported lazily so this module has no hard dependency on LLM credentials);
    tests can inject a fake or pass `ai_response_fn=False` to force the
    deterministic fallback without any network/credential dependency.
    """
    if not candidates:
        return {}

    if ai_response_fn is None:
        from src.service.chat_services import get_ai_response as ai_response_fn

    explanations: Dict[str, dict] = {}
    if ai_response_fn:
        try:
            raw_text = ai_response_fn(_build_prompt(candidates))
            validated = _validate_llm_payload(raw_text, candidates)
        except Exception:
            validated = None
        if validated is not None:
            explanations = validated

    for candidate in candidates:
        if candidate.property_id not in explanations:
            explanations[candidate.property_id] = {
                "match_reasons": _deterministic_reasons(candidate),
                "warnings": [],
            }

    return explanations
