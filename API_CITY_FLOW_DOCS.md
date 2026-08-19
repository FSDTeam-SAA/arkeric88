# Step 1 — POST /get_suggested_city (changed)

This document covers exactly what changed in `POST /get_suggested_city` and
`POST /regenerate_suggested_city`: the request body, the response body, and
what to update on the backend side that calls this service. For the full
two-step flow (city suggestions → activities) and a sequence diagram, see
`BACKEND_INTEGRATION_GUIDE.md`.

## 1. What changed and why

**Before:** `/get_suggested_city` took the old 12-question `questions_answers`
payload (birthdate, `trip_length_days`, free-text `todays_feeling`, etc.) plus
`preferred_destinations` and `hope_of_this_trip`, and asked a model to invent
3-5 city names from that profile.

**Now:** `/get_suggested_city` takes the same 15-question payload as
`POST /v2/retreat-recommendations` (see `API_V2_DOCS.md`). Internally it:

1. Runs the exact same deterministic filter/scoring engine used by
   `/v2/retreat-recommendations` — hard filters, then the 10-component
   weighted score — against all 150 retreat properties.
2. Groups the ranked properties by city/region, keeping only the single
   best-scoring property per distinct city.
3. Enriches each city with real photos/coordinates (same `get_cityinfo` tool
   step as before).
4. Returns those cities — each backed by one real, bookable property, with a
   `match_score` — instead of a model-invented city list.

**This is a breaking change to the request body.** The old
`{ "questions_answers": {...}, "preferred_destinations": ..., "hope_of_this_trip": ... }`
shape is no longer accepted and will fail with `422`.

## 2. Request

Identical to `POST /v2/retreat-recommendations` — see `API_V2_DOCS.md` §4 for
the full field-by-field breakdown of all 15 questions. Example:

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
  "transform_focus": ["Burnout Recovery", "Sleep"]
}
```

## 3. Response

Same envelope shape as before (`session_id`, `suggested_cities`, `response`),
so anything reading only `suggested_cities[].city_name`/`country_name` keeps
working unchanged. Every city now additionally carries `property_id`,
`match_score`, and `warnings`:

```json
{
  "session_id": "435730a0-1831-46dd-bf14-515570e20114",
  "suggested_cities": [
    {
      "city_name": "Nusa Dua Bali",
      "country_name": "Indonesia",
      "number_of_days": 5,
      "description": "Revivo Wellness Resort scores strongly on your traveler archetype. Revivo Wellness Resort scores strongly on your preferred amount of daily structure.",
      "city_image": ["https://maps.example/bali-1.jpg"],
      "latitude": -8.79,
      "longitude": 115.23,
      "property_id": "retreat_118",
      "match_score": 79,
      "warnings": [
        "Setting preference could not be verified: the workbook has no Setting column yet, so this property was not filtered or scored on environment.",
        "Activity restrictions could not be verified against structured property data yet; treat restriction_status as advisory only."
      ]
    },
    {
      "city_name": "Ipoh",
      "country_name": "Malaysia",
      "number_of_days": 5,
      "description": "The Banjaran Hotsprings Retreat scores strongly on your traveler archetype. The Banjaran Hotsprings Retreat scores strongly on your preferred amount of daily structure.",
      "city_image": [],
      "latitude": null,
      "longitude": null,
      "property_id": "retreat_075",
      "match_score": 78,
      "warnings": [
        "Setting preference could not be verified: the workbook has no Setting column yet, so this property was not filtered or scored on environment.",
        "Activity restrictions could not be verified against structured property data yet; treat restriction_status as advisory only."
      ]
    }
  ],
  "response": {
    "suggested_cities": [ "...same array as above..." ],
    "excluded_count": 115,
    "total_candidate_count": 150,
    "data_gaps": [
      "Setting is not yet a database field ...",
      "Activity/Restriction Tags are not yet a database field ..."
    ],
    "extracted_restrictions": {
      "codes": ["no_hiking", "no_water_activities"],
      "accessibility_needs": [],
      "unresolved_text": []
    }
  }
}
```

**Field notes:**

- `property_id` — pass this to `POST /get_tour_plan` (see
  `BACKEND_INTEGRATION_GUIDE.md`) instead of relying only on `city_name`, so
  Step 2 builds the itinerary around the *exact* property that was scored,
  never a different, same-named place.
- `match_score` — 0-100, the "similarity scoring" you asked to keep: it's the
  same scoring engine and scale as `/v2/retreat-recommendations`, just
  attached to a city instead of a raw property list.
- `city_image` / `latitude` / `longitude` can be empty/`null` when the photo
  tool can't verify that exact city in that exact country — this is the same
  safety behavior the old flow had (never attach a photo/coordinate pair from
  a same-named place in the wrong country), unchanged.
- `warnings` — carried over from the matching engine (e.g. restrictions or
  settings that couldn't be verified against the property database yet). Show
  these; don't hide them.
- `response.data_gaps` / `response.extracted_restrictions` — the same audit
  fields documented in `API_V2_DOCS.md`. Useful for logging/debugging on the
  backend side even if not shown to the end user.

## 4. Regenerate — `POST /regenerate_suggested_city`

Request body is unchanged: `{ "session_id": "...", "user_instruction": "..." }`.

Behavior changed: since the underlying ranking is fully deterministic, there
is no model call involved in picking different cities anymore. Regenerating
simply asks the same matching engine for the next-best distinct cities,
**excluding every property already shown in this session** (tracked
server-side, accumulates across multiple regenerate calls). Response shape is
identical to §3.

`user_instruction` is still accepted and stored in the session history for
audit purposes, but it no longer changes which cities come back — there's no
free-text steering step anymore, only "give me different (but still
best-matching) real properties."

**New error case:** `409 Conflict` if `session_id` refers to a session created
before this change (no stored profile to re-rank against) — start a new
session with `/get_suggested_city` instead. `404` if no further distinct
cities exist for the profile (all matching properties/cities already shown).

## 5. Session storage

Nothing new for the backend to manage — sessions are still created and
returned the same way (`session_id`), and `GET /session/{session_id}` still
works unchanged. Internally, the session now additionally stores the original
15-question request and the list of property IDs already shown, purely so
`/regenerate_suggested_city` and Step 2 can use them; this is opaque to
callers.

## 6. Migration checklist

- [ ] Send the 15-question payload (§2) instead of the old `InputData` shape.
- [ ] Read `property_id` and `match_score` off each suggested city.
- [ ] Pass `property_id` through to `POST /get_tour_plan` when the user picks
      a city (see `BACKEND_INTEGRATION_GUIDE.md` §3).
- [ ] Handle the new `409` response from `/regenerate_suggested_city` for any
      session created before this deploy.
- [ ] `POST /v2/retreat-recommendations` still exists separately if you ever
      need the raw ranked property list (not grouped into cities) — unchanged
      by this update.
