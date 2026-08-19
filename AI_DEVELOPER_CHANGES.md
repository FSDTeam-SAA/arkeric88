# AI and Matching Developer Handoff

## Objective

Replace prompt-led destination selection with a structured retreat-matching system. The AI should interpret open text and explain verified matches, but deterministic application code should rank and exclude properties.

## Current Problems

- The model recommends cities before the system selects a retreat.
- The prompt claims to match many workbook dimensions that the current ranking function does not actually score.
- Archetype, spirituality, season, party suitability, Nature, Luxury, Emotional Safety and Social scores are not consistently enforced.
- Restrictions are placed in prompts but are not reliably checked against structured property data.
- Model output is parsed as a dictionary but is not validated against a strict recommendation schema.

## Required Work

### 1. Create the matching profile

Convert the questionnaire into a normalized profile containing:

- Primary archetype and optional secondary archetype
- Desired emotional tones
- Transform Focus values
- Retreat structure
- Physical intensity
- Spirituality preference and maximum acceptable level
- Party type
- Nature, privacy, emotional-safety, luxury and social preferences
- Setting preferences
- Travel months
- Nightly budget and duration
- Structured restrictions

Use the user's Q1 archetype as the primary signal. Other answers may infer a secondary archetype but must not override the explicit selection.

### 2. Implement deterministic ranking

Apply hard filters before scoring:

- Budget incompatibility
- Unacceptable spirituality level
- Incompatible travel season
- Explicitly excluded activities, when property tags support the check
- Setting mismatch, unless the user selected no preference
- Clearly incompatible physical intensity

Score remaining properties using documented weights:

- Archetype fit
- Transform Focus overlap
- Emotional Tone fit
- Structure fit
- Physical Intensity fit
- Solo, Couple or Social suitability
- Emotional Safety
- Nature
- Luxury
- Spirituality preference

Return a score breakdown so results can be audited. Do not use unweighted keyword counting as the primary matcher.

### 3. Define deterministic answer mappings

Examples:

- `breathtaking_view` increases Nature preference.
- `silence_privacy` increases Solo and Emotional Safety preference and decreases desired Social score.
- `beautiful_design_service` increases Luxury preference.
- `digital_disconnection` maps to `Digital Detox` and calm/isolated tones.
- `doing_nothing` favors Gentle, Freeform and calm tones.
- Q4's two middle answers both map to database value `Semi-Guided`; preserve their raw answer for itinerary nuance.

Keep these mappings in version-controlled configuration and cover every frontend enum with tests.

### 4. Handle open-text restrictions

The AI may extract structured restrictions from `restrictions.text`, for example:

```json
{
  "codes": ["no_hiking", "no_water_activities"],
  "accessibility_needs": [],
  "unresolved_text": []
}
```

Requirements:

- Never treat extraction as a medical assessment.
- Preserve the original text.
- Flag anything that cannot be verified from property data.
- Never claim a property is safe solely because the text did not match a keyword.

### 5. Limit the LLM's responsibility

After deterministic ranking, give the model only the selected property facts and score breakdown. Use it to generate:

- Two or three personalized match reasons
- Honest trade-offs or warnings
- A concise explanation of why each property ranked where it did
- Later itinerary content, after a property is selected

The model must not invent prices, seasons, facilities, settings or restriction compatibility.

### 6. Produce structured output

Return and validate fields similar to:

```json
{
  "property_id": "retreat_042",
  "match_score": 87,
  "score_breakdown": {
    "archetype": 18,
    "transform_focus": 20,
    "structure": 10
  },
  "match_reasons": ["..."],
  "warnings": ["..."],
  "restriction_status": "partially_verified"
}
```

## Required Tests

- Every questionnaire answer maps to the intended database dimensions.
- Selecting `none` for spirituality excludes deeply spiritual retreats.
- Exact Transform Focus matching uses canonical values.
- Hard filters execute before weighted scoring.
- Unverified restrictions produce warnings.
- The model cannot add or change structured property facts.
- Ranking is stable when the same profile and database are used.

## Dependencies

- Backend must provide the new questionnaire schema and validated response models.
- Database work must add Setting, restriction/activity tags and family/group suitability.
- Frontend must send stable enum codes rather than visible labels.

## Definition of Done

- The shortlist contains properties, not model-generated cities.
- Every recommendation can be explained from a stored score breakdown.
- Hard constraints cannot be overridden by the LLM.
- Open-text restrictions are preserved, extracted and clearly marked as verified or unverified.
- No unsupported property claims appear in AI-generated explanations.
