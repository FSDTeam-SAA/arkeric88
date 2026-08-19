# Retreat Recommendation API v2 — What Changed

This document describes the new versioned retreat-matching system implemented from
`AI_DEVELOPER_CHANGES.md`, `BACKEND_DEVELOPER_CHANGES.md`, and
`FRONTEND_DEVELOPER_CHANGES.md`, the new 15-question questionnaire, and sample
requests/responses for the new API.

## 1. Summary of the change

**Before:** the AI model was asked to recommend *cities* directly from a 12-question
profile. Nothing in that flow guaranteed the model actually checked budget, season,
spirituality, or physical intensity against real property data — it could describe
scoring criteria in its prompt without any code enforcing them.

**Now:** a new endpoint, `POST /v2/retreat-recommendations`, ranks the 150 properties
in the Retreat Master Database directly:

1. The 15-question answers are converted into a normalized **matching profile**
   using deterministic, version-controlled lookup tables (no LLM involved).
2. **Hard filters** run first and unconditionally exclude incompatible properties
   (budget, spirituality ceiling, season, clearly incompatible physical intensity).
3. Surviving properties are **scored** with ten named, documented weights that sum
   to 100 — every score is returned as an auditable breakdown.
4. Only *after* ranking is complete does an LLM see anything — and only the already
   selected properties' stored facts and score breakdown, to write 2-3 short
   `match_reasons` and any `warnings`. It cannot change the shortlist, the order, or
   any structured fact (price, season, facilities, restriction compatibility). If the
   model call fails or returns something that doesn't validate, a deterministic
   template fallback is used instead, so the endpoint never depends on the LLM being
   available.

The old endpoints (`/get_suggested_city`, `/regenerate_suggested_city`,
`/get_tour_plan`, `/regenerate_tour_plan`) are **unchanged** and keep working exactly
as before for any existing client. `/get_tour_plan` additionally accepts the new
`property_id` field (see §6).

## 2. New files

| File | Purpose |
|---|---|
| `app/schemas/retreat_v2_schema.py` | Request/response models for the 15-question questionnaire and ranked-property response. All enums, no display labels. |
| `src/core/answer_mappings.py` | Version-controlled, testable mapping tables from every questionnaire enum code to a Retreat Master Database dimension. |
| `src/core/matching_profile.py` | Converts a validated request into a `MatchingProfile` (pure function, no I/O, no LLM). |
| `src/core/restriction_extractor.py` | Deterministic keyword extraction of restriction codes/accessibility needs from free text. Explicitly not a medical assessment. |
| `src/core/retreat_scoring.py` | Hard filters + the ten-component weighted scoring function for one property. |
| `src/core/retreat_ranker.py` | Runs filters + scoring across the whole catalog and returns a stably-ordered shortlist. |
| `src/core/retreat_explanation.py` | The one place an LLM is used — bounded fact-sheet prompt + strict output validation + deterministic fallback. |
| `src/session/retreat_session_store.py` | In-memory session store recording `schema_version` / `scoring_version` / `answer_mapping_version` / `database_version` for audit. |
| `app/router/retreat_recommendation_route.py` | The `/v2/retreat-recommendations` endpoints. |
| `tests/test_retreat_matching_v2.py` | Enum-mapping coverage, hard-filter tests, ranking-stability test, restriction-warning tests, request-validation tests. |

`src/core/retreat_catalog.py` gained: a stable `property_id` (`retreat_###`, derived
from the workbook's row number), `parse_avg_night()`, `parse_best_season()`, and
`get_database_version()`.

## 3. The new 15-question pattern

The questionnaire replaces the old 12-question flow **only for this new endpoint** —
`/get_suggested_city` still uses the old `QuestionAnswers` schema untouched. Every
question sends a stable code, never the label shown on screen.

| # | Question | Field | Type | Codes |
|---|---|---|---|---|
| 1 | Which wellness traveler feels most like you right now? | `archetype` | single | `burned_out_achiever`, `transformer`, `seeker`, `optimizer`, `escapist`, `reconnector` |
| 2 | What do you most want a break from? | `escape_from` | single | `noise_stimulation`, `responsibility_decisions`, `routine_repetition`, `emotional_heaviness` |
| 3 | What would make you exhale the moment you arrive? | `arrival_priority` | single | `breathtaking_view`, `silence_privacy`, `warmth_water_sunshine`, `beautiful_design_service` |
| 4 | How much structure do you want, day to day? | `structure_preference` | single | `almost_none`, `optional_rituals`, `one_daily_anchor`, `full_program` |
| 5 | What kind of reset sounds most appealing? | `reset_style` | single | `digital_disconnection`, `sensory_indulgence`, `creative_inspiration`, `doing_nothing` |
| 6 | How intense do you want the physical side of this trip to be? | `physical_intensity` | single | `gentle`, `moderate`, `challenging` |
| 7 | Who will share this journey with you? | `party.type` (+ `party.adults`/`party.children`) | single | `solo`, `couple`, `small_group`, `family` |
| 8 | How open are you to spiritual or ceremonial elements? | `spirituality` | single | `none`, `light`, `moderate`, `deep` |
| 9 | When are you hoping to travel? | `travel_window` | single, 2-step | `mode: flexible` **or** `mode: specific` + `season: spring\|summer\|autumn\|winter\|choose_month` (+ `months` when `choose_month`) |
| 10 | How would you like your trip organized? | `planning_service_level` | single | `loose`, `well_planned`, `hour_by_hour` |
| 11 | Any activities to avoid, or things you can't do? | `restrictions.text` (+ optional `restrictions.codes` chips) | open text | free text, 1000-char max |
| 12 | What environment speaks to your soul? | `settings` | **multi** | `mountains`, `ocean_beach`, `jungle_rainforest`, `desert`, `countryside_farmland`, `lake`, `city_urban` (empty array = no preference) |
| 13 | What's your trip budget per night? | `budget.per_person_per_night_max` (+ `budget.open_ended`) | slider | $100–$7,000+ (`open_ended: true` at the top of the slider) |
| 14 | How long is your trip? | `duration.bucket` (+ optional `duration.exact_nights`) | single | `1_3_nights`, `4_7_nights`, `1_2_weeks`, `2_plus_weeks` |
| 15 | What do you hope this trip gives you? | `transform_focus` | **multi, max 3** | `Burnout Recovery`, `Longevity`, `Detox`, `Weight Loss`, `Spiritual Growth`, `Emotional Healing`, `Nervous System Reset`, `Fitness`, `Creativity`, `Relationship Repair`, `Community`, `Sleep`, `Digital Detox`, `Cultural Immersion` |

Notes matching your Bengali summary:

- Q4's two middle answers (`optional_rituals`, `one_daily_anchor`) both map to the
  database value `Semi-Guided`; the raw answer is preserved on the profile
  separately for later itinerary nuance.
- Q9 is exactly the two-step pattern you described: pick `flexible` **or**
  `specific` first; only if `specific` do you then pick one season/month.
- **Setting, Spirituality, Structure, and Physical Intensity data already exist**
  in the Retreat Master Database and are fully wired into filtering/scoring today.
  **Setting (Q12) is the one exception**: the workbook has no `Setting` column yet,
  so it's accepted and validated, but cannot be filtered or scored — every response
  says so explicitly in `data_gaps` and per-property `warnings`, as your note
  anticipated. The same is true for restriction/activity tags (Q11) and
  family/small-group fit (Q7's `family`/`small_group`).

## 4. Request — `POST /v2/retreat-recommendations`

```json
{
  "archetype": "burned_out_achiever",
  "escape_from": "noise_stimulation",
  "arrival_priority": "silence_privacy",
  "structure_preference": "optional_rituals",
  "reset_style": "digital_disconnection",
  "physical_intensity": "gentle",
  "party": { "type": "solo", "adults": 1, "children": 0 },
  "spirituality": "none",
  "travel_window": { "mode": "flexible" },
  "planning_service_level": "well_planned",
  "restrictions": { "text": "No hiking or water activities", "codes": [] },
  "settings": ["ocean_beach"],
  "budget": { "currency": "USD", "per_person_per_night_max": 500, "open_ended": false },
  "duration": { "bucket": "4_7_nights" },
  "transform_focus": ["Burnout Recovery", "Sleep", "Digital Detox"]
}
```

Validation enforced by the schema (all return HTTP `422` with a field-level error
when violated):

- Every field above must be a known enum code — no free labels.
- `transform_focus`: 1–3 unique values from the canonical list.
- `travel_window`: `flexible` must not include `season`/`months`; `specific` requires
  a `season`, and only `season: "choose_month"` accepts explicit `months` (1–12).
- `party`: counts must match the type (`solo` → 1 adult/0 children, `couple` → 2/0,
  `family` → ≥1 adult, `small_group` → ≥3 adults).
- `budget.per_person_per_night_max` must be > 0.
- `restrictions.text` capped at 1000 characters.

## 5. Response

```json
{
  "recommendation_session_id": "b94f1b78-43d4-418d-8d00-4eee74a0992a",
  "schema_version": "2026-08-15.v2",
  "scoring_version": "2026-08-15.1",
  "answer_mapping_version": "2026-08-15.1",
  "database_version": "retreat-master-150rows-1784007794",
  "recommendations": [
    {
      "property_id": "retreat_118",
      "property_name": "Revivo Wellness Resort",
      "country": "Indonesia",
      "region": "Nusa Dua Bali",
      "settings": [],
      "match_score": 76,
      "score_breakdown": {
        "archetype": 20.0,
        "transform_focus": 6.0,
        "emotional_tone": 2.0,
        "structure": 10.0,
        "physical_intensity": 8.0,
        "party_social": 8.0,
        "emotional_safety": 6.4,
        "nature": 6.4,
        "luxury": 4.0,
        "spirituality": 5.0
      },
      "match_reasons": [
        "Revivo Wellness Resort scores strongly on your traveler archetype.",
        "Revivo Wellness Resort scores strongly on your preferred amount of daily structure."
      ],
      "warnings": [
        "Setting preference could not be verified: the workbook has no Setting column yet, so this property was not filtered or scored on environment.",
        "Activity restrictions could not be verified against structured property data yet; treat restriction_status as advisory only."
      ],
      "restriction_status": "unverified",
      "avg_night": 200.0,
      "avg_night_is_lower_bound": true,
      "avg_night_raw": "$200+",
      "budget_tier": "Premium",
      "program_cost": "$1,400+ / week",
      "best_season": [4, 5, 6, 7, 8, 9, 10],
      "best_season_raw": "Apr–Oct"
    }
  ],
  "excluded_count": 115,
  "total_candidate_count": 150,
  "extracted_restrictions": {
    "codes": ["no_hiking", "no_water_activities"],
    "accessibility_needs": [],
    "unresolved_text": []
  },
  "data_gaps": [
    "Setting is not yet a database field (see BACKEND_DEVELOPER_CHANGES.md 'Database Changes'); the requested setting(s) were recorded but could not be used to filter or score properties.",
    "Activity/Restriction Tags are not yet a database field; restrictions were extracted from free text but could not be verified against any property."
  ]
}
```

The `match_reasons` shown above are the **deterministic fallback** text (used when
the LLM is unavailable), so this exact sample is 100% reproducible. When the LLM
step succeeds, `match_reasons`/`warnings` read more naturally, e.g.:

> "You match strongly with its 'Medical Wellness' focus and semi-guided structure...
> The calm/clinical emotional tone and gentle physical intensity align with a
> burnout-recovery style..."

— but the `property_id`, `match_score`, `score_breakdown`, `avg_night`, and every
other structured field are **identical either way**, because the LLM never touches
them.

### Error example — unknown enum (`422`)

```json
{
  "detail": [
    {
      "type": "enum",
      "loc": ["body", "archetype"],
      "msg": "Input should be 'burned_out_achiever', 'transformer', 'seeker', 'optimizer', 'escapist' or 'reconnector'",
      "input": "not_real",
      "ctx": { "expected": "'burned_out_achiever', 'transformer', 'seeker', 'optimizer', 'escapist' or 'reconnector'" }
    }
  ]
}
```

### `GET /v2/retreat-recommendations/{recommendation_session_id}`

Returns the exact same response body again (from the in-memory session store), or
`404 {"detail": "Recommendation session not found."}`.

## 6. Itinerary generation now accepts `property_id`

`POST /get_tour_plan` gained an optional `property_id` field:

```json
{ "session_id": "...", "selected_city": "unused-when-property_id-is-set", "property_id": "retreat_118" }
```

When `property_id` is present it takes priority over `selected_city`: the exact
matched catalog record is resolved and used to build the stay (skipping the old
fuzzy city/name search), guaranteeing the itinerary is built around the *specific*
property the user picked from the v2 shortlist, not a same-named city elsewhere in
the world. `selected_city` remains required in the request body for backward
compatibility with existing clients that don't yet send `property_id`.

## 7. Scoring weights (sum to 100)

| Component | Weight | What it measures |
|---|---|---|
| `archetype` | 20 | Exact match to Q1 archetype (20) or an inferred secondary archetype from Q2 (12); else 0. |
| `transform_focus` | 18 | Overlap ratio between requested (≤3) and the property's Transform Focus list. |
| `emotional_tone` | 8 | Fuzzy match of desired tone words (derived from Q2/Q3/Q5) against the property's free-text Emotional Tone. |
| `structure` | 10 | Ordinal closeness between requested Structure (Q4) and the property's Structure. |
| `physical_intensity` | 8 | Ordinal closeness between requested (Q6) and the property's Physical Intensity. |
| `party_social` | 10 | Property's Solo/Couple/Social score (Q7); Family/Small Group currently reuse Social (flagged unverified). |
| `emotional_safety` | 8 | Property's Emotional Safety score, 0–10 scaled. |
| `nature` | 8 | Closeness to a nature target derived from Q3/Q12 signals. |
| `luxury` | 5 | Closeness to a luxury target derived from Q3/Q5. |
| `spirituality` | 5 | Ordinal closeness between requested (Q8) and the property's Spirituality. |

`match_score` is the rounded sum, 0–100.

## 8. Hard filters (run before scoring)

| Filter | Rule |
|---|---|
| Budget | Excluded if `Avg Night` amount > `budget.per_person_per_night_max`, **unless** `open_ended: true` (a floor, not a ceiling) or the price is `Donation` (not comparable, never excluded). |
| Spirituality | Q8 is a **maximum**: a property whose Spirituality ordinal exceeds the requested one is excluded (`none` excludes `Light`/`Moderate`/`Deep`). |
| Season | Only applied when `travel_window.mode = "specific"`; excluded if the requested months don't intersect the property's parsed Best Season months. `Year-round` and `Monthly` both resolve to all 12 months. |
| Physical intensity | Only the clearly incompatible pair is hard-filtered: `gentle` requested excludes `Challenging` properties. Other combinations are handled by scoring only. |
| Setting / restrictions | **Not** hard-filtered — the workbook has no Setting or Activity/Restriction Tags columns yet, so filtering on them would silently exclude properties based on data that doesn't exist. They're surfaced as `warnings`/`data_gaps` instead. |

## 9. Known limitations (carried over from the handoff docs, not silently hidden)

- **Setting** (Q12) cannot be filtered/scored until the database gains a `Setting`
  column — tracked in `BACKEND_DEVELOPER_CHANGES.md` "Database Changes."
- **Restriction/exclusion verification** (Q11) is always `"unverified"` today
  (or `"not_applicable"` when nothing was restricted) — the extractor only produces
  codes/accessibility flags/unresolved text; it never claims a property is safe.
- **Family / Small Group fit** (Q7) is estimated from the property's overall Social
  score, flagged with a warning, until Family Fit / Small Group Fit columns exist.
- **Session persistence** is in-memory, matching the existing legacy session stores.
  It is not durable across restarts and will not stay consistent with multiple API
  worker processes — swap `RetreatSessionStore` for a real datastore before scaling
  beyond one worker.
- `duration` is currently informational only (not a hard filter), because the
  workbook has no reliable Minimum/Maximum Nights columns yet.

## 10. Test coverage added

`tests/test_retreat_matching_v2.py` (27 new tests, all passing alongside the
existing 11 — 38 total):

- Every questionnaire enum maps to a real value present in the catalog.
- Q4's two middle answers both resolve to `Semi-Guided` while preserving the raw answer.
- `spirituality: "none"` excludes every `Light`/`Moderate`/`Deep` property.
- A non-open-ended budget excludes every property above the max; `open_ended: true` does not.
- `physical_intensity: "gentle"` excludes every `Challenging` property.
- Specific-month season filtering only returns properties whose Best Season overlaps.
- Hard filters run before scoring (a filtered-out property never reaches `score_property`).
- Ranking is stable across repeated calls with the same profile.
- Score breakdown components sum to the total score.
- Restriction extraction preserves original text, extracts known codes, and flags unresolved clauses.
- Request validation: `transform_focus` max 3 / must be canonical values, `party` counts must match type, `flexible` timing rejects months.
- API-level: stable `property_id`s, full score-breakdown key set, session retrieval by ID, 404 on unknown session, 422 on unknown enum, and the legacy `/get_suggested_city` schema is unaffected.
