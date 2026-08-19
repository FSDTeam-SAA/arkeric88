# Backend, API and Database Developer Handoff

## Objective

Introduce a versioned questionnaire and property-recommendation API that can filter and rank all 150 retreat records safely and consistently.

## Current Problems

- The current request model requires the old questionnaire, including birthdate, old energy/travel fields and exact trip days.
- The API returns cities first and accepts a city name for itinerary generation.
- Retreat selection happens only after city selection and returns one stay.
- Budget is a soft ranking signal rather than a reliable filter.
- The code treats trip days as lodging nights.
- Best Season and several numeric suitability fields are not used in ranking.
- `Primary Modalities` is empty for all records loaded by the current workbook reader.
- Sessions are held in process memory and are not durable across server restarts or multiple workers.

## Required API Contract

Create a versioned endpoint such as `POST /v2/retreat-recommendations`. Do not silently repurpose the existing city endpoint.

Recommended request shape:

```json
{
  "archetype": "burned_out_achiever",
  "escape_from": "noise_stimulation",
  "arrival_priority": "silence_privacy",
  "structure_preference": "optional_rituals",
  "reset_style": "digital_disconnection",
  "physical_intensity": "gentle",
  "party": {
    "type": "solo",
    "adults": 1,
    "children": 0
  },
  "spirituality": "none",
  "travel_window": {
    "mode": "specific",
    "months": [11, 12]
  },
  "planning_service_level": "well_planned",
  "restrictions": {
    "text": "No hiking or water activities",
    "codes": ["no_hiking", "no_water_activities"]
  },
  "settings": ["ocean_beach"],
  "budget": {
    "currency": "USD",
    "per_person_per_night_max": 7000,
    "open_ended": true
  },
  "duration": {
    "bucket": "4_7_nights",
    "exact_nights": null
  },
  "transform_focus": [
    "Burnout Recovery",
    "Sleep",
    "Digital Detox"
  ]
}
```

Backend validation must enforce:

- Known enum values only
- One to three Transform Focus values
- Valid months from 1 through 12
- Positive budget
- Valid adult and child counts
- Consistency between party type and party counts
- Empty `settings` for no preference
- A reasonable maximum length for restriction text

## Required Response Contract

Return ranked properties with stable IDs:

```json
{
  "recommendation_session_id": "...",
  "recommendations": [
    {
      "property_id": "retreat_042",
      "property_name": "Example Retreat",
      "country": "Indonesia",
      "region": "Bali",
      "settings": ["jungle_rainforest"],
      "match_score": 87,
      "score_breakdown": {},
      "match_reasons": ["..."],
      "warnings": ["..."],
      "restriction_status": "partially_verified",
      "avg_night": 400,
      "avg_night_is_lower_bound": true,
      "budget_tier": "Luxury",
      "program_cost": "À la carte",
      "best_season": [4, 5, 6, 7, 8, 9, 10]
    }
  ]
}
```

Itinerary generation must accept `property_id`, not only a city name.

## Database Changes

### Required columns

- Stable `Property ID`
- `Setting` as a multi-value field
- `Family Fit`
- `Small Group Fit`
- `Activity Tags`
- `Restriction/Exclusion Tags`
- `Accessibility Tags`
- `Minimum Nights`, where known
- `Maximum Nights`, where relevant

Optional but recommended:

- `Privacy`
- `Design and Service`
- Normalized numeric price fields
- Normalized program duration and package price

Do not encode “No strong preference” as a property setting.

### Data cleanup

- Correct the workbook/header alignment responsible for empty Primary Modalities.
- Normalize Best Season into month numbers while retaining the source text.
- Normalize Transform Focus into arrays.
- Normalize Archetypes into arrays.
- Convert score fields to integers.
- Store `$1,000+` as amount `1000` plus `is_lower_bound=true`.
- Represent Donation without inventing a numeric price.
- Represent `À la carte` explicitly.

## Budget Rules

- `À la carte`: filter using Avg Night and Budget Tier only.
- Package properties: calculate a duration-adjusted program cost when the data supports it.
- Donation: do not compare as zero dollars.
- Open-ended budget: treat the supplied value as a starting threshold, not a strict maximum.
- Define clearly whether the displayed total includes lodging, programming, meals and activities.
- Use nights, not days, for accommodation totals.

## Season Rules

- Flexible travel applies no season filter.
- Specific travel compares requested months with normalized Best Season months.
- Handle ranges crossing the year boundary, such as November through April.
- Decide and document how `Monthly` differs from `Year-round`; clean the source value if it is incorrect.

## Service Separation

`planning_service_level` must not affect property ranking. Store it for the later itinerary/concierge workflow only.

## Persistence and Versioning

- Keep the old endpoints temporarily if an existing frontend depends on them.
- Store a questionnaire schema version with every recommendation session.
- Persist sessions in a durable store before using multiple API workers.
- Record the database version and scoring version used for each shortlist.

## Required Tests

- API rejects invalid enums and more than three Transform Focus choices.
- Season parsing handles all workbook formats.
- Budget cases cover lower-bound, Donation, package and À la carte prices.
- Hard filters execute before ranking.
- Family requests are warned or filtered based on actual data.
- Every returned property has a stable ID.
- A selected property ID produces the correct itinerary property.
- Old and new endpoints do not reinterpret each other's payloads.

## Definition of Done

- The new endpoint accepts all 15 questionnaire answers without legacy fields.
- All 150 properties can be evaluated directly.
- Responses contain ranked properties, score evidence and warnings.
- Budget, season and supported restrictions are enforced deterministically.
- Property selection remains stable across city names and duplicate locations.
